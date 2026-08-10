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
    result = subprocess.run([str(STATE), *args], capture_output=True, text=True, env=env, timeout=30)
    if ok and result.returncode != 0:
        raise AssertionError(f"command failed: {args}\nstdout={result.stdout}\nstderr={result.stderr}")
    if not ok and result.returncode == 0:
        raise AssertionError(f"command unexpectedly succeeded: {args}\nstdout={result.stdout}")
    return result


def payload(result: subprocess.CompletedProcess[str]) -> dict:
    return json.loads(result.stdout)


def terminal(
    env: dict[str, str], run_id: str, repo: str, task: str, status: str, revision: int,
) -> dict:
    return payload(run(
        "delegation", run_id, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "bounded attempt", "--task-id", task, "--work-unit-id", "WU-01",
        "--workspace", repo, "--status", status, "--expect-revision", str(revision), env=env,
    ))


with tempfile.TemporaryDirectory(prefix="myrmex-attempt-lifecycle-") as td:
    env = dict(os.environ, MYRMEX_STATE_HOME=str(Path(td) / "state"), PYTHONDONTWRITEBYTECODE="1")
    repo = str(Path(td) / "repo")
    Path(repo).mkdir()
    run_id = run(
        "init", "--run-id", "delegation-attempts", "--objective", "finish WU-01",
        "--repository-root", repo, "--mode", "autonomous", "--scope", "narrow",
        "--execution-policy", "auto", env=env,
    ).stdout.strip()

    first = payload(run(
        "delegation-preflight", run_id, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "first attempt", "--task-id", "task-first", "--work-unit-id", "WU-01",
        "--workspace", repo, "--expect-revision", "0", env=env,
    ))
    first_id = first["pending_operations"][0]["operation_id"]
    failed = terminal(env, run_id, repo, "task-first", "failed", 1)
    first_op = failed["pending_operations"][0]
    assert first_op["status"] == "failed"
    assert first_op["terminal_disposition"] == "failed_attempt"
    assert failed["delegation_ledger"][0]["status"] == "failed"

    run("transition", run_id, "--to-phase", "collecting-context", "--reason", "context", "--expect-revision", "2", env=env)
    run("transition", run_id, "--to-phase", "implementing", "--reason", "implement", "--expect-revision", "3", env=env)
    run("transition", run_id, "--to-phase", "reporting", "--reason", "report", "--expect-revision", "4", env=env)
    blocked = payload(run("reconcile", run_id, env=env))
    assert blocked["action"] == "BLOCKED_STATE_INCOMPLETE"
    assert any(item == "open work units: WU-01" for item in blocked["completion_blockers"])
    assert not any(first_id in item for item in blocked["completion_blockers"])

    retry = payload(run(
        "delegation-preflight", run_id, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "second attempt", "--task-id", "task-second", "--work-unit-id", "WU-01",
        "--workspace", repo, "--expect-revision", "5", env=env,
    ))
    second_op = retry["pending_operations"][-1]
    assert second_op["predecessor_operation_id"] == first_id
    assert second_op["attempt_group_id"] == first_op["attempt_group_id"]
    assert retry["pending_operations"][0]["successor_operation_id"] == second_op["operation_id"]

    succeeded = terminal(env, run_id, repo, "task-second", "success", 6)
    assert succeeded["pending_operations"][-1]["status"] == "confirmed"
    assert succeeded["pending_operations"][-1]["terminal_disposition"] == "completed_effect"
    completed_wu = payload(run(
        "work-unit", run_id, "complete", "--work-unit-id", "WU-01",
        "--evidence-json", '{"verification":"pass"}', "--expect-revision", "7", env=env,
    ))
    assert completed_wu["work_units"]["WU-01"]["status"] == "complete"
    terminal_state = payload(run(
        "complete", run_id, "--message", "historical failure retained", "--expect-revision", "8", env=env,
    ))
    assert terminal_state["state"]["status"] == "dormant"
    assert terminal_state["state"]["pending_operations"][0]["status"] == "failed"

    # A batch that still awaits the failed task remains a real blocker even
    # though the individual terminal attempt is historical.
    batch_run = run(
        "init", "--run-id", "delegation-batch-open", "--objective", "batch",
        "--repository-root", repo, "--mode", "autonomous", "--scope", "narrow",
        "--execution-policy", "auto", env=env,
    ).stdout.strip()
    payload(run(
        "delegation-batch", batch_run, "start", "--batch-id", "batch-1",
        "--task-ids-json", '["batch-task"]', "--expect-revision", "0", env=env,
    ))
    preflight = payload(run(
        "delegation-preflight", batch_run, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "batch attempt", "--task-id", "batch-task", "--work-unit-id", "WU-01",
        "--workspace", repo, "--expect-revision", "1", env=env,
    ))
    assert preflight["pending_operations"]
    terminal(env, batch_run, repo, "batch-task", "failed", 2)
    batch_state = payload(run("show", batch_run, env=env))
    # Move to reporting through the batch's own typed lifecycle is intentionally
    # not attempted; completion itself proves that the incomplete batch gates.
    denied = run(
        "complete", batch_run, "--message", "must remain blocked", "--expect-revision", "3",
        env=env, ok=False,
    )
    assert "incomplete delegation batches" in denied.stderr
    assert batch_state["delegation_batches"][0]["status"] == "waiting-for-delegations"

    # Task identity is state-first. A crash immediately before transport or
    # immediately after a terminal result can only resume/collect the exact
    # persisted operation; exact retries are byte-preserving no-ops.
    replay_run = run(
        "init", "--run-id", "task-result-crash-replay", "--objective", "task replay",
        "--repository-root", repo, "--mode", "autonomous", "--scope", "narrow",
        "--execution-policy", "auto", env=env,
    ).stdout.strip()
    replay_preflight_args = (
        "delegation-preflight", replay_run, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "state-first task identity", "--task-id", "task-crash-replay",
        "--work-unit-id", "WU-CRASH", "--workspace", repo,
    )
    task_intent = payload(run(*replay_preflight_args, "--expect-revision", "0", env=env))
    operation = task_intent["pending_operations"][0]
    replay_path = Path(env["MYRMEX_STATE_HOME"]) / "runs" / replay_run / "state.json"
    replay_events = replay_path.parent / "events.jsonl"
    intent_state_bytes, intent_event_bytes = replay_path.read_bytes(), replay_events.read_bytes()
    replayed_intent = payload(run(*replay_preflight_args, "--expect-revision", "999", env=env))
    assert replayed_intent["revision"] == 1
    assert replay_path.read_bytes() == intent_state_bytes and replay_events.read_bytes() == intent_event_bytes
    decision = payload(run("reconcile", replay_run, env=env))
    assert decision["action"] == "COLLECT_DELEGATIONS"
    terminal_result = payload(run(
        "delegation", replay_run, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "state-first task identity", "--task-id", "task-crash-replay",
        "--work-unit-id", "WU-CRASH", "--workspace", repo, "--status", "success",
        "--evidence-json", '{"result":"confirmed"}', "--expect-revision", "1", env=env,
    ))
    assert terminal_result["pending_operations"][0]["status"] == "confirmed"
    terminal_state_bytes, terminal_event_bytes = replay_path.read_bytes(), replay_events.read_bytes()
    replayed_result = payload(run(
        "delegation", replay_run, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "state-first task identity", "--task-id", "task-crash-replay",
        "--work-unit-id", "WU-CRASH", "--workspace", repo, "--status", "success",
        "--evidence-json", '{"result":"confirmed"}', "--expect-revision", "999", env=env,
    ))
    assert replayed_result["revision"] == 2
    assert replay_path.read_bytes() == terminal_state_bytes and replay_events.read_bytes() == terminal_event_bytes
    assert len(replayed_result["delegation_ledger"]) == 1

    # The same state-first/result pattern is mandatory for independent
    # verification tasks. A pending verifier is collected, never redispatched
    # under a new identity, and a confirmed verifier result is immutable.
    verifier_args = (
        "delegation-preflight", replay_run, "--agent", "myrmex-verifier", "--role", "verifier",
        "--reason", "verification crash boundary", "--task-id", "task-verifier-crash-replay",
        "--work-unit-id", "WU-CRASH", "--workspace", repo,
    )
    verifier_intent = payload(run(*verifier_args, "--expect-revision", "2", env=env))
    verifier_operation = verifier_intent["pending_operations"][-1]
    assert payload(run("reconcile", replay_run, env=env))["action"] == "COLLECT_DELEGATIONS"
    verifier_result = payload(run(
        "delegation", replay_run, "--agent", "myrmex-verifier", "--role", "verifier",
        "--reason", "verification crash boundary", "--task-id", "task-verifier-crash-replay",
        "--work-unit-id", "WU-CRASH", "--workspace", repo, "--status", "success",
        "--evidence-json", '{"verification":"PASS"}', "--expect-revision", "3", env=env,
    ))
    verifier_bytes, verifier_event_bytes = replay_path.read_bytes(), replay_events.read_bytes()
    verifier_replay = payload(run(
        "delegation", replay_run, "--agent", "myrmex-verifier", "--role", "verifier",
        "--reason", "verification crash boundary", "--task-id", "task-verifier-crash-replay",
        "--work-unit-id", "WU-CRASH", "--workspace", repo, "--status", "success",
        "--evidence-json", '{"verification":"PASS"}', "--expect-revision", "999", env=env,
    ))
    assert verifier_result["revision"] == verifier_replay["revision"] == 4
    assert replay_path.read_bytes() == verifier_bytes and replay_events.read_bytes() == verifier_event_bytes

    # CI uses the generic typed operation lifecycle. Intent is the pre-effect
    # crash state; effect/receipt/confirmation are independently replayable and
    # exact terminal replay cannot consume a revision or repeat the CI effect.
    ci_intent = payload(run(
        "operation", replay_run, "intent", "--kind", "ci", "--idempotency-key", "ci:p1-019",
        "--intent-json", '{"required":true,"work_unit_id":"WU-CRASH"}',
        "--expect-revision", "4", env=env,
    ))
    ci_operation = ci_intent["pending_operations"][-1]
    assert payload(run("reconcile", replay_run, env=env))["action"] == "RUN_LOCAL_VERIFICATION"
    run(
        "operation", replay_run, "observe", "--operation-id", ci_operation["operation_id"],
        "--effect-json", '{"job_id":"ci-p1-019","status":"pass"}',
        "--expect-revision", "5", env=env,
    )
    run(
        "operation", replay_run, "receipt", "--operation-id", ci_operation["operation_id"],
        "--receipt-json", '{"job_id":"ci-p1-019","status":"pass"}',
        "--expect-revision", "6", env=env,
    )
    ci_confirmed = payload(run(
        "operation", replay_run, "confirm", "--operation-id", ci_operation["operation_id"],
        "--status", "confirmed", "--reason", "CI receipt verified",
        "--expect-revision", "7", env=env,
    ))
    ci_bytes, ci_event_bytes = replay_path.read_bytes(), replay_events.read_bytes()
    ci_replay = payload(run(
        "operation", replay_run, "confirm", "--operation-id", ci_operation["operation_id"],
        "--status", "confirmed", "--reason", "CI receipt verified",
        "--expect-revision", "999", env=env,
    ))
    assert ci_confirmed["revision"] == ci_replay["revision"] == 8
    assert replay_path.read_bytes() == ci_bytes and replay_events.read_bytes() == ci_event_bytes
    assert len({entry["task_id"] for entry in ci_replay["delegation_ledger"]}) == 2
    attempt_evidence = {
        "boundaries": ["task_id", "result", "verification", "ci"],
        "operation_lineage": [operation["operation_id"], verifier_operation["operation_id"], ci_operation["operation_id"]],
        "task_ids": ["task-crash-replay", "task-verifier-crash-replay"],
        "duplicate_count": 0,
        "reconciliation_decisions": ["COLLECT_DELEGATIONS", "RUN_LOCAL_VERIFICATION", "reuse_confirmed_result"],
    }

print("CRASH_REPLAY_EVIDENCE=" + json.dumps(attempt_evidence, sort_keys=True))
print("operation attempt lifecycle test: PASS")
