#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "bin" / "myrmex-state"


def run(*args: str, env: dict[str, str], ok: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run([str(STATE), *args], capture_output=True, text=True, env=env, timeout=20)
    if ok and result.returncode != 0:
        raise AssertionError(f"command failed: {args}\nstdout={result.stdout}\nstderr={result.stderr}")
    if not ok and result.returncode == 0:
        raise AssertionError(f"command unexpectedly succeeded: {args}\nstdout={result.stdout}")
    return result


def payload(result: subprocess.CompletedProcess[str]) -> dict:
    return json.loads(result.stdout)


def init(env: dict[str, str], repository: str, run_id: str) -> str:
    return run(
        "init", "--run-id", run_id, "--objective", "first bounded task",
        "--parent-objective", "continuous parent objective", "--repository-root", repository,
        "--mode", "autonomous", "--scope", "continuous", env=env,
    ).stdout.strip()


def finish_work_unit(env: dict[str, str], run_id: str, work_unit_id: str = "WU-one") -> dict:
    run(
        "delegation-preflight", run_id, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "bounded implementation", "--task-id", f"task-{work_unit_id}",
        "--work-unit-id", work_unit_id, "--workspace", env["TEST_REPOSITORY"],
        "--expect-revision", "0", env=env,
    )
    run(
        "delegation", run_id, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "verified implementation", "--task-id", f"task-{work_unit_id}",
        "--work-unit-id", work_unit_id, "--workspace", env["TEST_REPOSITORY"],
        "--status", "success", "--expect-revision", "1", env=env,
    )
    return payload(run(
        "work-unit", run_id, "complete", "--work-unit-id", work_unit_id,
        "--evidence-json", '{"verification":"passed"}', "--expect-revision", "2", env=env,
    ))


def start_gate(env: dict[str, str], run_id: str, revision: int, suffix: str = "one") -> tuple[dict, str]:
    started = payload(run(
        "frontier", run_id, "start", "--request-id", f"gate-request-{suffix}",
        "--task-id", f"gate-task-{suffix}", "--message-id", f"gate-message-{suffix}",
        "--expect-revision", str(revision), env=env,
    ))
    return started, started["pending_operations"][-1]["operation_id"]


def frontier_result(
    env: dict[str, str], run_id: str, operation_id: str, revision: int, response_type: str,
    *, plan: dict | None = None, suffix: str = "one", transport: str = "success",
) -> dict:
    request_id = f"gate-request-{suffix}"
    message_id = f"gate-message-{suffix}"
    evidence: dict = {
        "request_id": request_id, "message_id": message_id,
        "transport_status": transport, "frontier_decision": "ACCEPT",
        "response_type": response_type,
    }
    if plan is not None:
        evidence["proposed_plan"] = plan
    if response_type == "blocking_clarification":
        evidence["clarification"] = {"question": "Which deployment target is authoritative?"}
    encoded = json.dumps(evidence, sort_keys=True)
    return payload(run(
        "frontier", run_id, "result", "--operation-id", operation_id,
        "--request-id", request_id, "--message-id", message_id,
        "--transport-status", transport, "--frontier-decision", "ACCEPT",
        "--response-type", response_type, "--effect-json", encoded,
        "--receipt-json", encoded, "--expect-revision", str(revision), env=env,
    ))


with tempfile.TemporaryDirectory(prefix="myrmex-parent-continuity-") as td:
    repository = str(Path(td) / "repo")
    Path(repository).mkdir()
    env = dict(
        os.environ, MYRMEX_STATE_HOME=str(Path(td) / "state"),
        TEST_REPOSITORY=repository, PYTHONDONTWRITEBYTECODE="1",
    )

    # A completed WU is not a completed continuous parent.  It leaves a typed
    # gate request and never enters dormant state.
    run_id = init(env, repository, "continuity-gate")
    completed = finish_work_unit(env, run_id)
    assert completed["status"] == "active" and completed["phase"] == "parent-gate"
    assert completed["parent_gate"]["status"] == "required"
    assert payload(run("reconcile", run_id, env=env))["action"] == "REQUEST_PARENT_GATE"

    started, operation_id = start_gate(env, run_id, 3)
    plan = {"work_unit_id": "WU-two", "summary": "next exact intent", "tasks": ["implement"]}
    planned = frontier_result(env, run_id, operation_id, 4, "plan", plan=plan)
    assert planned["status"] == "active"
    assert planned["parent_gate"]["response_type"] == "plan"
    assert planned["next_work_unit"]["status"] == "approved"
    assert planned["next_work_unit"]["intent"] == plan
    provenance = planned["next_work_unit"]["provenance"]
    assert provenance["operation_id"] == operation_id
    assert provenance["request_id"] == "gate-request-one"
    assert provenance["message_id"] == "gate-message-one"
    assert payload(run("reconcile", run_id, env=env))["action"] == "BEGIN_NEXT_WORK_UNIT"

    state_path = Path(env["MYRMEX_STATE_HOME"]) / "runs" / run_id / "state.json"
    events_path = state_path.parent / "events.jsonl"
    before_state, before_events = state_path.read_bytes(), events_path.read_bytes()
    duplicate = frontier_result(env, run_id, operation_id, 999, "plan", plan=plan)
    assert duplicate["revision"] == planned["revision"]
    assert state_path.read_bytes() == before_state and events_path.read_bytes() == before_events
    conflicting_plan = {"work_unit_id": "WU-three", "summary": "conflicting intent"}
    conflict = run(
        "frontier", run_id, "result", "--operation-id", operation_id,
        "--request-id", "gate-request-one", "--message-id", "gate-message-one",
        "--transport-status", "success", "--frontier-decision", "ACCEPT",
        "--response-type", "plan", "--effect-json", json.dumps({"proposed_plan": conflicting_plan}),
        "--receipt-json", json.dumps({"proposed_plan": conflicting_plan}),
        "--expect-revision", "999", env=env, ok=False,
    )
    assert "FRONTIER_RESULT_OPERATION_CONFLICT" in conflict.stderr
    assert state_path.read_bytes() == before_state and events_path.read_bytes() == before_events

    # A next WU requires the confirmed plan provenance, and one active WU is
    # allowed.  A different WU cannot be launched concurrently or without it.
    run(
        "delegation-preflight", run_id, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "approved next work", "--task-id", "task-two", "--work-unit-id", "WU-two",
        "--workspace", repository, "--expect-revision", "5", env=env,
    )
    concurrent = run(
        "delegation-preflight", run_id, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "must not overlap", "--task-id", "task-three", "--work-unit-id", "WU-three",
        "--workspace", repository, "--expect-revision", "6", env=env, ok=False,
    )
    assert "BLOCKED_NEXT_WORK_UNIT_PROVENANCE_REQUIRED" in concurrent.stderr or "BLOCKED_CONCURRENT_ACTIVE_WORK_UNIT" in concurrent.stderr
    patch = run(
        "patch", run_id, "--set", "parent_objective=changed", "--expect-revision", "6", env=env, ok=False,
    )
    assert "protected state path" in patch.stderr

    # SUB_OBJECTIVE_COMPLETE is a confirmed typed response, but never the
    # parent terminal gate.  A second parent gate may then explicitly complete.
    sub_run = init(env, repository, "sub-objective-only")
    finish_work_unit(env, sub_run)
    _, sub_operation = start_gate(env, sub_run, 3)
    sub_result = frontier_result(env, sub_run, sub_operation, 4, "sub_objective_complete")
    assert sub_result["status"] == "active"
    rejected = run("complete", sub_run, "--message", "sub is not parent complete", "--expect-revision", "5", env=env, ok=False)
    assert "parent objective has not passed a parent gate" in rejected.stderr
    assert payload(run("reconcile", sub_run, env=env))["action"] == "REQUEST_PARENT_GATE"
    _, parent_operation = start_gate(env, sub_run, 5, suffix="two")
    parent_result = frontier_result(env, sub_run, parent_operation, 6, "parent_objective_complete", suffix="two")
    assert parent_result["parent_gate"]["response_type"] == "parent_objective_complete"
    terminal = payload(run("complete", sub_run, "--message", "parent explicitly complete", "--expect-revision", "7", env=env))
    assert terminal["state"]["status"] == terminal["state"]["phase"] == "dormant"

    # Blocking clarification is typed and resumable; failed required work is
    # still a completion blocker.
    clarification_run = init(env, repository, "clarification")
    finish_work_unit(env, clarification_run)
    _, clarification_operation = start_gate(env, clarification_run, 3)
    blocked = frontier_result(env, clarification_run, clarification_operation, 4, "blocking_clarification")
    assert blocked["status"] == blocked["phase"] == "blocked"
    assert blocked["blocker"] == "BLOCKING_CLARIFICATION"
    assert payload(run("reconcile", clarification_run, env=env))["action"] == "RESUME_BLOCKING_CLARIFICATION"
    resumed = payload(run("resume", clarification_run, "--expect-revision", "5", env=env))
    assert resumed["status"] == "active" and resumed["clarification"]["status"] == "resumed"

    failed_run = init(env, repository, "failed-required")
    failed_operation = payload(run(
        "operation", failed_run, "intent", "--kind", "ci", "--idempotency-key", "ci:required",
        "--intent-json", '{"required":true}', "--expect-revision", "0", env=env,
    ))["pending_operations"][0]["operation_id"]
    run(
        "operation", failed_run, "observe", "--operation-id", failed_operation,
        "--effect-json", '{"job":"required-ci"}', "--expect-revision", "1", env=env,
    )
    run(
        "operation", failed_run, "receipt", "--operation-id", failed_operation,
        "--receipt-json", '{"status":"FAILED"}', "--expect-revision", "2", env=env,
    )
    failed = payload(run(
        "operation", failed_run, "confirm", "--operation-id", failed_operation,
        "--status", "failed", "--reason", "required CI failed", "--expect-revision", "3", env=env,
    ))
    assert failed["status"] == "active"
    denied = run("complete", failed_run, "--message", "must remain blocked", "--expect-revision", "4", env=env, ok=False)
    assert "required operation" in denied.stderr

    # Informational status is pure; explicit pause/resume and cancellation are
    # distinct typed transitions.
    sideband_run = init(env, repository, "side-band")
    run("frontier", sideband_run, "start", "--request-id", "side-request", "--task-id", "side-task", "--message-id", "side-message", "--expect-revision", "0", env=env)
    before = Path(env["MYRMEX_STATE_HOME"]) / "runs" / sideband_run
    before_bytes = (before / "state.json").read_bytes(), (before / "events.jsonl").read_bytes()
    action_before = payload(run("reconcile", sideband_run, env=env))
    assert action_before["action"] == "RECOVER_FRONTIER_EXCHANGE"
    # show/reconcile are the side-band read path used by the active agent.
    run("show", sideband_run, env=env)
    action_after = payload(run("reconcile", sideband_run, env=env))
    assert action_after["action"] == action_before["action"]
    assert ((before / "state.json").read_bytes(), (before / "events.jsonl").read_bytes()) == before_bytes
    paused = payload(run("pause", sideband_run, "--reason", "explicit pause", "--expect-revision", "1", env=env))
    assert paused["blocker"] == "EXPLICIT_PAUSE" and paused["pause"]["action"]["action"] == action_before["action"]
    resumed_sideband = payload(run("resume", sideband_run, "--expect-revision", "2", env=env))
    assert resumed_sideband["status"] == "active"
    cancelled = init(env, repository, "cancelled-parent")
    cancelled_state = payload(run("cancel", cancelled, "--reason", "parent stopped", "--expect-revision", "0", env=env))
    assert cancelled_state["status"] == "cancelled"
    assert cancelled_state["cancellation"]["type"] == "PARENT_OBJECTIVE_CANCELLED"

    # The two shipped schema copies are byte-identical.
    assert (ROOT / "contracts/frontier-state-v2.schema.json").read_bytes() == (
        ROOT / "skills/myrmex-frontier-delegation/assets/schemas/frontier-state.schema.json"
    ).read_bytes()

print("parent objective continuity test: PASS")
