#!/usr/bin/env python3
"""P1-014 governed replanning, WU dispositions, lineage, and gate re-entry."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import pathlib
import runpy
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN = ROOT / "bin/myrmex-campaign"
sys.path.insert(0, str(ROOT / "scripts"))

import myrmex_campaign_intelligence as intel  # noqa: E402
import myrmex_dag_validate as dag_validate  # noqa: E402
import myrmex_plan_critic as critic  # noqa: E402
import myrmex_plan_revision as activation  # noqa: E402
import myrmex_plan_store as plan_store  # noqa: E402
import myrmex_planner as planner  # noqa: E402
import myrmex_planner_gateway as gateway  # noqa: E402
import myrmex_replanning as replanning  # noqa: E402


def run_campaign(args, state_home, ok=True):
    env = dict(os.environ, XDG_STATE_HOME=str(state_home), PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run([sys.executable, str(BIN), *args], capture_output=True, text=True, env=env)
    if ok and proc.returncode != 0:
        raise AssertionError(proc.stdout + proc.stderr)
    if not ok and proc.returncode == 0:
        raise AssertionError("campaign command unexpectedly succeeded: " + " ".join(args))
    return proc


def tree_bytes(root):
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*") if path.is_file()
    }


def load_payload(campaign_dir, campaign_id, artifact_id):
    return intel.get_artifact(campaign_dir, campaign_id, artifact_id)["artifact"]["payload"]


def refresh_plan_record(record):
    record["plan_digest"] = plan_store.compute_plan_digest(record)
    record["plan_revision_id"] = plan_store.derive_plan_revision_id(record["plan_digest"])
    record["record_digest"] = plan_store.compute_record_digest(record)
    record["record_id"] = plan_store.derive_record_id(record["record_digest"])
    plan_store.validate_plan_revision_record(record)


def build_candidate(ctx, active):
    campaign_dir = ctx["campaign_dir"]
    campaign_id = ctx["campaign_id"]
    active_artifact = intel.get_artifact(
        campaign_dir, campaign_id, "plan-revision/record/" + active["record_id"],
    )["artifact"]
    parent = {"artifact_id": active_artifact["artifact_id"], "artifact_digest": active_artifact["artifact_digest"]}
    old_request_key = hashlib.sha256(active["planning_request_id"].encode("utf-8")).hexdigest()
    old_request = load_payload(campaign_dir, campaign_id, "planning-request/request/" + old_request_key)
    normalized = next(item for item in old_request["input_digests"] if item["kind"] == "normalized-backlog")
    repository = next(item for item in old_request["input_digests"] if item["kind"] == "repository-context")
    repository_context = load_payload(campaign_dir, campaign_id, repository["identity"])
    request_id = "replan-request-p1014"
    task_id = "task-planner-p1014"
    prepared = gateway.prepare_planner_task(
        campaign_dir, campaign_id, 2, request_id, task_id, old_request["run_id"],
        active["objective_id"], active["base_sha"], normalized["identity"].rsplit("/", 1)[-1],
        repository_context, old_request["constraints"], "2026-08-10T05:30:00+00:00", parent,
    )
    work_unit = copy.deepcopy(active["work_units"][0])
    work_unit["id"] = "WU-P1-014"
    work_unit["dependencies"] = []
    work_unit["human_gates"] = []
    work_unit["objective"] = "Execute the governed replacement plan"
    work_unit["terminal_gate"] = "G3-GOVERNED-REPLANNING"
    record = {
        "schema": plan_store.PLAN_SCHEMA, "record_id": "", "plan_revision_id": "",
        "campaign_id": campaign_id, "objective_id": active["objective_id"],
        "planning_request_id": request_id, "base_sha": active["base_sha"],
        "parent_revision": parent, "input_digests": prepared["request"]["input_digests"],
        "assumptions": [], "work_units": [work_unit], "edges": [],
        "lifecycle_status": "proposed", "previous_record_id": None,
        "plan_digest": "", "record_digest": "", "created_at": "2026-08-10T05:30:00+00:00",
    }
    refresh_plan_record(record)
    snapshot = load_payload(campaign_dir, campaign_id, normalized["identity"])
    result = {
        "schema": planner.RESULT_SCHEMA, "request_id": request_id,
        "run_id": old_request["run_id"], "campaign_id": campaign_id,
        "objective_id": active["objective_id"], "base_sha": active["base_sha"],
        "response_type": "plan", "plan_revision": record,
        "analysis": {"facts": ["active plan and trigger are exact"], "assumptions": [], "uncertainties": []},
        "coverage_matrix": [{"backlog_item_id": snapshot["items"][0]["backlog_item_id"], "work_unit_ids": [work_unit["id"]]}],
        "clarification": None, "completion_evidence": [], "authority": dict(planner.AUTHORITY),
        "result_digest": "", "created_at": "2026-08-10T05:30:00+00:00",
    }
    result["result_digest"] = planner._sha({key: value for key, value in result.items() if key != "result_digest"})
    gateway.record_planner_task_result(campaign_dir, campaign_id, 2, request_id, task_id, result)
    # Exact planner request/result replay must resolve the same immutable root.
    gateway.prepare_planner_task(
        campaign_dir, campaign_id, 2, request_id, task_id, old_request["run_id"],
        active["objective_id"], active["base_sha"], normalized["identity"].rsplit("/", 1)[-1],
        repository_context, old_request["constraints"], "2026-08-10T05:30:00+00:00", parent,
    )
    replay = gateway.record_planner_task_result(campaign_dir, campaign_id, 2, request_id, task_id, result)
    assert replay["planner_receipt"]["record_id"] == record["record_id"]
    roots = [
        entry for entry in plan_store.get_plan_chain(campaign_dir, campaign_id, record["plan_revision_id"])
        if entry["previous_record_id"] is None
    ]
    assert roots == [record]
    return record, result, parent, task_id


def set_wu_state(campaign_dir, *, status, phase):
    path = campaign_dir / "campaign.json"
    campaign = json.loads(path.read_text(encoding="utf-8"))
    wu = campaign["work_units"][0]
    wu["status"] = status
    wu["phase"] = phase
    wu["evidence"] = {"receipt": "historical-evidence"}
    wu["writer_receipt"] = {"receipt": "historical-writer"}
    wu["verifier_receipt"] = {"receipt": "historical-verifier"}
    wu["ci_operation"] = {"receipt": "historical-ci"}
    wu["commit_receipt"] = {"receipt": "historical-commit"}
    wu["correction_runs"] = [{"receipt": "historical-correction"}]
    path.write_text(json.dumps(campaign, indent=2) + "\n", encoding="utf-8")
    return campaign, copy.deepcopy(wu)


def disposition(wu, trigger_id, action, replacement_id=None):
    active = wu["status"] in {"active", "verifying", "remediating", "ci", "delivering"}
    return {
        "work_unit_id": wu["id"], "disposition": action,
        "replacement_work_unit_ids": [replacement_id] if action == "replace" else [],
        "reason": f"governed {action} decision",
        "interruption": {
            "transition": "interrupted_for_replan", "from_status": wu["status"],
            "from_phase": wu["phase"], "evidence_trigger_id": trigger_id,
        } if active else None,
    }


def clone_state(source_home, root, label, campaign_id):
    state_home = root / f"state-{label}"
    shutil.copytree(source_home, state_home)
    return state_home, state_home / "myrmex/campaigns" / campaign_id


def make_authority(plan_revision_id):
    body = {
        "schema": activation.AUTHORITY_SCHEMA, "role": "primary_orchestrator", "subject": "p1-014-test",
        "plan_revision_id": plan_revision_id, "scope": "plan_activation_only",
        "granted_at": "2026-08-10T06:20:00+00:00", "expires_at": "2026-08-10T07:00:00+00:00",
    }
    digest = activation._sha(body)
    return {**body, "authority_id": "auth_" + digest, "authority_digest": digest}


activation_fixtures = runpy.run_path(str(ROOT / "tests/test-plan-activation.py"))
prepare = activation_fixtures["prepare"]
make_initial_authority = activation_fixtures["make_authority"]
make_initial_decision = activation_fixtures["make_decision"]
make_review = activation_fixtures["make_review"]
ACTIVATED_AT = activation_fixtures["ACTIVATED_AT"]


with tempfile.TemporaryDirectory(prefix="myrmex-p1014-") as td:
    root = pathlib.Path(td)
    ctx = prepare(root, "replanmain")
    reviewed = plan_store.get_plan_head(ctx["campaign_dir"], ctx["campaign_id"], ctx["proposed"]["plan_revision_id"])
    initial_receipt = activation.activate_plan(
        ctx["campaign_dir"], ctx["campaign_id"], 2, "activation-replan-p1014",
        reviewed["plan_revision_id"], reviewed["record_id"], ctx["review"]["review_digest"],
        ctx["dag"], make_initial_authority(reviewed["plan_revision_id"]),
        [make_initial_decision(reviewed["plan_revision_id"])], ACTIVATED_AT,
    )
    active = plan_store.get_plan_record(ctx["campaign_dir"], ctx["campaign_id"], initial_receipt["active_record_id"])

    evidence_payload = {"schema": "myrmex.test-replan-evidence/v1", "campaign_id": ctx["campaign_id"], "human_decision": "replace active plan"}
    evidence_id = "replan-evidence/human-decision/" + replanning._sha(evidence_payload)
    intel.put_artifact(ctx["campaign_dir"], ctx["campaign_id"], 2, "decision", evidence_id, evidence_payload)
    trigger_result = replanning.record_trigger(
        ctx["campaign_dir"], ctx["campaign_id"], 2, "human_decision", "Operator changed the bounded plan",
        [replanning.evidence_reference(ctx["campaign_dir"], ctx["campaign_id"], evidence_id)],
        "2026-08-10T05:00:00+00:00", {"producer": "test-governed-replanning", "event_id": "human-decision-001"},
    )
    trigger_id = trigger_result["trigger"]["trigger_id"]
    candidate, candidate_result, parent, planner_task_id = build_candidate(ctx, active)
    campaign, old_wu = set_wu_state(ctx["campaign_dir"], status="pending", phase="ready")

    # Every permitted pending disposition is deterministic; completed history
    # is immutable, while active/verifying states require a typed interruption.
    expected = {"preserve": "pending", "replace": "superseded", "supersede": "superseded", "cancel": "cancelled", "defer": "blocked"}
    for action, expected_status in expected.items():
        after, _ = replanning._apply_dispositions(
            campaign, [disposition(old_wu, trigger_id, action, candidate["work_units"][0]["id"])],
            active["plan_revision_id"], {candidate["work_units"][0]["id"]}, {trigger_id}, "2026-08-10T06:00:00+00:00",
        )
        assert after["work_units"][0]["status"] == expected_status
    completed_campaign = copy.deepcopy(campaign)
    completed_campaign["work_units"][0]["status"] = "completed"
    completed_campaign["work_units"][0]["phase"] = "completed"
    completed_before = copy.deepcopy(completed_campaign["work_units"][0])
    completed_after, _ = replanning._apply_dispositions(
        completed_campaign, [disposition(completed_campaign["work_units"][0], trigger_id, "preserve")],
        active["plan_revision_id"], {candidate["work_units"][0]["id"]}, {trigger_id}, "2026-08-10T06:00:00+00:00",
    )
    assert completed_after["work_units"][0] == completed_before
    active_campaign = copy.deepcopy(campaign)
    active_campaign["work_units"][0].update(status="active", phase="implementing")
    try:
        replanning._apply_dispositions(
            active_campaign, [{**disposition(active_campaign["work_units"][0], trigger_id, "supersede"), "interruption": None}],
            active["plan_revision_id"], {candidate["work_units"][0]["id"]}, {trigger_id}, "2026-08-10T06:00:00+00:00",
        )
    except replanning.ReplanTriggerInputInvalid:
        pass
    else:
        raise AssertionError("active WU interruption was accepted without a typed transition")

    main_row = disposition(old_wu, trigger_id, "replace", candidate["work_units"][0]["id"])
    before_preview = tree_bytes(ctx["campaign_dir"])
    preview = replanning.preview_replan(
        ctx["campaign_dir"], ctx["campaign_id"], 2, candidate["plan_revision_id"],
        [trigger_id], [main_row], "2026-08-10T06:00:00+00:00",
    )
    assert preview["status"] == "PREVIEW" and tree_bytes(ctx["campaign_dir"]) == before_preview
    decision = preview["decision"]
    replanning.validate_replan_decision(decision)
    import jsonschema
    jsonschema.validate(decision, json.loads((ROOT / "contracts/replan-decision-v1.schema.json").read_text(encoding="utf-8")))
    assert decision["semantic_plan_diff"]["added_work_unit_ids"] == [candidate["work_units"][0]["id"]]
    assert decision["semantic_plan_diff"]["removed_work_unit_ids"] == [old_wu["id"]]
    assert decision["parent_revision"] == parent and decision["planning_result_digest"] == candidate_result["result_digest"]

    dispositions_file = root / "dispositions.json"
    dispositions_file.write_text(json.dumps([main_row]), encoding="utf-8")
    cli_preview = json.loads(run_campaign([
        "replan-preview", ctx["campaign_id"], "--candidate-plan-revision-id", candidate["plan_revision_id"],
        "--trigger-id", trigger_id, "--dispositions-json", str(dispositions_file),
        "--decided-at", "2026-08-10T06:00:00+00:00", "--expect-revision", "2",
    ], ctx["state_home"]).stdout)
    assert cli_preview == {"status": "PREVIEW", "decision": decision}
    assert tree_bytes(ctx["campaign_dir"]) == before_preview

    try:
        replanning.preview_replan(
            ctx["campaign_dir"], ctx["campaign_id"], 2, active["plan_revision_id"],
            [trigger_id], [main_row], "2026-08-10T06:00:00+00:00",
        )
    except replanning.ReplanTriggerInputInvalid:
        pass
    else:
        raise AssertionError("stale active plan was accepted as a candidate")

    competing = replanning.preview_replan(
        ctx["campaign_dir"], ctx["campaign_id"], 2, candidate["plan_revision_id"], [trigger_id],
        [disposition(old_wu, trigger_id, "supersede")], "2026-08-10T06:00:00+00:00",
    )["decision"]
    assert competing["decision_id"] != decision["decision_id"]

    # Completed, active, and verifying source states all pass through the full
    # governed apply boundary. The active case injects the campaign/lifecycle
    # crash window and recovers without a second decision or lifecycle fork.
    completed_home, completed_dir = clone_state(ctx["state_home"], root, "completed", ctx["campaign_id"])
    _, completed_wu = set_wu_state(completed_dir, status="completed", phase="completed")
    completed_preview = replanning.preview_replan(
        completed_dir, ctx["campaign_id"], 2, candidate["plan_revision_id"], [trigger_id],
        [disposition(completed_wu, trigger_id, "preserve")], "2026-08-10T06:00:00+00:00",
    )
    completed_result = replanning.apply_replan(completed_dir, ctx["campaign_id"], completed_preview["decision"])
    assert completed_result["status"] == "APPLIED"
    assert json.loads((completed_dir / "campaign.json").read_text(encoding="utf-8"))["work_units"][0] == completed_wu

    active_home, active_dir = clone_state(ctx["state_home"], root, "active", ctx["campaign_id"])
    _, interrupted_wu = set_wu_state(active_dir, status="active", phase="implementing")
    interrupted_preview = replanning.preview_replan(
        active_dir, ctx["campaign_id"], 2, candidate["plan_revision_id"], [trigger_id],
        [disposition(interrupted_wu, trigger_id, "supersede")], "2026-08-10T06:00:00+00:00",
    )
    original_store = replanning.plan_store.store_plan_record
    replanning.plan_store.store_plan_record = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("injected after campaign apply"))
    try:
        replanning.apply_replan(active_dir, ctx["campaign_id"], interrupted_preview["decision"])
    except RuntimeError as error:
        assert "injected" in str(error)
    else:
        raise AssertionError("replan lifecycle interruption was not injected")
    finally:
        replanning.plan_store.store_plan_record = original_store
    assert json.loads((active_dir / "campaign.json").read_text(encoding="utf-8"))["revision"] == 3
    assert plan_store.get_plan_head(active_dir, ctx["campaign_id"], active["plan_revision_id"])["lifecycle_status"] == "active"
    recovered = replanning.apply_replan(active_dir, ctx["campaign_id"], interrupted_preview["decision"])
    assert recovered["status"] == "APPLIED"
    interrupted_after = json.loads((active_dir / "campaign.json").read_text(encoding="utf-8"))["work_units"][0]
    assert interrupted_after["status"] == "superseded"
    assert interrupted_after["recovery_events"][-1]["transition"] == "interrupted_for_replan"
    assert replanning.apply_replan(active_dir, ctx["campaign_id"], interrupted_preview["decision"])["status"] == "REUSED"

    verifying_home, verifying_dir = clone_state(ctx["state_home"], root, "verifying", ctx["campaign_id"])
    _, verifying_wu = set_wu_state(verifying_dir, status="verifying", phase="verifying")
    verifying_preview = replanning.preview_replan(
        verifying_dir, ctx["campaign_id"], 2, candidate["plan_revision_id"], [trigger_id],
        [disposition(verifying_wu, trigger_id, "defer")], "2026-08-10T06:00:00+00:00",
    )
    replanning.apply_replan(verifying_dir, ctx["campaign_id"], verifying_preview["decision"])
    verifying_after = json.loads((verifying_dir / "campaign.json").read_text(encoding="utf-8"))["work_units"][0]
    assert verifying_after["status"] == "blocked" and verifying_after["recovery_events"][-1]["from_status"] == "verifying"

    decision_file = root / "decision.json"
    decision_file.write_text(json.dumps(decision), encoding="utf-8")
    applied = json.loads(run_campaign([
        "replan-apply", ctx["campaign_id"], "--decision-json", str(decision_file),
    ], ctx["state_home"]).stdout)
    assert applied["status"] == "APPLIED"
    replanning.validate_replan_receipt(applied["receipt"])
    assert plan_store.get_plan_head(ctx["campaign_dir"], ctx["campaign_id"], active["plan_revision_id"])["lifecycle_status"] == "superseded"
    assert plan_store.get_plan_head(ctx["campaign_dir"], ctx["campaign_id"], candidate["plan_revision_id"])["lifecycle_status"] == "proposed"

    try:
        replanning.apply_replan(ctx["campaign_dir"], ctx["campaign_id"], competing)
    except replanning.ReplanTriggerConflict as error:
        assert "conflicting" in str(error)
    else:
        raise AssertionError("conflicting human replan decision was accepted")
    before_replay = tree_bytes(ctx["campaign_dir"])
    exact_replay = json.loads(run_campaign([
        "replan-apply", ctx["campaign_id"], "--decision-json", str(decision_file),
    ], ctx["state_home"]).stdout)
    assert exact_replay["status"] == "REUSED" and exact_replay["receipt"] == applied["receipt"]
    assert tree_bytes(ctx["campaign_dir"]) == before_replay

    # The candidate remains proposed after replanning and must re-enter critic,
    # compilation, semantic DAG validation, and governed activation.
    review_prepared = critic.prepare_critic_task(
        ctx["campaign_dir"], ctx["campaign_id"], 3, "review-replan-p1014", "task-critic-replan-p1014",
        candidate["planning_request_id"], planner_task_id, candidate["record_id"], "2026-08-10T06:10:00+00:00",
    )
    candidate_review = make_review(review_prepared["task_intent"])
    candidate_review["created_at"] = "2026-08-10T06:10:00+00:00"
    candidate_review["review_digest"] = critic._sha({key: value for key, value in candidate_review.items() if key != "review_digest"})
    critic.record_review(
        ctx["campaign_dir"], ctx["campaign_id"], 3, "review-replan-p1014", "task-critic-replan-p1014", candidate_review,
    )
    candidate_reviewed = plan_store.get_plan_head(ctx["campaign_dir"], ctx["campaign_id"], candidate["plan_revision_id"])
    assert candidate_reviewed["lifecycle_status"] == "reviewed"
    compiled = json.loads(run_campaign([
        "plan-compile-apply", ctx["campaign_id"], "--plan-revision-id", candidate["plan_revision_id"], "--expect-revision", "3",
    ], ctx["state_home"]).stdout)
    assert compiled["status"] == "APPLIED" and compiled["campaign_revision_after"] == 4
    campaign_after_compile = json.loads((ctx["campaign_dir"] / "campaign.json").read_text(encoding="utf-8"))
    replacement = next(wu for wu in campaign_after_compile["work_units"] if wu["id"] == candidate["work_units"][0]["id"])
    historical = next(wu for wu in campaign_after_compile["work_units"] if wu["id"] == old_wu["id"])
    assert replacement["work_order"]["plan_provenance"]["plan_revision_id"] == candidate["plan_revision_id"]
    assert all(replacement[field] in (None, []) for field in ("evidence", "writer_receipt", "verifier_receipt", "commit_receipt", "correction_runs"))
    assert historical["writer_receipt"] == old_wu["writer_receipt"] and historical["commit_receipt"] == old_wu["commit_receipt"]

    dag = json.loads(run_campaign([
        "dag", ctx["campaign_id"], "--plan-revision-id", candidate["plan_revision_id"], "--expect-revision", "4",
    ], ctx["state_home"]).stdout)
    dag_validate.validate_result(dag)
    assert dag["status"] == "PASS" and dag["topological_order"] == [candidate["work_units"][0]["id"]]
    dag_file = root / "candidate-dag.json"; dag_file.write_text(json.dumps(dag), encoding="utf-8")
    authority_file = root / "candidate-authority.json"; authority_file.write_text(json.dumps(make_authority(candidate["plan_revision_id"])), encoding="utf-8")
    decisions_file = root / "candidate-decisions.json"; decisions_file.write_text("[]\n", encoding="utf-8")
    candidate_activation = json.loads(run_campaign([
        "plan-activate", ctx["campaign_id"], "--request-id", "activation-candidate-p1014",
        "--plan-revision-id", candidate["plan_revision_id"], "--reviewed-record-id", candidate_reviewed["record_id"],
        "--review-digest", candidate_review["review_digest"], "--dag-validation-json", str(dag_file),
        "--authority-json", str(authority_file), "--human-decisions-json", str(decisions_file),
        "--activated-at", "2026-08-10T06:30:00+00:00", "--expect-revision", "4",
    ], ctx["state_home"]).stdout)
    activation.validate_activation_receipt(candidate_activation)
    old_chain = plan_store.get_plan_chain(ctx["campaign_dir"], ctx["campaign_id"], active["plan_revision_id"])
    candidate_chain = plan_store.get_plan_chain(ctx["campaign_dir"], ctx["campaign_id"], candidate["plan_revision_id"])
    assert [item["lifecycle_status"] for item in old_chain][-2:] == ["active", "superseded"]
    assert [item["lifecycle_status"] for item in candidate_chain] == ["proposed", "reviewed", "validated", "active"]
    assert candidate_chain[0]["parent_revision"] == parent
    assert decision["active_record_id"] == old_chain[-2]["record_id"]
    assert decision["candidate_record_id"] == candidate_chain[0]["record_id"]
    assert decision["trigger_references"] == [{"trigger_id": trigger_id, "trigger_digest": trigger_result["trigger"]["trigger_digest"]}]
    assert len(activation._active_heads(ctx["campaign_dir"], ctx["campaign_id"])) == 1

print("governed replanning: completed/pending/active/verifying dispositions, crash replay, no duplicate/conflict, lineage, and critic-DAG-activation re-entry PASS")
