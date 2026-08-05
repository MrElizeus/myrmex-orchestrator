#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "bin" / "myrmex-state"
NO_EFFECT = {
    "browser_tab_opened": False,
    "outbound_request_observed": False,
    "request_sent": False,
}


def run(*args: str, env: dict[str, str], ok: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run([str(STATE), *args], capture_output=True, text=True, env=env, timeout=30)
    if ok and result.returncode != 0:
        raise AssertionError(f"command failed: {args}\nstdout={result.stdout}\nstderr={result.stderr}")
    if not ok and result.returncode == 0:
        raise AssertionError(f"command unexpectedly succeeded: {args}\nstdout={result.stdout}")
    return result


def payload(result: subprocess.CompletedProcess[str]) -> dict:
    return json.loads(result.stdout)


with tempfile.TemporaryDirectory(prefix="myrmex-blocked-frontier-") as td:
    env = dict(os.environ, MYRMEX_STATE_HOME=str(Path(td) / "state"), PYTHONDONTWRITEBYTECODE="1")
    repo = str(Path(td) / "repo")
    Path(repo).mkdir()
    run_id = run(
        "init", "--run-id", "blocked-frontier", "--objective", "continue one plan",
        "--repository-root", repo, "--mode", "autonomous", "--scope", "narrow",
        "--execution-policy", "auto", env=env,
    ).stdout.strip()

    first = payload(run(
        "frontier", run_id, "start", "--request-id", "request-02", "--task-id", "task-plan-02",
        "--intent-json", '{"purpose":"planning","request_payload":"same-plan"}',
        "--expect-revision", "0", env=env,
    ))
    predecessor_id = first["pending_operations"][0]["operation_id"]
    failed = payload(run(
        "frontier", run_id, "result", "--operation-id", predecessor_id,
        "--request-id", "request-02", "--transport-status", "transport_error",
        "--effect-stage", "none", "--pre-effect-absence-proven",
        "--effect-json", json.dumps(NO_EFFECT), "--receipt-json", json.dumps(NO_EFFECT),
        "--expect-revision", "1", env=env,
    ))
    assert failed["pending_operations"][0]["status"] == "failed"

    second = payload(run(
        "frontier", run_id, "start", "--request-id", "request-04", "--task-id", "task-plan-04",
        "--intent-json", '{"purpose":"planning","request_payload":"same-plan"}',
        "--expect-revision", "2", env=env,
    ))
    successor_id = second["pending_operations"][-1]["operation_id"]
    confirmed = payload(run(
        "frontier", run_id, "result", "--operation-id", successor_id,
        "--request-id", "request-04", "--message-id", "conversation-turn-12",
        "--transport-status", "success", "--frontier-decision", "ACCEPT",
        "--response-type", "plan", "--plan-json", '{"work_unit_id":"WU-08"}',
        "--effect-json", '{"request_id":"request-04","message_id":"conversation-turn-12"}',
        "--receipt-json", '{"request_id":"request-04","message_id":"conversation-turn-12"}',
        "--expect-revision", "3", env=env,
    ))
    assert confirmed["pending_operations"][0]["status"] == "failed"
    assert confirmed["pending_operations"][-1]["status"] == "confirmed"

    run("transition", run_id, "--to-phase", "collecting-context", "--reason", "context", "--expect-revision", "4", env=env)
    run("transition", run_id, "--to-phase", "implementing", "--reason", "implementation", "--expect-revision", "5", env=env)
    run("transition", run_id, "--to-phase", "verifying", "--reason", "verification", "--expect-revision", "6", env=env)

    evidence_digest = hashlib.sha256(json.dumps(NO_EFFECT, sort_keys=True).encode()).hexdigest()
    blocked = payload(run(
        "transition", run_id, "--to-phase", "blocked", "--reason", "legacy recovery blocker",
        "--blocker", "BLOCKED_FRONTIER_RECOVERY_MISSING_ORIGINAL_IDENTITY: request-02 has no message_id",
        "--recovery-code", "BLOCKED_FRONTIER_RECOVERY_MISSING_ORIGINAL_IDENTITY",
        "--recovery-operation-id", predecessor_id, "--recovery-resume-phase", "verifying",
        "--recovery-evidence-digest", evidence_digest, "--expect-revision", "7", env=env,
    ))
    assert blocked["status"] == "blocked"

    action = payload(run("reconcile", run_id, env=env))
    assert action["action"] == "FINALIZE_FRONTIER_SUPERSESSION"
    assert action["operation_ids"] == [predecessor_id, successor_id]

    resolved = payload(run(
        "recovery", run_id, "resolve-frontier", "--operation-id", predecessor_id,
        "--successor-operation-id", successor_id, "--disposition", "supersede",
        "--reason", "PRE_EFFECT_FAILURE", "--expect-revision", "8", env=env,
    ))
    assert resolved["status"] == "active"
    assert resolved["phase"] == "verifying"
    assert resolved["blocker"] is None
    assert resolved["recovery"]["status"] == "resolved"
    assert resolved["pending_operations"][0]["status"] == "superseded"
    assert resolved["pending_operations"][0]["successor_operation_id"] == successor_id
    assert resolved["pending_operations"][-1]["message_id"] == "conversation-turn-12"
    assert len(resolved["pending_operations"]) == 2

    replay = payload(run(
        "recovery", run_id, "resolve-frontier", "--operation-id", predecessor_id,
        "--successor-operation-id", successor_id, "--disposition", "supersede",
        "--reason", "PRE_EFFECT_FAILURE", "--expect-revision", "8", env=env,
    ))
    assert replay["revision"] == resolved["revision"]
    final_action = payload(run("reconcile", run_id, env=env))
    assert final_action["action"] == "RUN_LOCAL_VERIFICATION"

    # A Frontier resolver cannot erase an unrelated human blocker.
    unrelated = run(
        "init", "--run-id", "human-blocker", "--objective", "human",
        "--repository-root", repo, "--mode", "autonomous", "--scope", "narrow",
        "--execution-policy", "auto", env=env,
    ).stdout.strip()
    run(
        "transition", unrelated, "--to-phase", "blocked", "--reason", "human decision",
        "--blocker", "HUMAN_DECISION_REQUIRED", "--expect-revision", "0", env=env,
    )
    denied = run(
        "recovery", unrelated, "resolve-frontier", "--operation-id", predecessor_id,
        "--successor-operation-id", successor_id, "--expect-revision", "1", env=env, ok=False,
    )
    assert "operation not found" in denied.stderr or "RECOVERY_CANNOT_CLEAR_UNRELATED_BLOCKER" in denied.stderr

print("frontier blocked recovery test: PASS")
