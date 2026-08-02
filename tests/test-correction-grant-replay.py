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
    result = subprocess.run(
        [str(STATE), *args], capture_output=True, text=True, env=env, timeout=20,
    )
    if ok and result.returncode != 0:
        raise AssertionError(f"command failed: {args}\nstdout={result.stdout}\nstderr={result.stderr}")
    if not ok and result.returncode == 0:
        raise AssertionError(f"command unexpectedly succeeded: {args}")
    return result


def state_path(env: dict[str, str], run_id: str) -> Path:
    return Path(env["MYRMEX_STATE_HOME"]) / "runs" / run_id / "state.json"


def events_path(env: dict[str, str], run_id: str) -> Path:
    return state_path(env, run_id).with_name("events.jsonl")


def authorize(
    run_id: str, env: dict[str, str], *, revision: int, request_id: str = "grant-1",
    work_unit_id: str = "WU-replay", verification_request_id: str | None = None,
    defect_revision: int | None = None, scope_digest: str = "a" * 64,
    candidate_sha: str = "a" * 40, ok: bool = True,
) -> subprocess.CompletedProcess[str]:
    args = [
        "correction", "authorize", run_id, "--work-unit-id", work_unit_id,
        "--authority", "frontier", "--request-id", request_id,
        "--scope-digest", scope_digest, "--source-candidate-sha", candidate_sha,
        "--max-additional-attempts", "1", "--expect-revision", str(revision),
    ]
    if verification_request_id is not None:
        args.extend(["--verification-request-id", verification_request_id])
    if defect_revision is not None:
        args.extend(["--defect-revision", str(defect_revision)])
    return run(*args, env=env, ok=ok)


def start(
    run_id: str, env: dict[str, str], *, revision: int, task_id: str,
    work_unit_id: str = "WU-replay", request_id: str = "verify-1",
    scope_digest: str = "a" * 64, candidate_sha: str = "a" * 40,
    ok: bool = True,
) -> subprocess.CompletedProcess[str]:
    return run(
        "correction", "start", run_id, "--reason", "replay identity test",
        "--task-id", task_id, "--work-unit-id", work_unit_id,
        "--workspace", str(Path(env["MYRMEX_STATE_HOME"]).parent),
        "--source-request-id", request_id, "--scope-digest", scope_digest,
        "--source-candidate-sha", candidate_sha, "--expect-revision", str(revision),
        env=env, ok=ok,
    )


def finish(run_id: str, env: dict[str, str], *, revision: int, task_id: str, evidence: str = "{}") -> dict:
    return json.loads(run(
        "delegation", run_id, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "replay identity test", "--task-id", task_id,
        "--work-unit-id", "WU-replay",
        "--workspace", str(Path(env["MYRMEX_STATE_HOME"]).parent),
        "--status", "success", "--evidence-json", evidence,
        "--expect-revision", str(revision), env=env,
    ).stdout)


with tempfile.TemporaryDirectory(prefix="myrmex-grant-replay-") as td:
    env = dict(os.environ, MYRMEX_STATE_HOME=str(Path(td) / "state"), PYTHONDONTWRITEBYTECODE="1")
    run_id = run(
        "init", "--run-id", "myrmex-grant-replay", "--objective", "Grant replay identity",
        "--repository-root", td, "--mode", "autonomous", "--scope", "narrow", env=env,
    ).stdout.strip()

    defects = json.loads(run(
        "defects", run_id, "--work-unit-id", "WU-replay",
        "--verification-request-id", "verify-1", "--candidate-sha", "a" * 40,
        "--scope-digest", "a" * 64, "--defects-json", '["defect-1"]',
        "--expect-revision", "0", env=env,
    ).stdout)
    assert defects["verification_revision"] == 1

    # Exhaust the base WU capacity, then prove the grant is bound to every
    # blocker identity component before it is allowed to clear anything.
    start(run_id, env, revision=1, task_id="base-1")
    finish(run_id, env, revision=2, task_id="base-1")
    start(run_id, env, revision=3, task_id="base-2")
    finish(run_id, env, revision=4, task_id="base-2")
    blocked = json.loads(start(run_id, env, revision=5, task_id="blocked-1", ok=False).stdout)
    assert blocked["revision"] == 6
    assert blocked["remediation"]["blocked"] == {
        **blocked["remediation"]["blocked"],
        "verification_request_id": "verify-1",
        "defect_revision": 1,
    }

    state_before_failures = state_path(env, run_id).read_bytes()
    event_before_failures = events_path(env, run_id).read_bytes()
    for mismatch in [
        {"candidate_sha": "b" * 40},
        {"scope_digest": "b" * 64},
        {"work_unit_id": "WU-other"},
        {"verification_request_id": "verify-other"},
        {"defect_revision": 2},
    ]:
        authorize(run_id, env, revision=6, ok=False, **mismatch)
        assert state_path(env, run_id).read_bytes() == state_before_failures
        assert events_path(env, run_id).read_bytes() == event_before_failures

    # A stale expected revision is rejected before any blocker or grant change.
    authorize(run_id, env, revision=5, ok=False)
    assert state_path(env, run_id).read_bytes() == state_before_failures
    assert events_path(env, run_id).read_bytes() == event_before_failures

    authorized = json.loads(authorize(
        run_id, env, revision=6, verification_request_id="verify-1", defect_revision=1,
    ).stdout)
    grant = authorized["remediation"]["grants"][0]
    assert grant["work_unit_id"] == "WU-replay"
    assert grant["source_candidate_sha"] == "a" * 40
    assert grant["scope_digest"] == "a" * 64
    assert grant["verification_request_id"] == "verify-1"
    assert grant["defect_revision"] == 1
    assert grant["consumed_attempts"] == 0

    # Exact authorization replay is a byte-stable no-op after the blocker was
    # cleared, which is the crash-recovery path for the original identity.
    state_before_auth_replay = state_path(env, run_id).read_bytes()
    event_before_auth_replay = events_path(env, run_id).read_bytes()
    replayed_authorization = json.loads(authorize(
        run_id, env, revision=7, verification_request_id="verify-1", defect_revision=1,
    ).stdout)
    assert replayed_authorization["revision"] == 7
    assert state_path(env, run_id).read_bytes() == state_before_auth_replay
    assert events_path(env, run_id).read_bytes() == event_before_auth_replay

    consumed = start(run_id, env, revision=7, task_id="grant-1")
    consumed_state = json.loads(consumed.stdout)
    assert consumed_state["remediation"]["grants"][0]["consumed_attempts"] == 1
    finish(run_id, env, revision=8, task_id="grant-1")

    # A consumed grant cannot be replayed as a new task and cannot create a new
    # blocker as a side effect of the rejection.
    state_before_consumed_replay = state_path(env, run_id).read_bytes()
    event_before_consumed_replay = events_path(env, run_id).read_bytes()
    consumed_replay = start(run_id, env, revision=9, task_id="grant-1-replay", ok=False)
    assert "CORRECTION_GRANT_ALREADY_CONSUMED" in consumed_replay.stderr
    assert state_path(env, run_id).read_bytes() == state_before_consumed_replay
    assert events_path(env, run_id).read_bytes() == event_before_consumed_replay

    # A receipt conflict fails before mutation, while an exact terminal replay
    # remains idempotent.  The same checks cover a correction crash recovery.
    receipt_run = run(
        "init", "--run-id", "myrmex-receipt-conflict", "--objective", "Receipt conflict",
        "--repository-root", td, "--mode", "autonomous", "--scope", "narrow", env=env,
    ).stdout.strip()
    start(receipt_run, env, revision=0, task_id="receipt-task", request_id="verify-receipt")
    finish(receipt_run, env, revision=1, task_id="receipt-task", evidence='{"result":"one"}')
    receipt_state = state_path(env, receipt_run).read_bytes()
    receipt_events = events_path(env, receipt_run).read_bytes()
    conflict = run(
        "delegation", receipt_run, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "conflicting receipt", "--task-id", "receipt-task",
        "--work-unit-id", "WU-replay", "--workspace", str(Path(env["MYRMEX_STATE_HOME"]).parent),
        "--status", "success", "--evidence-json", '{"result":"two"}',
        "--expect-revision", "2", env=env, ok=False,
    )
    assert "DELEGATION_RESULT_OPERATION_CONFLICT" in conflict.stderr
    assert state_path(env, receipt_run).read_bytes() == receipt_state
    assert events_path(env, receipt_run).read_bytes() == receipt_events
    replayed_start = json.loads(start(
        receipt_run, env, revision=99, task_id="receipt-task", request_id="verify-receipt",
    ).stdout)
    assert replayed_start["revision"] == 2

    # Generic patching cannot rewrite grant identity, consumption, or blocker
    # ownership, even when it targets a dotted descendant.
    patch_state = state_path(env, run_id).read_bytes()
    run(
        "patch", run_id, "--set", "remediation.grants.0.source_candidate_sha=" + "b" * 40,
        "--expect-revision", "9", env=env, ok=False,
    )
    assert state_path(env, run_id).read_bytes() == patch_state

    # A newer verifier revision creates a newer blocker.  The old grant is not
    # allowed to clear or replace it; a new exact grant is required.
    newer = json.loads(run(
        "defects", run_id, "--work-unit-id", "WU-replay",
        "--verification-request-id", "verify-2", "--candidate-sha", "c" * 40,
        "--scope-digest", "c" * 64, "--remaining-json", '["defect-1"]',
        "--expect-revision", "9", env=env,
    ).stdout)
    assert newer["verification_revision"] == 2
    newer_blocked = json.loads(start(
        run_id, env, revision=10, task_id="blocked-2", request_id="verify-2",
        scope_digest="c" * 64, candidate_sha="c" * 40, ok=False,
    ).stdout)
    assert newer_blocked["revision"] == 11
    old_replay = authorize(
        run_id, env, revision=11, request_id="grant-1", verification_request_id="verify-1",
        defect_revision=1, ok=False,
    )
    assert "CORRECTION_AUTHORIZATION_IDENTITY_CONFLICT" in old_replay.stderr
    assert json.loads(run("show", run_id, env=env).stdout)["revision"] == 11
    new_grant = json.loads(authorize(
        run_id, env, revision=11, request_id="grant-2", verification_request_id="verify-2",
        defect_revision=2, scope_digest="c" * 64, candidate_sha="c" * 40,
    ).stdout)
    assert len(new_grant["remediation"]["grants"]) == 2
    assert new_grant["remediation"]["grants"][0]["consumed_attempts"] == 1

print("correction grant replay test: PASS")
