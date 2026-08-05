#!/usr/bin/env python3
from __future__ import annotations

import copy
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


def state(result: subprocess.CompletedProcess[str]) -> dict:
    return json.loads(result.stdout)


def init(env: dict[str, str], repository: str, name: str) -> str:
    return run(
        "init", "--run-id", name, "--objective", name, "--repository-root", repository,
        "--mode", "autonomous", "--scope", "narrow", "--execution-policy", "auto", env=env,
    ).stdout.strip()


def start(env: dict[str, str], run_id: str, request_id: str, task_id: str, message_id: str, revision: int) -> dict:
    return state(run(
        "frontier", run_id, "start", "--request-id", request_id, "--task-id", task_id,
        "--message-id", message_id, "--expect-revision", str(revision), env=env,
    ))


def evidence(request_id: str, message_id: str, decision: str, *, transport: str = "success") -> str:
    return json.dumps({
        "request_id": request_id,
        "message_id": message_id,
        "transport_status": transport,
        "frontier_decision": decision,
    }, sort_keys=True)


def result(
    env: dict[str, str], run_id: str, operation_id: str, request_id: str, message_id: str,
    decision: str | None, transport: str, revision: int, *, task_id: str | None = None,
    effect_json: str | None = None, receipt_json: str | None = None,
) -> dict:
    args = [
        "frontier", run_id, "result", "--operation-id", operation_id,
        "--request-id", request_id, "--message-id", message_id,
        "--transport-status", transport, "--expect-revision", str(revision),
    ]
    if decision is not None:
        args.extend(["--frontier-decision", decision])
    if task_id is not None:
        args.extend(["--task-id", task_id])
    if effect_json is not None:
        args.extend(["--effect-json", effect_json])
    if receipt_json is not None:
        args.extend(["--receipt-json", receipt_json])
    return state(run(*args, env=env))


with tempfile.TemporaryDirectory(prefix="myrmex-frontier-recovery-test-") as td:
    env = dict(os.environ, MYRMEX_STATE_HOME=str(Path(td) / "state"), PYTHONDONTWRITEBYTECODE="1")

    # A successful transport is technically confirmed independently of the
    # substantive Frontier decision. Historical exchanges do not interfere.
    independent = init(env, td, "frontier-independent")
    revisions = 0
    operations: list[tuple[str, str]] = []
    for index, decision in enumerate(("ACCEPT", "REMEDIATE", "BLOCKED"), start=1):
        request_id = f"request-{index}"
        message_id = f"message-{index}"
        started = start(env, independent, request_id, f"task-{index}", message_id, revisions)
        revisions += 1
        operation = started["pending_operations"][-1]
        payload = evidence(request_id, message_id, decision)
        finished = result(
            env, independent, operation["operation_id"], request_id, message_id, decision,
            "success", revisions, task_id=f"task-{index}", effect_json=payload, receipt_json=payload,
        )
        revisions += 1
        recorded = finished["pending_operations"][-1]
        assert recorded["status"] == "confirmed"
        assert recorded["transport_status"] == "success"
        assert recorded["frontier_decision"] == decision
        assert recorded["effect"]["frontier_decision"] == decision
        assert recorded["receipt"]["message_id"] == message_id
        operations.append((operation["operation_id"], decision))
    completed = state(run(
        "complete", independent, "--message", "all decisions are technically confirmed",
        "--expect-revision", str(revisions), env=env,
    ))
    assert completed["state"]["status"] == "dormant"

    # Transport failures, malformed results, and identity mismatches remain
    # genuine terminal failures and block completion.
    failure_cases = (
        ("transport_error", None, "request-failure", "message-failure"),
        ("timeout", None, "request-timeout", "message-timeout"),
        ("malformed", None, "request-malformed", "message-malformed"),
        ("request_mismatch", "ACCEPT", "wrong-request", "message-request"),
        ("response_identity_mismatch", "ACCEPT", "request-response", "wrong-message"),
    )
    for index, (transport, decision, request_id, message_id) in enumerate(failure_cases, start=1):
        failure_run = init(env, td, f"frontier-failure-{index}")
        expected_request = request_id if transport != "request_mismatch" else f"expected-{index}"
        expected_message = message_id if transport != "response_identity_mismatch" else f"expected-message-{index}"
        started = start(env, failure_run, expected_request, f"failure-task-{index}", expected_message, 0)
        operation_id = started["pending_operations"][0]["operation_id"]
        if transport == "request_mismatch":
            failed = result(env, failure_run, operation_id, request_id, expected_message, decision, "success", 1)
        elif transport == "response_identity_mismatch":
            failed = result(env, failure_run, operation_id, expected_request, message_id, decision, "success", 1)
        elif transport == "malformed":
            failed = result(
                env, failure_run, operation_id, request_id, message_id, decision, transport, 1,
                effect_json='{"raw_response":true}', receipt_json='{"raw_response":true}',
            )
        else:
            failed = result(env, failure_run, operation_id, request_id, message_id, decision, transport, 1)
        recorded = failed["pending_operations"][0]
        assert recorded["status"] == "failed"
        assert recorded["transport_status"] == transport
        assert state(run("reconcile", failure_run, env=env))["action"] == "RECOVER_FRONTIER_EXCHANGE"
        blocked = run("complete", failure_run, "--message", "must remain blocked", "--expect-revision", "2", env=env, ok=False)
        assert "required operation" in blocked.stderr

    # Legacy failed REMEDIATE and BLOCKED exchanges are recovered additively.
    recovery_run = init(env, td, "frontier-legacy-recovery")
    first = start(env, recovery_run, "legacy-request-1", "legacy-task-1", "legacy-message-1", 0)
    first_id = first["pending_operations"][0]["operation_id"]
    failed_first = result(
        env, recovery_run, first_id, "legacy-request-1", "legacy-message-1", "REMEDIATE", "transport_error", 1,
    )
    original_first = copy.deepcopy(failed_first["pending_operations"][0])
    valid_recovery_effect = evidence("legacy-request-1", "legacy-message-1", "REMEDIATE")
    state_path = Path(env["MYRMEX_STATE_HOME"]) / "runs" / recovery_run / "state.json"
    before_wrong_scope = state_path.read_bytes()
    run(
        "frontier", recovery_run, "recover", "--operation-id", first_id, "--request-id", "legacy-request-1",
        "--message-id", "legacy-message-1", "--transport-status", "success", "--frontier-decision", "REMEDIATE",
        "--effect-json", valid_recovery_effect, "--expect-revision", "2", env=env, ok=False,
    )
    assert state_path.read_bytes() == before_wrong_scope
    run(
        "frontier", recovery_run, "recover", "--operation-id", first_id, "--request-id", "legacy-request-1",
        "--message-id", "legacy-message-1", "--task-id", "wrong-task", "--transport-status", "success",
        "--frontier-decision", "REMEDIATE", "--effect-json", valid_recovery_effect,
        "--receipt-json", valid_recovery_effect, "--expect-revision", "2", env=env, ok=False,
    )
    assert state_path.read_bytes() == before_wrong_scope
    run(
        "frontier", recovery_run, "recover", "--operation-id", first_id, "--request-id", "wrong-request",
        "--message-id", "legacy-message-1", "--transport-status", "success", "--frontier-decision", "REMEDIATE",
        "--effect-json", valid_recovery_effect, "--receipt-json", valid_recovery_effect,
        "--expect-revision", "2", env=env, ok=False,
    )
    assert state_path.read_bytes() == before_wrong_scope
    run(
        "frontier", recovery_run, "recover", "--operation-id", first_id, "--request-id", "legacy-request-1",
        "--message-id", "legacy-message-1", "--transport-status", "success", "--frontier-decision", "REMEDIATE",
        "--effect-json", valid_recovery_effect, "--receipt-json", valid_recovery_effect,
        "--expect-revision", "1", env=env, ok=False,
    )
    assert state_path.read_bytes() == before_wrong_scope
    recovered_first = state(run(
        "frontier", recovery_run, "recover", "--operation-id", first_id, "--request-id", "legacy-request-1",
        "--message-id", "legacy-message-1", "--transport-status", "success", "--frontier-decision", "REMEDIATE",
        "--effect-json", valid_recovery_effect, "--receipt-json", valid_recovery_effect,
        "--expect-revision", "2", env=env,
    ))
    recovered_operation = recovered_first["pending_operations"][0]
    assert recovered_operation["status"] == "failed"
    assert recovered_operation["effective_status"] == "confirmed"
    assert recovered_operation["effective_outcome"]["frontier_decision"] == "REMEDIATE"
    assert recovered_operation["effect"] == original_first["effect"]
    assert recovered_operation["receipt"] == original_first["receipt"]
    assert len(recovered_operation["recovery_history"]) == 1
    replay_bytes = state_path.read_bytes()
    replayed = state(run(
        "frontier", recovery_run, "recover", "--operation-id", first_id, "--request-id", "legacy-request-1",
        "--message-id", "legacy-message-1", "--transport-status", "success", "--frontier-decision", "REMEDIATE",
        "--effect-json", valid_recovery_effect, "--receipt-json", valid_recovery_effect,
        "--expect-revision", "999", env=env,
    ))
    assert replayed["revision"] == 3 and state_path.read_bytes() == replay_bytes

    second = start(env, recovery_run, "legacy-request-2", "legacy-task-2", "legacy-message-2", 3)
    second_id = second["pending_operations"][-1]["operation_id"]
    result(env, recovery_run, second_id, "legacy-request-2", "legacy-message-2", "BLOCKED", "transport_error", 4)
    second_effect = evidence("legacy-request-2", "legacy-message-2", "BLOCKED")
    recovered_second = state(run(
        "frontier", recovery_run, "recover", "--operation-id", second_id, "--request-id", "legacy-request-2",
        "--message-id", "legacy-message-2", "--transport-status", "success", "--frontier-decision", "BLOCKED",
        "--effect-json", second_effect, "--receipt-json", second_effect, "--expect-revision", "5", env=env,
    ))
    assert recovered_second["pending_operations"][-1]["effective_frontier_decision"] == "BLOCKED"
    run(
        "frontier", recovery_run, "recover", "--operation-id", second_id, "--request-id", "legacy-request-2",
        "--message-id", "legacy-message-2", "--transport-status", "success", "--frontier-decision", "ACCEPT",
        "--effect-json", second_effect, "--receipt-json", second_effect, "--expect-revision", "6", env=env, ok=False,
    )
    final = state(run(
        "complete", recovery_run, "--message", "recovered Frontier exchanges are complete", "--expect-revision", "6", env=env,
    ))
    assert final["state"]["status"] == "dormant"

    # The generic observe/receipt lifecycle cannot confirm an untyped Frontier
    # exchange, and completion remains blocked without mutating state.
    untyped = init(env, td, "frontier-untyped-generic")
    untyped_started = start(env, untyped, "untyped-request", "untyped-task", "untyped-message", 0)
    untyped_id = untyped_started["pending_operations"][0]["operation_id"]
    run(
        "operation", untyped, "observe", "--operation-id", untyped_id,
        "--effect-json", '{"transport":"browser"}', "--expect-revision", "1", env=env,
    )
    run(
        "operation", untyped, "receipt", "--operation-id", untyped_id,
        "--receipt-json", '{"status":"PLAN_RECEIVED"}', "--expect-revision", "2", env=env,
    )
    untyped_path = Path(env["MYRMEX_STATE_HOME"]) / "runs" / untyped / "state.json"
    untyped_before_confirm = untyped_path.read_bytes()
    rejected_confirm = run(
        "operation", untyped, "confirm", "--operation-id", untyped_id, "--status", "confirmed",
        "--reason", "untyped response", "--expect-revision", "2", env=env, ok=False,
    )
    assert "FRONTIER_TYPED_CONFIRMATION_REQUIRED" in rejected_confirm.stderr
    assert untyped_path.read_bytes() == untyped_before_confirm
    rejected_complete = run(
        "complete", untyped, "--message", "untyped must not complete", "--expect-revision", "2",
        env=env, ok=False,
    )
    assert "pending operations" in rejected_complete.stderr
    assert untyped_path.read_bytes() == untyped_before_confirm

    wrong_kind = init(env, td, "frontier-wrong-kind")
    generic = state(run(
        "operation", wrong_kind, "intent", "--kind", "ci", "--idempotency-key", "ci:wrong-kind",
        "--intent-json", '{"required":true}', "--expect-revision", "0", env=env,
    ))
    wrong_kind_path = Path(env["MYRMEX_STATE_HOME"]) / "runs" / wrong_kind / "state.json"
    wrong_kind_bytes = wrong_kind_path.read_bytes()
    run(
        "frontier", wrong_kind, "recover", "--operation-id", generic["pending_operations"][0]["operation_id"],
        "--request-id", "ci-request", "--message-id", "ci-message", "--transport-status", "success",
        "--frontier-decision", "ACCEPT", "--effect-json", "{}", "--receipt-json", "{}",
        "--expect-revision", "1", env=env, ok=False,
    )
    assert wrong_kind_path.read_bytes() == wrong_kind_bytes

    # Generic patch cannot reach operation recovery fields.
    protected = init(env, td, "frontier-patch-protection")
    run(
        "patch", protected, "--json-patch", '{"pending_operations":[]}', "--expect-revision", "0", env=env, ok=False,
    )

print("frontier operation recovery test: PASS")
