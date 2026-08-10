#!/usr/bin/env python3
"""P1-011 semantic DAG invariants, corruption, replay, and property tests."""
from __future__ import annotations

import copy
import json
import os
import pathlib
import random
import runpy
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN = ROOT / "bin/myrmex-campaign"
sys.path.insert(0, str(ROOT / "scripts"))
import myrmex_dag_validate as dag_validate  # noqa: E402
import myrmex_plan_critic as critic  # noqa: E402
import myrmex_work_unit_compiler as compiler  # noqa: E402


def run_campaign(args, state_home, ok=True):
    env = dict(os.environ, XDG_STATE_HOME=str(state_home), PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run([sys.executable, str(BIN), *args], capture_output=True, text=True, env=env)
    if ok and proc.returncode != 0:
        raise AssertionError(proc.stdout + proc.stderr)
    if not ok and proc.returncode == 0:
        raise AssertionError("command unexpectedly succeeded: " + " ".join(args))
    return proc


critic_fixtures = runpy.run_path(str(ROOT / "tests/test-plan-critic.py"))
fixture = critic_fixtures["fixture"]
make_review = critic_fixtures["make_review"]
source_root, campaign_id, planning_request_id, planner_task_id, proposed = fixture("semanticdag")
prepared = critic.prepare_critic_task(
    source_root, campaign_id, 1, "review-semantic-dag", "task-critic-semantic-dag",
    planning_request_id, planner_task_id, proposed["record_id"], "2026-08-10T03:00:00+00:00",
)
review = make_review(prepared["task_intent"])
review["created_at"] = "2026-08-10T03:00:00+00:00"
review["review_digest"] = critic._sha({key: value for key, value in review.items() if key != "review_digest"})
critic.record_review(source_root, campaign_id, 1, "review-semantic-dag", "task-critic-semantic-dag", review)


with tempfile.TemporaryDirectory(prefix="myrmex-dag-state-") as state_home, tempfile.TemporaryDirectory(prefix="myrmex-dag-repo-") as repo:
    run_campaign(["init", "--id", campaign_id, "--title", "Semantic DAG", "--repo-root", repo], state_home)
    campaign_dir = pathlib.Path(state_home) / "myrmex/campaigns" / campaign_id
    shutil.copytree(source_root / "intelligence", campaign_dir / "intelligence", dirs_exist_ok=True)
    applied = json.loads(run_campaign([
        "plan-compile-apply", campaign_id, "--plan-revision-id", proposed["plan_revision_id"],
        "--expect-revision", "1", "--protected-dirty-path", "protected.txt",
    ], state_home).stdout)
    assert applied["status"] == "APPLIED" and applied["campaign_revision_after"] == 2
    campaign_file = campaign_dir / "campaign.json"
    events_file = campaign_dir / "events.jsonl"
    pristine = json.loads(campaign_file.read_text(encoding="utf-8"))
    before_campaign, before_events = campaign_file.read_bytes(), events_file.read_bytes()

    args = ["dag", campaign_id, "--plan-revision-id", proposed["plan_revision_id"], "--expect-revision", "2"]
    first = json.loads(run_campaign(args, state_home).stdout)
    second = json.loads(run_campaign(args, state_home).stdout)
    assert first == second and first["status"] == "PASS"
    assert first["validation_id"] == "dagval_" + first["validation_digest"]
    assert first["topological_order"] == ["WU-P1-009"] and first["critical_path"] == ["WU-P1-009"]
    assert first["defects"] == [] and first["authority"]["activate_plan"] is False
    dag_validate.validate_result(first)
    tampered_receipt = copy.deepcopy(first); tampered_receipt["graph_digest"] = "0" * 64
    try:
        dag_validate.validate_result(tampered_receipt)
    except dag_validate.DAGValidationResultInvalid:
        pass
    else:
        raise AssertionError("tampered validation receipt accepted")
    assert campaign_file.read_bytes() == before_campaign and events_file.read_bytes() == before_events

    stale = json.loads(run_campaign(args[:-1] + ["1"], state_home, ok=False).stdout)
    assert stale["status"] == "FAIL" and "DAG-002" in {item["code"] for item in stale["defects"]}

    def corrupt(mutator, required_codes):
        candidate = copy.deepcopy(pristine)
        mutator(candidate)
        campaign_file.write_text(json.dumps(candidate, indent=2) + "\n", encoding="utf-8")
        result = json.loads(run_campaign(args, state_home, ok=False).stdout)
        codes = {item["code"] for item in result["defects"]}
        assert result["status"] == "FAIL" and set(required_codes).issubset(codes), (required_codes, codes)
        campaign_file.write_bytes(before_campaign)

    corrupt(lambda data: data["dag"]["edges"].append(["WU-GHOST", "WU-P1-009"]), {"DAG-010", "DAG-014"})
    corrupt(lambda data: (data["work_units"][0].update(dependencies=["WU-P1-009"]), data["dag"].update(edges=[["WU-P1-009", "WU-P1-009"]])), {"DAG-011"})
    corrupt(lambda data: data["dag"]["edges"].extend(data["dag"]["edges"] or [["WU-P1-009", "WU-P1-009"], ["WU-P1-009", "WU-P1-009"]]), {"DAG-009"})

    def corrupt_order(data):
        order = data["work_units"][0]["work_order"]
        order["objective"] = "tampered objective"
        order["work_order_digest"] = compiler._sha({key: value for key, value in order.items() if key not in {"work_order_id", "work_order_digest"}})
        order["work_order_id"] = "wo_" + order["work_order_digest"]
        data["work_units"][0]["objective"] = order["objective"]
    corrupt(corrupt_order, {"DAG-022"})

    def unsafe_scope(data):
        order = data["work_units"][0]["work_order"]
        order["scope"]["allowed_paths"] = ["../outside"]
        order["work_order_digest"] = compiler._sha({key: value for key, value in order.items() if key not in {"work_order_id", "work_order_digest"}})
        order["work_order_id"] = "wo_" + order["work_order_digest"]
        data["work_units"][0]["scope"] = ["../outside"]
    corrupt(unsafe_scope, {"DAG-024"})

    def hidden_gate(data):
        order = data["work_units"][0]["work_order"]
        order["human_gates"] = [{"gate_id": "human-1", "decision_type": "approve", "reason": "human authority", "required_before": "work_unit_ready"}]
        order["work_order_digest"] = compiler._sha({key: value for key, value in order.items() if key not in {"work_order_id", "work_order_digest"}})
        order["work_order_id"] = "wo_" + order["work_order_digest"]
        data["work_units"][0]["status"] = "ready"
    corrupt(hidden_gate, {"DAG-032"})

    # Structural readiness also refuses a compiled hidden human gate, even in
    # legacy DAG inspection mode; the campaign is restored byte-for-byte after.
    gated = copy.deepcopy(pristine); hidden_gate(gated)
    campaign_file.write_text(json.dumps(gated, indent=2) + "\n", encoding="utf-8")
    assert json.loads(run_campaign(["dag", campaign_id], state_home).stdout)["ready_work_units"] == []
    campaign_file.write_bytes(before_campaign)

    reviewed = compiler._reviewed_head_read_only(campaign_dir, campaign_id, proposed["plan_revision_id"])
    pass_receipt = compiler._review_receipt(campaign_dir, campaign_id, reviewed)
    _, planning_result = compiler._planning_result(campaign_dir, campaign_id, reviewed)
    original_head, original_receipt, original_result = compiler._reviewed_head_read_only, compiler._review_receipt, compiler._planning_result

    def validate_generated(campaign, plan, result):
        compiler._reviewed_head_read_only = lambda *_: plan
        compiler._review_receipt = lambda *_: pass_receipt
        compiler._planning_result = lambda *_: ({}, result)
        try:
            return dag_validate.validate_semantic_dag(campaign_dir, campaign, 2, plan["plan_revision_id"])
        finally:
            compiler._reviewed_head_read_only, compiler._review_receipt, compiler._planning_result = original_head, original_receipt, original_result

    # Seeded generated acyclic graphs exercise deterministic topology and
    # critical-path selection without adding a non-stdlib property dependency.
    rng = random.Random(11011)
    template_wu = pristine["work_units"][0]
    template_plan_wu = reviewed["work_units"][0]
    backlog_id = template_wu["work_order"]["backlog_provenance"][0]["backlog_item_id"]
    for size in range(1, 13):
        ids = [f"WU-PROP-{index:02d}" for index in range(size)]
        edges = []
        generated_plan_wus, generated_campaign_wus = [], []
        for index, wu_id in enumerate(ids):
            dependencies = [ids[prior] for prior in range(index) if rng.randrange(4) == 0]
            edges.extend([dependency, wu_id] for dependency in dependencies)
            plan_wu = copy.deepcopy(template_plan_wu)
            plan_wu.update(id=wu_id, objective=f"Property {index}", dependencies=dependencies)
            plan_wu["scope"]["allowed_paths"] = [f"property/{index}/"]
            generated_plan_wus.append(plan_wu)
            order = copy.deepcopy(template_wu["work_order"])
            order.update(work_unit_id=wu_id, objective=plan_wu["objective"], dependencies=dependencies)
            order["scope"]["allowed_paths"] = [f"property/{index}/"]
            order["scope"]["preexisting_dirty_paths"] = []
            order["work_order_digest"] = compiler._sha({key: value for key, value in order.items() if key not in {"work_order_id", "work_order_digest"}})
            order["work_order_id"] = "wo_" + order["work_order_digest"]
            campaign_wu = copy.deepcopy(template_wu)
            campaign_wu.update(id=wu_id, status="pending", phase="ready", objective=plan_wu["objective"], dependencies=dependencies, scope=[f"property/{index}/"], work_order=order)
            generated_campaign_wus.append(campaign_wu)
        plan = copy.deepcopy(reviewed); plan["work_units"] = generated_plan_wus; plan["edges"] = edges
        campaign = copy.deepcopy(pristine); campaign["work_units"] = generated_campaign_wus; campaign["dag"]["edges"] = edges
        result = copy.deepcopy(planning_result); result["coverage_matrix"] = [{"backlog_item_id": backlog_id, "work_unit_ids": ids}]
        validation = validate_generated(campaign, plan, result)
        assert validation["status"] == "PASS", validation["defects"]
        assert validation == validate_generated(campaign, plan, result)
        assert sorted(validation["topological_order"]) == ids and validation["critical_path"]

    def bind_order(campaign_wu, plan_wu):
        order = campaign_wu["work_order"]
        order.update(work_unit_id=plan_wu["id"], objective=plan_wu["objective"], dependencies=plan_wu["dependencies"])
        order["scope"]["allowed_paths"] = plan_wu["scope"]["allowed_paths"]
        order["human_gates"] = plan_wu["human_gates"]
        order["work_order_digest"] = compiler._sha({key: value for key, value in order.items() if key not in {"work_order_id", "work_order_digest"}})
        order["work_order_id"] = "wo_" + order["work_order_digest"]
        campaign_wu.update(id=plan_wu["id"], objective=plan_wu["objective"], dependencies=plan_wu["dependencies"], scope=plan_wu["scope"]["allowed_paths"])

    # A coherent two-node cycle is rejected independently of work-order and
    # plan projection checks.
    cycle_campaign = copy.deepcopy(campaign); cycle_plan = copy.deepcopy(plan); cycle_result = copy.deepcopy(result)
    cycle_campaign["work_units"] = cycle_campaign["work_units"][:2]
    cycle_plan["work_units"] = cycle_plan["work_units"][:2]
    cycle_ids = [wu["id"] for wu in cycle_plan["work_units"]]
    cycle_plan["work_units"][0]["dependencies"] = [cycle_ids[1]]
    cycle_plan["work_units"][1]["dependencies"] = [cycle_ids[0]]
    cycle_plan["edges"] = [[cycle_ids[1], cycle_ids[0]], [cycle_ids[0], cycle_ids[1]]]
    cycle_campaign["dag"]["edges"] = copy.deepcopy(cycle_plan["edges"])
    for campaign_wu, plan_wu in zip(cycle_campaign["work_units"], cycle_plan["work_units"]): bind_order(campaign_wu, plan_wu)
    cycle_result["coverage_matrix"] = [{"backlog_item_id": backlog_id, "work_unit_ids": cycle_ids}]
    cycle_validation = validate_generated(cycle_campaign, cycle_plan, cycle_result)
    assert "DAG-015" in {item["code"] for item in cycle_validation["defects"]}

    # Independent WUs may not claim the same repository resource. The graph
    # must order them explicitly or semantic activation fails.
    resource_campaign = copy.deepcopy(cycle_campaign); resource_plan = copy.deepcopy(cycle_plan)
    resource_plan["edges"] = []; resource_campaign["dag"]["edges"] = []
    for campaign_wu, plan_wu in zip(resource_campaign["work_units"], resource_plan["work_units"]):
        plan_wu["dependencies"] = []; plan_wu["scope"]["allowed_paths"] = ["shared/resource/"]; bind_order(campaign_wu, plan_wu)
    resource_validation = validate_generated(resource_campaign, resource_plan, cycle_result)
    assert "DAG-031" in {item["code"] for item in resource_validation["defects"]}

    uncovered_result = copy.deepcopy(cycle_result)
    uncovered_id = "backlog_" + "9" * 64
    uncovered_result["coverage_matrix"].append({"backlog_item_id": uncovered_id, "work_unit_ids": [cycle_ids[0]]})
    uncovered_validation = validate_generated(resource_campaign, resource_plan, uncovered_result)
    assert uncovered_validation["uncovered_backlog_item_ids"] == [uncovered_id]
    assert "DAG-029" in {item["code"] for item in uncovered_validation["defects"]}

    gated_campaign = copy.deepcopy(resource_campaign); gated_plan = copy.deepcopy(resource_plan); gated_result = copy.deepcopy(cycle_result)
    gated_campaign["work_units"] = gated_campaign["work_units"][:1]
    gated_plan["work_units"] = gated_plan["work_units"][:1]
    gated_id = gated_plan["work_units"][0]["id"]
    gate = {"gate_id": "gate-property", "decision_type": "approve", "reason": "human authority", "required_before": "work_unit_ready"}
    gated_plan["work_units"][0]["human_gates"] = [gate]
    gated_plan["work_units"][0]["scope"]["allowed_paths"] = ["gated/resource/"]
    gated_plan["edges"] = []; gated_campaign["dag"]["edges"] = []
    bind_order(gated_campaign["work_units"][0], gated_plan["work_units"][0])
    gated_campaign["work_units"][0]["status"] = "pending"
    gated_result["coverage_matrix"] = [{"backlog_item_id": backlog_id, "work_unit_ids": [gated_id]}]
    gated_validation = validate_generated(gated_campaign, gated_plan, gated_result)
    assert gated_validation["status"] == "PASS" and gated_validation["ready_work_units"] == []
    assert gated_validation["human_gated_work_units"] == [gated_id]

    fuzzed = copy.deepcopy(gated_campaign); fuzzed["dag"]["edges"] = [[gated_id], 7, [gated_id, gated_id, gated_id]]
    fuzzed_validation = validate_generated(fuzzed, gated_plan, gated_result)
    assert "DAG-008" in {item["code"] for item in fuzzed_validation["defects"]}

    invalid_parent = copy.deepcopy(reviewed)
    invalid_parent["parent_revision"] = {"artifact_id": "plan-revision/record/planrec_" + "f" * 64, "artifact_digest": "e" * 64}
    parent_result = validate_generated(pristine, invalid_parent, planning_result)
    assert parent_result["status"] == "FAIL" and "DAG-028" in {item["code"] for item in parent_result["defects"]}
    assert campaign_file.read_bytes() == before_campaign and events_file.read_bytes() == before_events

print("semantic DAG: exact provenance, corruption, gates, property DAGs, replay, and determinism PASS")
