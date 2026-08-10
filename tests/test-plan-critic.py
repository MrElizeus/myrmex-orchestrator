#!/usr/bin/env python3
"""P1-009 independent plan critic, adversarial review, and replay tests."""
from __future__ import annotations

import copy
import json
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import myrmex_backlog_normalizer as backlog  # noqa: E402
import myrmex_campaign_intelligence as intel  # noqa: E402
import myrmex_plan_critic as critic  # noqa: E402
import myrmex_plan_store as plan_store  # noqa: E402
import myrmex_planner as planner  # noqa: E402
import myrmex_planner_gateway as gateway  # noqa: E402

failures: list[str] = []


def check(value, message):
    if not value:
        failures.append(message)


def raises(exc, fn, message):
    try:
        fn()
    except exc:
        return
    except Exception as error:
        failures.append(f"{message}: unexpected {type(error).__name__}: {error}")
    else:
        failures.append(f"{message}: no failure")


def fixture(suffix="main", *, cycle=False, hidden_decision=False):
    root = pathlib.Path(tempfile.mkdtemp(prefix=f"myrmex-plan-critic-{suffix}-"))
    campaign_id = f"camp-p1009-{suffix}"
    identity = {"kind": "local-roadmap", "canonical_id": f"{suffix}.md"}
    entity = "srcitem_" + ("a" if suffix == "main" else "b") * 64
    item = {
        "schema": backlog.BACKLOG_ITEM_SCHEMA, "backlog_item_id": "", "item_digest": "",
        "source_adapter": "local-markdown-roadmap/v1", "source_identity": identity,
        "source_entity_type": "local-item", "source_entity_id": entity,
        "title": "review proposed plan", "priority": None, "state": None,
        "dependency_hints": [], "constraints": [], "context_constraints": [],
        "labels": [], "group_ref": None,
    }
    item["backlog_item_id"] = backlog.compute_backlog_item_id(item["source_adapter"], identity, entity)
    item["item_digest"] = backlog.compute_item_digest(item)
    item_id = f"normalized-backlog/item/{item['backlog_item_id']}/{item['item_digest']}"
    intel.put_artifact(root, campaign_id, 1, "backlog", item_id, item)
    observation_digest = ("c" if suffix == "main" else "d") * 64
    snapshot = {
        "schema": backlog.BACKLOG_SNAPSHOT_SCHEMA, "snapshot_record_id": "",
        "snapshot_record_digest": "", "snapshot_digest": "", "source_count": 1,
        "sources": [{
            "operation_id": "importop-" + observation_digest[:24],
            "observation_id": "srcobs_" + observation_digest,
            "observation_digest": observation_digest, "request_digest": "e" * 64,
            "content_digest": "f" * 64, "outcome": "changed",
            "adapter": "local-markdown-roadmap/v1", "source_identity": identity,
        }],
        "item_count": 1,
        "items": [{"backlog_item_id": item["backlog_item_id"], "item_digest": item["item_digest"], "artifact_id": item_id}],
    }
    snapshot["snapshot_digest"] = backlog.compute_snapshot_digest_from_snapshot(snapshot)
    snapshot["snapshot_record_digest"] = backlog.compute_snapshot_record_digest(snapshot)
    snapshot["snapshot_record_id"] = "blsnaprec_" + snapshot["snapshot_record_digest"]
    intel.put_artifact(root, campaign_id, 1, "backlog", "normalized-backlog/snapshot/" + snapshot["snapshot_record_id"], snapshot)
    run_id = f"run-p1009-{suffix}"
    objective_id = f"obj-p1009-{suffix}"
    repository_context = {
        "schema": "myrmex.repository-context/v1", "run_id": run_id,
        "objective_id": objective_id, "repository_root": "/repo", "branch": "feat/p1",
        "base_sha": "1" * 40, "git_status": ["clean"], "objective": "Review a bounded plan",
        "relevant_files": ["scripts/myrmex_plan_critic.py"], "relevant_symbols": ["validate_review"],
        "architecture": ["independent critic"], "current_behavior": ["proposed plan exists"],
        "tests": ["tests/test-plan-critic.py"], "data_contracts": ["myrmex.plan-review/v1"],
        "observed_conventions": ["immutable artifacts"], "implementation_constraints": ["review-only"],
        "unresolved_decisions": ["Choose destructive migration semantics"] if hidden_decision else [], "protected_dirty_paths": [],
        "excluded_sensitive_paths": [".env"], "evidence": ["planner receipt"],
    }
    constraints = {
        "allowed_paths": ["scripts/"], "forbidden_paths": [".env"],
        "required_invariants": ["critic identity differs"], "required_sections": ["work_units"],
    }
    planning_request_id = f"req-plan-{suffix}"
    planner_task_id = f"task-planner-{suffix}"
    prepared = gateway.prepare_planner_task(
        root, campaign_id, 1, planning_request_id, planner_task_id, run_id,
        objective_id, "1" * 40, snapshot["snapshot_record_id"], repository_context,
        constraints, "2026-08-10T00:00:00+00:00",
    )
    request = prepared["request"]
    work_unit = {
        "id": "WU-P1-009", "objective": "Implement independent plan review",
        "non_goals": ["activation"], "dependencies": [],
        "scope": {"allowed_paths": ["scripts/"], "forbidden_paths": [".env"]},
        "acceptance_criteria": ["critic identity differs from planner"],
        "verification": {"commands": ["python3 tests/test-plan-critic.py"], "manual_checks": [], "discover_when_missing": False},
        "risk_class": "bounded", "required_route": "direct-only", "human_gates": [],
        "required_evidence": ["critic receipt"], "terminal_gate": "G2-PLAN-CRITIC",
    }
    work_units = [work_unit]
    edges = []
    if cycle:
        work_unit["dependencies"] = ["WU-P1-010"]
        second_work_unit = copy.deepcopy(work_unit)
        second_work_unit["id"] = "WU-P1-010"
        second_work_unit["dependencies"] = ["WU-P1-009"]
        work_units.append(second_work_unit)
        edges = [["WU-P1-010", "WU-P1-009"], ["WU-P1-009", "WU-P1-010"]]
    record = {
        "schema": plan_store.PLAN_SCHEMA, "record_id": "", "plan_revision_id": "",
        "campaign_id": campaign_id, "objective_id": objective_id,
        "planning_request_id": planning_request_id, "base_sha": "1" * 40,
        "parent_revision": None, "input_digests": request["input_digests"],
        "assumptions": [], "work_units": work_units, "edges": edges,
        "lifecycle_status": "proposed", "previous_record_id": None,
        "plan_digest": "", "record_digest": "", "created_at": "2026-08-10T00:00:00+00:00",
    }
    record["plan_digest"] = plan_store.compute_plan_digest(record)
    record["plan_revision_id"] = plan_store.derive_plan_revision_id(record["plan_digest"])
    record["record_digest"] = plan_store.compute_record_digest(record)
    record["record_id"] = plan_store.derive_record_id(record["record_digest"])
    result = {
        "schema": planner.RESULT_SCHEMA, "request_id": planning_request_id,
        "run_id": run_id, "campaign_id": campaign_id, "objective_id": objective_id,
        "base_sha": "1" * 40, "response_type": "plan", "plan_revision": record,
        "analysis": {"facts": ["exact plan input"], "assumptions": [], "uncertainties": []},
        "coverage_matrix": [{"backlog_item_id": item["backlog_item_id"], "work_unit_ids": ["WU-P1-009"]}],
        "clarification": None, "completion_evidence": [], "authority": dict(planner.AUTHORITY),
        "result_digest": "", "created_at": "2026-08-10T00:00:00+00:00",
    }
    result["result_digest"] = planner._sha({key: value for key, value in result.items() if key != "result_digest"})
    gateway.record_planner_task_result(root, campaign_id, 1, planning_request_id, planner_task_id, result)
    return root, campaign_id, planning_request_id, planner_task_id, record


def make_review(intent, verdict="PASS", checks=None, defects=None):
    review = {
        "schema": critic.REVIEW_SCHEMA, "review_request_id": intent["review_request_id"],
        "run_id": intent["run_id"], "campaign_id": intent["campaign_id"],
        "objective_id": intent["objective_id"], "critic_task_id": intent["critic_task_id"],
        "planner_task_id": intent["planner_task_id"], "plan_revision_id": intent["plan_revision_id"],
        "plan_record_id": intent["plan_record_id"], "plan_digest": intent["plan_digest"],
        "plan_record_digest": intent["plan_record_digest"], "verdict": verdict,
        "checks": checks or {name: "PASS" for name in critic.CHECK_NAMES},
        "defects": defects or [], "summary": "Independent plan review completed",
        "authority": dict(critic.AUTHORITY), "review_digest": "",
        "created_at": "2026-08-10T01:00:00+00:00",
    }
    review["review_digest"] = critic._sha({key: value for key, value in review.items() if key != "review_digest"})
    return review


root, cid, planning_request_id, planner_task_id, proposed = fixture()
review_request_id = "review-main-001"
critic_task_id = "task-critic-main"
prepared = critic.prepare_critic_task(
    root, cid, 1, review_request_id, critic_task_id, planning_request_id,
    planner_task_id, proposed["record_id"], "2026-08-10T01:00:00+00:00",
)
check(prepared["task_intent"]["critic_task_id"] != prepared["task_intent"]["planner_task_id"], "critic/planner task identity separated")
check(prepared["task_intent"]["authority"] == critic.AUTHORITY, "critic intent is review-only")
check(all(value == "PASS" for value in prepared["context"]["deterministic_preflight"]["checks"].values()), "valid plan deterministic checks pass")
review = make_review(prepared["task_intent"])
seen: list[str] = []


def executor(*, agent, task_id, prompt):
    seen.append(task_id)
    check(agent == "myrmex-plan-critic", "dedicated critic agent selected")
    check("myrmex.plan-review/v1" in prompt, "critic prompt requests exact schema")
    return {"task_id": task_id, "review": review}


completed = critic.execute_critic_task(
    root, cid, 1, review_request_id, critic_task_id, planning_request_id,
    planner_task_id, proposed["record_id"], executor, "2026-08-10T01:00:00+00:00",
)
check(completed["verdict"] == "PASS" and completed["review_digest"] == review["review_digest"], "PASS review receipt binds exact digest")
head = plan_store.get_plan_head(root, cid, proposed["plan_revision_id"])
check(head["lifecycle_status"] == "reviewed" and head["previous_record_id"] == proposed["record_id"], "PASS advances only to reviewed")
replayed = critic.execute_critic_task(
    root, cid, 1, review_request_id, critic_task_id, planning_request_id,
    planner_task_id, proposed["record_id"], lambda **_: (_ for _ in ()).throw(AssertionError("executor repeated")),
    "2026-08-10T01:00:00+00:00",
)
check(replayed["status"] == "reused" and len(seen) == 1, "confirmed critic replay does not repeat executor")

raises(critic.PlanCriticTaskInvalid, lambda: critic.prepare_critic_task(
    root, cid, 1, "review-same-task", planner_task_id, planning_request_id,
    planner_task_id, proposed["record_id"],
), "same planner/critic task rejected")
wrong_identity = make_review(prepared["task_intent"]); wrong_identity["critic_task_id"] = planner_task_id
wrong_identity["review_digest"] = critic._sha({key: value for key, value in wrong_identity.items() if key != "review_digest"})
raises(critic.PlanReviewInvalid, lambda: critic.validate_review(prepared["task_intent"], prepared["context"], wrong_identity), "planner identity cannot be critic")

# Deterministic adversarial plan checks cannot be contradicted by a critic PASS.
missing_criteria_context = copy.deepcopy(prepared["context"])
missing_criteria_context["plan_revision"]["work_units"][0]["acceptance_criteria"] = []
missing_criteria_context["deterministic_preflight"] = critic.compute_preflight(missing_criteria_context)
check(missing_criteria_context["deterministic_preflight"]["checks"]["verification"] == "FAIL", "missing criteria detected")
raises(critic.PlanReviewInvalid, lambda: critic.validate_review(prepared["task_intent"], missing_criteria_context, review), "critic cannot PASS missing criteria")

hidden_decision_context = copy.deepcopy(prepared["context"])
hidden_decision_context["repository_context"]["unresolved_decisions"] = ["Choose destructive migration semantics"]
hidden_decision_context["deterministic_preflight"] = critic.compute_preflight(hidden_decision_context)
check(hidden_decision_context["deterministic_preflight"]["checks"]["human_gates"] == "BLOCKED", "hidden product decision detected")
raises(critic.PlanReviewInvalid, lambda: critic.validate_review(prepared["task_intent"], hidden_decision_context, review), "hidden product decision cannot PASS")

invalid_dag_context = copy.deepcopy(prepared["context"])
invalid_dag_context["plan_revision"]["work_units"][0]["dependencies"] = ["WU-P1-009"]
invalid_dag_context["deterministic_preflight"] = critic.compute_preflight(invalid_dag_context)
check(invalid_dag_context["deterministic_preflight"]["invalid_dag"], "invalid DAG detected")
raises(critic.PlanReviewInvalid, lambda: critic.validate_review(prepared["task_intent"], invalid_dag_context, review), "invalid DAG cannot PASS")

# Persisted, digest-valid two-node cycle reaches the critic but deterministically
# requires INVALID; this exercises the real artifact/task boundary.
cycle_root, cycle_cid, cycle_request, cycle_planner_task, cycle_proposed = fixture("cycle", cycle=True)
cycle_prepared = critic.prepare_critic_task(
    cycle_root, cycle_cid, 1, "review-cycle-001", "task-critic-cycle",
    cycle_request, cycle_planner_task, cycle_proposed["record_id"],
    "2026-08-10T01:00:00+00:00",
)
check(cycle_prepared["context"]["deterministic_preflight"]["invalid_dag"], "persisted cyclic plan preflight is INVALID")
cycle_pass = make_review(cycle_prepared["task_intent"])
raises(critic.PlanReviewInvalid, lambda: critic.record_review(
    cycle_root, cycle_cid, 1, "review-cycle-001", "task-critic-cycle", cycle_pass,
), "persisted cyclic plan cannot receive PASS")

# An unresolved repository product decision requires a concrete BLOCKED review
# and does not append a reviewed lifecycle record.
hidden_root, hidden_cid, hidden_request, hidden_planner_task, hidden_proposed = fixture("hidden", hidden_decision=True)
hidden_prepared = critic.prepare_critic_task(
    hidden_root, hidden_cid, 1, "review-hidden-001", "task-critic-hidden",
    hidden_request, hidden_planner_task, hidden_proposed["record_id"],
    "2026-08-10T01:00:00+00:00",
)
check(hidden_prepared["context"]["deterministic_preflight"]["hidden_product_decision"], "persisted hidden product decision blocks review")
blocked_checks = {name: "PASS" for name in critic.CHECK_NAMES}; blocked_checks["human_gates"] = "BLOCKED"
blocked_defect = [{
    "defect_id": "PRD-001", "category": "human_gates", "severity": "BLOCKING",
    "message": "Product decision requires an explicit human gate", "work_unit_ids": [],
    "evidence_references": ["repository_context.unresolved_decisions"],
}]
blocked_review = make_review(hidden_prepared["task_intent"], "BLOCKED", blocked_checks, blocked_defect)
blocked_receipt = critic.record_review(
    hidden_root, hidden_cid, 1, "review-hidden-001", "task-critic-hidden", blocked_review,
)
check(blocked_receipt["verdict"] == "BLOCKED" and blocked_receipt["defect_references"] == ["PRD-001"], "BLOCKED review preserves defect reference")
check(plan_store.get_plan_head(hidden_root, hidden_cid, hidden_proposed["plan_revision_id"])["lifecycle_status"] == "proposed", "BLOCKED review cannot advance lifecycle")

unsupported_context = copy.deepcopy(prepared["context"])
unsupported_context["planning_result"]["analysis"]["uncertainties"] = ["Unknown retention rule"]
unsupported_context["deterministic_preflight"] = critic.compute_preflight(unsupported_context)
check(unsupported_context["deterministic_preflight"]["checks"]["unsupported_assumptions"] == "BLOCKED", "unsupported uncertainty detected")

bad_verdict = make_review(prepared["task_intent"]); bad_verdict["verdict"] = "pass"
bad_verdict["review_digest"] = critic._sha({key: value for key, value in bad_verdict.items() if key != "review_digest"})
raises(critic.PlanReviewInvalid, lambda: critic.validate_review(prepared["task_intent"], prepared["context"], bad_verdict), "verdict enum is exact")
revise_without_defects = make_review(prepared["task_intent"], verdict="REVISE")
raises(critic.PlanReviewInvalid, lambda: critic.validate_review(prepared["task_intent"], prepared["context"], revise_without_defects), "non-PASS requires defect references")

# Crash after review/lifecycle persistence but before critic receipt is recovered
# from immutable review bytes without invoking transport again.
crash_root, crash_cid, crash_planning_request, crash_planner_task, crash_proposed = fixture("crash")
crash_review_request = "review-crash-001"
crash_critic_task = "task-critic-crash"
crash_prepared = critic.prepare_critic_task(
    crash_root, crash_cid, 1, crash_review_request, crash_critic_task,
    crash_planning_request, crash_planner_task, crash_proposed["record_id"],
    "2026-08-10T01:00:00+00:00",
)
crash_review = make_review(crash_prepared["task_intent"])
original_put = critic.intel.put_artifact
injected = {"done": False}


def fault_put(campaign_dir, campaign_id, revision, kind, artifact_id, payload):
    if artifact_id == critic._artifact_id(crash_review_request, "receipt") and not injected["done"]:
        injected["done"] = True
        raise RuntimeError("injected receipt interruption")
    return original_put(campaign_dir, campaign_id, revision, kind, artifact_id, payload)


critic.intel.put_artifact = fault_put
raises(RuntimeError, lambda: critic.record_review(
    crash_root, crash_cid, 1, crash_review_request, crash_critic_task, crash_review,
), "receipt interruption occurs after durable review")
critic.intel.put_artifact = original_put
recovered = critic.execute_critic_task(
    crash_root, crash_cid, 1, crash_review_request, crash_critic_task,
    crash_planning_request, crash_planner_task, crash_proposed["record_id"],
    lambda **_: (_ for _ in ()).throw(AssertionError("executor must not run")),
    "2026-08-10T01:00:00+00:00",
)
check(recovered["verdict"] == "PASS" and recovered["reviewed_plan_record_id"], "review-before-receipt crash recovered")

agent_text = (ROOT / "agents/myrmex-plan-critic.md").read_text(encoding="utf-8")
for invariant in ("edit: deny", "task: deny", '"mem_*": deny', '"git commit*": deny', '"git push*": deny', "myrmex.plan-review/v1"):
    check(invariant in agent_text, f"critic permission/output invariant: {invariant}")
source = pathlib.Path(critic.__file__).read_text(encoding="utf-8")
for banned in ("import subprocess", "import requests", "import socket", "git commit", "git push"):
    check(banned not in source, f"critic orchestrator has no capability: {banned}")

if failures:
    raise SystemExit("plan critic failures: " + "; ".join(failures))
print("plan critic: independent identity, adversarial checks, review lifecycle, and recovery PASS")
