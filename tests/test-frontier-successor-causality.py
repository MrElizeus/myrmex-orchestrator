#!/usr/bin/env python3
"""Tests that a Frontier successor must appear LATER in pending_operations than the predecessor.

A confirmed operation created BEFORE a failed one cannot close/supersede it.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "bin" / "myrmex-state"


def run(*args: str, env: dict[str, str], ok: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run([str(STATE), *args], capture_output=True, text=True, env=env, timeout=30)
    if ok and result.returncode != 0:
        raise AssertionError(f"command failed: {args}\nstdout={result.stdout}\nstderr={result.stderr}")
    if not ok and result.returncode == 0:
        raise AssertionError(f"command unexpectedly succeeded: {args}\nstdout={result.stdout}")
    return result


def payload(result: subprocess.CompletedProcess[str]) -> dict:
    return json.loads(result.stdout)


def find_op(state: dict, operation_id: str) -> dict:
    for op in state.get("pending_operations", []):
        if op.get("operation_id") == operation_id:
            return op
    raise AssertionError(f"operation not found: {operation_id}")


NO_EFFECT = {
    "browser_tab_opened": False,
    "outbound_request_observed": False,
    "request_sent": False,
}


def start_frontier(env: dict, run_id: str, request_id: str, task_id: str, revision: int) -> tuple[str, dict]:
    """Start a frontier exchange; returns (operation_id, new_state)."""
    state = payload(run(
        "frontier", run_id, "start",
        "--request-id", request_id,
        "--task-id", task_id,
        "--intent-json", '{"purpose":"plan"}',
        "--expect-revision", str(revision),
        env=env,
    ))
    return state["pending_operations"][-1]["operation_id"], state


def confirm_frontier(env: dict, run_id: str, op_id: str, request_id: str, message_id: str, revision: int) -> dict:
    """Record a successful ACCEPT result for a frontier exchange."""
    return payload(run(
        "frontier", run_id, "result",
        "--operation-id", op_id,
        "--request-id", request_id,
        "--message-id", message_id,
        "--transport-status", "success",
        "--frontier-decision", "ACCEPT",
        "--response-type", "plan",
        "--plan-json", '{"work_unit_id":"WU-01"}',
        "--effect-json", f'{{"request_id":"{request_id}","message_id":"{message_id}"}}',
        "--receipt-json", f'{{"request_id":"{request_id}","message_id":"{message_id}"}}',
        "--expect-revision", str(revision),
        env=env,
    ))


def fail_frontier(env: dict, run_id: str, op_id: str, request_id: str, revision: int) -> dict:
    """Record a transport_error with pre-effect-absence proof so supersession needs no extra evidence."""
    import json as _json
    return payload(run(
        "frontier", run_id, "result",
        "--operation-id", op_id,
        "--request-id", request_id,
        "--transport-status", "transport_error",
        "--effect-stage", "none",
        "--pre-effect-absence-proven",
        "--effect-json", _json.dumps(NO_EFFECT),
        "--receipt-json", _json.dumps(NO_EFFECT),
        "--expect-revision", str(revision),
        env=env,
    ))


def make_run(env: dict, run_id: str, repo: str) -> dict:
    """Create a frontier-gated run and return its initial state."""
    run(
        "init", "--run-id", run_id, "--objective", run_id,
        "--repository-root", repo, "--mode", "autonomous", "--scope", "narrow",
        "--execution-policy", "frontier-gated", "--execution-authority", "prompt",
        "--execution-request-id", f"req-init-{run_id}",
        env=env,
    )
    return payload(run("show", run_id, env=env))


with tempfile.TemporaryDirectory(prefix="myrmex-successor-causality-") as td:
    env = dict(os.environ, MYRMEX_STATE_HOME=str(Path(td) / "state"), PYTHONDONTWRITEBYTECODE="1")
    repo = str(Path(td) / "repo")
    Path(repo).mkdir()

    # ── Case 1: operation supersede path ────────────────────────────────────
    state = make_run(env, "causality-supersede", repo)

    # op_a: confirmed, position 0
    op_a_id, state = start_frontier(env, "causality-supersede", "req-a", "task-a", state["revision"])
    state = confirm_frontier(env, "causality-supersede", op_a_id, "req-a", "turn-a", state["revision"])
    assert find_op(state, op_a_id)["status"] == "confirmed"

    # op_b: failed, position 1
    op_b_id, state = start_frontier(env, "causality-supersede", "req-b", "task-b", state["revision"])
    state = fail_frontier(env, "causality-supersede", op_b_id, "req-b", state["revision"])
    assert find_op(state, op_b_id)["status"] == "failed"

    ops = state["pending_operations"]
    pos_a = next(i for i, o in enumerate(ops) if o["operation_id"] == op_a_id)
    pos_b = next(i for i, o in enumerate(ops) if o["operation_id"] == op_b_id)
    assert pos_a < pos_b, f"op_a must be before op_b: positions {pos_a} vs {pos_b}"

    # Using op_a (earlier, position 0) to supersede op_b (later, position 1) → MUST FAIL
    blocked = run(
        "operation", "causality-supersede", "supersede",
        "--operation-id", op_b_id,
        "--successor-operation-id", op_a_id,
        "--reason", "PRE_EFFECT_FAILURE",
        "--pre-effect-absence-proven",
        "--expect-revision", str(state["revision"]),
        env=env, ok=False,
    )
    assert "OPERATION_SUCCESSOR_NOT_LATER" in blocked.stderr, (
        f"Expected OPERATION_SUCCESSOR_NOT_LATER, got: {blocked.stderr}"
    )

    # Revision must not have changed; op_b still failed
    after_blocked = payload(run("show", "causality-supersede", env=env))
    assert after_blocked["revision"] == state["revision"], (
        f"Revision must not change on blocked supersede: {after_blocked['revision']} vs {state['revision']}"
    )
    assert find_op(after_blocked, op_b_id)["status"] == "failed"
    assert find_op(after_blocked, op_a_id)["status"] == "confirmed"

    # op_c: confirmed, position 2 — CAN supersede op_b
    op_c_id, state = start_frontier(env, "causality-supersede", "req-c", "task-c", after_blocked["revision"])
    state = confirm_frontier(env, "causality-supersede", op_c_id, "req-c", "turn-c", state["revision"])

    superseded_state = payload(run(
        "operation", "causality-supersede", "supersede",
        "--operation-id", op_b_id,
        "--successor-operation-id", op_c_id,
        "--reason", "PRE_EFFECT_FAILURE",
        "--pre-effect-absence-proven",
        "--expect-revision", str(state["revision"]),
        env=env,
    ))
    assert find_op(superseded_state, op_b_id)["status"] == "superseded"
    assert find_op(superseded_state, op_b_id)["successor_operation_id"] == op_c_id

    # Exact replay is idempotent — must not bump revision
    replay_state = payload(run(
        "operation", "causality-supersede", "supersede",
        "--operation-id", op_b_id,
        "--successor-operation-id", op_c_id,
        "--reason", "PRE_EFFECT_FAILURE",
        "--pre-effect-absence-proven",
        "--expect-revision", str(superseded_state["revision"]),
        env=env,
    ))
    assert replay_state["revision"] == superseded_state["revision"], (
        f"Replay must not increment revision: {replay_state['revision']} vs {superseded_state['revision']}"
    )

    # ── Case 2: recovery resolve-frontier path ──────────────────────────────
    rec_state = make_run(env, "causality-recovery", repo)

    # op_x: confirmed, position 0
    op_x_id, rec_state = start_frontier(env, "causality-recovery", "req-x", "task-x", rec_state["revision"])
    rec_state = confirm_frontier(env, "causality-recovery", op_x_id, "req-x", "turn-x", rec_state["revision"])

    # op_y: failed, position 1
    op_y_id, rec_state = start_frontier(env, "causality-recovery", "req-y", "task-y", rec_state["revision"])
    rec_state = fail_frontier(env, "causality-recovery", op_y_id, "req-y", rec_state["revision"])

    # Using op_x (earlier) as successor for op_y via recovery → MUST FAIL
    blocked_recovery = run(
        "recovery", "causality-recovery", "resolve-frontier",
        "--operation-id", op_y_id,
        "--successor-operation-id", op_x_id,
        "--disposition", "supersede",
        "--reason", "PRE_EFFECT_FAILURE",
        "--expect-revision", str(rec_state["revision"]),
        env=env, ok=False,
    )
    assert "OPERATION_SUCCESSOR_NOT_LATER" in blocked_recovery.stderr, (
        f"Expected OPERATION_SUCCESSOR_NOT_LATER in recovery path, got: {blocked_recovery.stderr}"
    )

    # Revision unchanged; op_y still failed
    after_rec_blocked = payload(run("show", "causality-recovery", env=env))
    assert after_rec_blocked["revision"] == rec_state["revision"]
    assert find_op(after_rec_blocked, op_y_id)["status"] == "failed"

    # op_z: confirmed, position 2 — CAN supersede op_y via recovery
    op_z_id, rec_state2 = start_frontier(env, "causality-recovery", "req-z", "task-z", after_rec_blocked["revision"])
    rec_state2 = confirm_frontier(env, "causality-recovery", op_z_id, "req-z", "turn-z", rec_state2["revision"])

    resolved_rec = payload(run(
        "recovery", "causality-recovery", "resolve-frontier",
        "--operation-id", op_y_id,
        "--successor-operation-id", op_z_id,
        "--disposition", "supersede",
        "--reason", "PRE_EFFECT_FAILURE",
        "--expect-revision", str(rec_state2["revision"]),
        env=env,
    ))
    assert find_op(resolved_rec, op_y_id)["status"] == "superseded"
    assert find_op(resolved_rec, op_y_id)["successor_operation_id"] == op_z_id

    # ── Case 3: reconcile auto-selection honours ledger position ─────────────
    recon_state = make_run(env, "causality-recon", repo)

    # op_earlier: confirmed, position 0
    op_earlier_id, recon_state = start_frontier(env, "causality-recon", "req-earlier", "task-earlier", recon_state["revision"])
    recon_state = confirm_frontier(env, "causality-recon", op_earlier_id, "req-earlier", "turn-earlier", recon_state["revision"])
    assert find_op(recon_state, op_earlier_id)["status"] == "confirmed"

    # op_failed: failed, position 1
    op_failed_id, recon_state = start_frontier(env, "causality-recon", "req-failed", "task-failed", recon_state["revision"])
    recon_state = fail_frontier(env, "causality-recon", op_failed_id, "req-failed", recon_state["revision"])
    assert find_op(recon_state, op_failed_id)["status"] == "failed"

    # Verify that having only an earlier confirmed operation does NOT produce FINALIZE_FRONTIER_SUPERSESSION
    recon_action_1 = payload(run("reconcile", "causality-recon", env=env))
    assert recon_action_1["action"] != "FINALIZE_FRONTIER_SUPERSESSION", (
        f"Expected action other than FINALIZE_FRONTIER_SUPERSESSION, got: {recon_action_1['action']}"
    )

    # op_later: confirmed, position 2
    op_later_id, recon_state = start_frontier(env, "causality-recon", "req-later", "task-later", recon_state["revision"])
    recon_state = confirm_frontier(env, "causality-recon", op_later_id, "req-later", "turn-later", recon_state["revision"])
    assert find_op(recon_state, op_later_id)["status"] == "confirmed"

    # Now we have a later confirmed operation. Reconcile MUST return FINALIZE_FRONTIER_SUPERSESSION
    recon_action_2 = payload(run("reconcile", "causality-recon", env=env))
    assert recon_action_2["action"] == "FINALIZE_FRONTIER_SUPERSESSION", (
        f"Expected FINALIZE_FRONTIER_SUPERSESSION, got: {recon_action_2['action']}"
    )

    # It must contain exactly the expected predecessor/successor IDs
    expected_ids = [op_failed_id, op_later_id]
    assert recon_action_2["operation_ids"] == expected_ids, (
        f"Expected operation_ids {expected_ids}, got: {recon_action_2['operation_ids']}"
    )

print("frontier successor causality test: PASS")
