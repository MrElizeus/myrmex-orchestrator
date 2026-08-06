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

print("operation attempt lifecycle test: PASS")
