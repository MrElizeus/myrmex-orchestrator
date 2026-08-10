#!/usr/bin/env python3
"""P1-008 planner-agent gateway, task identity, replay, and authority tests."""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import myrmex_backlog_normalizer as backlog  # noqa: E402
import myrmex_campaign_intelligence as intel  # noqa: E402
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


def fixture():
    root = pathlib.Path(tempfile.mkdtemp(prefix="myrmex-planner-gateway-"))
    campaign_id = "camp-p1008-gateway"
    identity = {"kind": "local-roadmap", "canonical_id": "roadmap.md"}
    entity = "srcitem_" + "a" * 64
    item = {
        "schema": backlog.BACKLOG_ITEM_SCHEMA, "backlog_item_id": "", "item_digest": "",
        "source_adapter": "local-markdown-roadmap/v1", "source_identity": identity,
        "source_entity_type": "local-item", "source_entity_id": entity,
        "title": "implement gateway", "priority": None, "state": None,
        "dependency_hints": [], "constraints": [], "context_constraints": [],
        "labels": [], "group_ref": None,
    }
    item["backlog_item_id"] = backlog.compute_backlog_item_id(item["source_adapter"], identity, entity)
    item["item_digest"] = backlog.compute_item_digest(item)
    item_id = f"normalized-backlog/item/{item['backlog_item_id']}/{item['item_digest']}"
    intel.put_artifact(root, campaign_id, 1, "backlog", item_id, item)
    observation_digest = "b" * 64
    source = {
        "operation_id": "importop-" + "b" * 24,
        "observation_id": "srcobs_" + observation_digest,
        "observation_digest": observation_digest,
        "request_digest": "c" * 64, "content_digest": "d" * 64,
        "outcome": "changed", "adapter": "local-markdown-roadmap/v1",
        "source_identity": identity,
    }
    snapshot = {
        "schema": backlog.BACKLOG_SNAPSHOT_SCHEMA, "snapshot_record_id": "",
        "snapshot_record_digest": "", "snapshot_digest": "", "source_count": 1,
        "sources": [source], "item_count": 1,
        "items": [{"backlog_item_id": item["backlog_item_id"], "item_digest": item["item_digest"], "artifact_id": item_id}],
    }
    snapshot["snapshot_digest"] = backlog.compute_snapshot_digest_from_snapshot(snapshot)
    snapshot["snapshot_record_digest"] = backlog.compute_snapshot_record_digest(snapshot)
    snapshot["snapshot_record_id"] = "blsnaprec_" + snapshot["snapshot_record_digest"]
    intel.put_artifact(root, campaign_id, 1, "backlog", "normalized-backlog/snapshot/" + snapshot["snapshot_record_id"], snapshot)
    repository_context = {
        "schema": "myrmex.repository-context/v1", "run_id": "run-p1008-gateway",
        "objective_id": "obj-p1008-gateway", "repository_root": "/repo",
        "branch": "feat/p1", "base_sha": "1" * 40,
        "git_status": ["clean"], "objective": "Produce a bounded plan",
        "relevant_files": ["scripts/myrmex_planner.py"],
        "relevant_symbols": ["create_planning_request"],
        "architecture": ["planning-only sidecar"], "current_behavior": ["planner core exists"],
        "tests": ["tests/test-planner-gateway.py"],
        "data_contracts": ["myrmex.planning-request/v1", "myrmex.planning-result/v1"],
        "observed_conventions": ["immutable artifacts"],
        "implementation_constraints": ["no repository writes"],
        "unresolved_decisions": [], "protected_dirty_paths": ["protected.txt"],
        "excluded_sensitive_paths": [".env"], "evidence": ["base SHA verified"],
    }
    constraints = {
        "allowed_paths": ["scripts/"], "forbidden_paths": [".env"],
        "required_invariants": ["planning-only"], "required_sections": ["work_units"],
    }
    return root, campaign_id, snapshot, repository_context, constraints


def plan_result(request, backlog_item_id):
    wu = {
        "id": "WU-P1-008", "objective": "Bounded gateway", "non_goals": ["activation"],
        "dependencies": [], "scope": {"allowed_paths": ["scripts/"], "forbidden_paths": [".env"]},
        "acceptance_criteria": ["task identity is durable"],
        "verification": {"commands": ["python3 tests/test-planner-gateway.py"], "manual_checks": [], "discover_when_missing": False},
        "risk_class": "bounded", "required_route": "direct-only", "human_gates": [],
        "required_evidence": ["planner task receipt"], "terminal_gate": "G2-PLANNER",
    }
    record = {
        "schema": plan_store.PLAN_SCHEMA, "record_id": "", "plan_revision_id": "",
        "campaign_id": request["campaign_id"], "objective_id": request["objective_id"],
        "planning_request_id": request["request_id"], "base_sha": request["base_sha"],
        "parent_revision": None, "input_digests": request["input_digests"],
        "assumptions": [], "work_units": [wu], "edges": [],
        "lifecycle_status": "proposed", "previous_record_id": None,
        "plan_digest": "", "record_digest": "", "created_at": "2026-08-10T00:00:00+00:00",
    }
    record["plan_digest"] = plan_store.compute_plan_digest(record)
    record["plan_revision_id"] = plan_store.derive_plan_revision_id(record["plan_digest"])
    record["record_digest"] = plan_store.compute_record_digest(record)
    record["record_id"] = plan_store.derive_record_id(record["record_digest"])
    result = {
        "schema": planner.RESULT_SCHEMA, "request_id": request["request_id"],
        "run_id": request["run_id"], "campaign_id": request["campaign_id"],
        "objective_id": request["objective_id"], "base_sha": request["base_sha"],
        "response_type": "plan", "plan_revision": record,
        "analysis": {"facts": ["backlog and repository digests are exact"], "assumptions": [], "uncertainties": []},
        "coverage_matrix": [{"backlog_item_id": backlog_item_id, "work_unit_ids": ["WU-P1-008"]}],
        "clarification": None, "completion_evidence": [], "authority": dict(planner.AUTHORITY),
        "result_digest": "", "created_at": "2026-08-10T00:00:00+00:00",
    }
    result["result_digest"] = planner._sha({key: value for key, value in result.items() if key != "result_digest"})
    return result


root, cid, snapshot, repository_context, constraints = fixture()
prepared = gateway.prepare_planner_task(
    root, cid, 1, "req-gateway", "task-gateway-001", "run-p1008-gateway",
    "obj-p1008-gateway", "1" * 40, snapshot["snapshot_record_id"],
    repository_context, constraints,
)
check([entry["kind"] for entry in prepared["request"]["input_digests"]] == ["normalized-backlog", "repository-context"], "request binds both exact inputs")
check(prepared["context"]["repository_context"] == repository_context, "prompt context includes exact repository snapshot")
check(prepared["task_intent"]["task_id"] == "task-gateway-001", "task identity durable before result")
check(prepared["task_intent"]["authority"] == gateway.AUTHORITY, "task intent has planning-only authority")
check(_intent := intel.get_artifact(root, cid, prepared["task_intent_artifact_id"])["artifact"]["payload"], "task intent artifact readable")
check(_intent == prepared["task_intent"], "task intent artifact exact")

result = plan_result(prepared["request"], snapshot["items"][0]["backlog_item_id"])
seen_task_ids: list[str] = []


def executor(*, agent, task_id, prompt):
    seen_task_ids.append(task_id)
    check(agent == "myrmex-planner", "gateway selects dedicated planner agent")
    check("myrmex.planning-result/v1" in prompt, "prompt requests exact result contract")
    return {"task_id": task_id, "result": result}


executed = gateway.execute_planner_task(
    root, cid, 1, "req-gateway", "task-gateway-001", "run-p1008-gateway",
    "obj-p1008-gateway", "1" * 40, snapshot["snapshot_record_id"],
    repository_context, constraints, executor,
)
check(executed["receipt"]["task_id"] == "task-gateway-001", "receipt binds exact task ID")
check(executed["receipt"]["result_digest"] == result["result_digest"], "receipt binds result digest")
replayed = gateway.execute_planner_task(
    root, cid, 1, "req-gateway", "task-gateway-001", "run-p1008-gateway",
    "obj-p1008-gateway", "1" * 40, snapshot["snapshot_record_id"],
    repository_context, constraints, executor,
)
check(replayed["receipt"]["status"] == "reused", "task/result replay reuses immutable receipt")
check(set(seen_task_ids) == {"task-gateway-001"}, "replay never invents a second task identity")
check(len(seen_task_ids) == 1, "confirmed task replay does not invoke executor again")

raises(gateway.PlannerTaskConflict, lambda: gateway.prepare_planner_task(
    root, cid, 1, "req-gateway", "task-gateway-other", "run-p1008-gateway",
    "obj-p1008-gateway", "1" * 40, snapshot["snapshot_record_id"], repository_context, constraints,
), "same request cannot change task identity")
raises(gateway.PlannerTaskInvalid, lambda: gateway.prepare_planner_task(
    root, cid, 1, "same-id", "same-id", "run-p1008-gateway", "obj-p1008-gateway",
    "1" * 40, snapshot["snapshot_record_id"], repository_context, constraints,
), "task identity must differ from request identity")

# Crash after intent persistence: retry uses the exact same task identity.
crash_prepared = gateway.prepare_planner_task(
    root, cid, 1, "req-crash", "task-crash-001", "run-p1008-gateway",
    "obj-p1008-gateway", "1" * 40, snapshot["snapshot_record_id"], repository_context, constraints,
)
crash_result = plan_result(crash_prepared["request"], snapshot["items"][0]["backlog_item_id"])
raises(RuntimeError, lambda: gateway.execute_planner_task(
    root, cid, 1, "req-crash", "task-crash-001", "run-p1008-gateway",
    "obj-p1008-gateway", "1" * 40, snapshot["snapshot_record_id"], repository_context,
    constraints, lambda **_: (_ for _ in ()).throw(RuntimeError("injected transport interruption")),
), "transport interruption propagates after durable intent")
recovered = gateway.execute_planner_task(
    root, cid, 1, "req-crash", "task-crash-001", "run-p1008-gateway",
    "obj-p1008-gateway", "1" * 40, snapshot["snapshot_record_id"], repository_context,
    constraints, lambda **kwargs: {"task_id": kwargs["task_id"], "result": crash_result},
)
check(recovered["receipt"]["task_id"] == "task-crash-001", "crash recovery preserves task identity")

# Crash after immutable planning response but before task receipt: recover the
# receipt from durable result bytes without reinvoking the task transport.
response_prepared = gateway.prepare_planner_task(
    root, cid, 1, "req-response-crash", "task-response-crash-001", "run-p1008-gateway",
    "obj-p1008-gateway", "1" * 40, snapshot["snapshot_record_id"], repository_context, constraints,
)
response_result = plan_result(response_prepared["request"], snapshot["items"][0]["backlog_item_id"])
planner.record_planning_result(root, cid, 1, "req-response-crash", response_result)
response_recovered = gateway.execute_planner_task(
    root, cid, 1, "req-response-crash", "task-response-crash-001", "run-p1008-gateway",
    "obj-p1008-gateway", "1" * 40, snapshot["snapshot_record_id"], repository_context,
    constraints, lambda **_: (_ for _ in ()).throw(AssertionError("executor must not run")),
)
check(response_recovered["receipt"]["task_id"] == "task-response-crash-001", "response-before-receipt recovery preserves task identity")

# Missing or invented backlog coverage is INVALID before any task receipt.
coverage_prepared = gateway.prepare_planner_task(
    root, cid, 1, "req-missing-coverage", "task-missing-coverage-001", "run-p1008-gateway",
    "obj-p1008-gateway", "1" * 40, snapshot["snapshot_record_id"], repository_context, constraints,
)
missing_coverage_result = plan_result(coverage_prepared["request"], snapshot["items"][0]["backlog_item_id"])
missing_coverage_result["coverage_matrix"] = []
missing_coverage_result["result_digest"] = planner._sha({key: value for key, value in missing_coverage_result.items() if key != "result_digest"})
raises(planner.PlanningResultInvalid, lambda: gateway.record_planner_task_result(
    root, cid, 1, "req-missing-coverage", "task-missing-coverage-001", missing_coverage_result,
), "missing backlog coverage is invalid")
invented_coverage_result = plan_result(coverage_prepared["request"], "backlog_" + "f" * 64)
raises(planner.PlanningResultInvalid, lambda: gateway.record_planner_task_result(
    root, cid, 1, "req-missing-coverage", "task-missing-coverage-001", invented_coverage_result,
), "invented backlog coverage is invalid")

bad_context = dict(repository_context); bad_context["base_sha"] = "2" * 40
raises(planner.PlanningInputInvalid, lambda: gateway.prepare_planner_task(
    root, cid, 1, "req-bad-context", "task-bad-context", "run-p1008-gateway",
    "obj-p1008-gateway", "1" * 40, snapshot["snapshot_record_id"], bad_context, constraints,
), "stale repository context rejected before task intent")

agent_text = (ROOT / "agents" / "myrmex-planner.md").read_text(encoding="utf-8")
for invariant in ("edit: deny", "task: deny", '"mem_*": deny', '"git commit*": deny', '"git push*": deny', "myrmex.planning-result/v1"):
    check(invariant in agent_text, f"planner agent permission/output invariant: {invariant}")
gateway_source = pathlib.Path(gateway.__file__).read_text(encoding="utf-8")
for banned in ("import subprocess", "import requests", "import socket", "git commit", "git push"):
    check(banned not in gateway_source, f"gateway has no capability: {banned}")

if failures:
    raise SystemExit("planner gateway failures: " + "; ".join(failures))
print("planner gateway: task identity, context binding, replay, recovery, and authority PASS")
