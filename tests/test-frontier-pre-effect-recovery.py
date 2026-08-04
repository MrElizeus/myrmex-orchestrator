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
        "init", "--run-id", run_id, "--objective", run_id,
        "--repository-root", repository, "--mode", "autonomous", "--scope", "narrow",
        env=env,
    ).stdout.strip()


def start(env: dict[str, str], run_id: str, revision: int, request_id: str = "request-02") -> dict:
    return payload(run(
        "frontier", run_id, "start", "--request-id", request_id, "--task-id", "task-plan",
        "--expect-revision", str(revision), env=env,
    ))


def fail_before_effect(
    env: dict[str, str], run_id: str, operation_id: str, revision: int,
    request_id: str = "request-02",
) -> dict:
    return payload(run(
        "frontier", run_id, "result", "--operation-id", operation_id,
        "--request-id", request_id, "--transport-status", "transport_error",
        "--effect-stage", "none", "--pre-effect-absence-proven",
        "--effect-json", '{"browser_tab_opened":false,"outbound_request_observed":false}',
        "--receipt-json", '{"browser_tab_opened":false,"outbound_request_observed":false}',
        "--expect-revision", str(revision), env=env,
    ))


with tempfile.TemporaryDirectory(prefix="myrmex-pre-effect-recovery-") as td:
    repository = str(Path(td) / "repo")
    Path(repository).mkdir()
    env = dict(os.environ, MYRMEX_STATE_HOME=str(Path(td) / "state"), PYTHONDONTWRITEBYTECODE="1")

    # A proven pre-effect failure is retryable and does not require a fictional
    # response message identity.
    retry_run = init(env, repository, "pre-effect-retry")
    started = start(env, retry_run, 0)
    predecessor_id = started["pending_operations"][0]["operation_id"]
    failed = fail_before_effect(env, retry_run, predecessor_id, 1)
    predecessor = failed["pending_operations"][0]
    assert predecessor["status"] == "failed"
    assert predecessor["effect_stage"] == "none"
    assert predecessor["pre_effect_absence_proven"] is True
    assert payload(run("reconcile", retry_run, env=env))["action"] == "RETRY_FRONTIER_EXCHANGE"

    retried = payload(run(
        "frontier", retry_run, "retry", "--operation-id", predecessor_id,
        "--request-id", "request-02", "--pre-effect-absence-proven",
        "--expect-revision", "2", env=env,
    ))
    successor_id = retried["pending_operations"][-1]["operation_id"]
    assert successor_id != predecessor_id
    assert retried["pending_operations"][-1]["predecessor_operation_id"] == predecessor_id

    confirmed = payload(run(
        "frontier", retry_run, "result", "--operation-id", successor_id,
        "--request-id", "request-02", "--message-id", "conversation-turn-12",
        "--transport-status", "success", "--frontier-decision", "ACCEPT",
        "--effect-json", '{"request_id":"request-02","message_id":"conversation-turn-12"}',
        "--receipt-json", '{"request_id":"request-02","message_id":"conversation-turn-12"}',
        "--expect-revision", "3", env=env,
    ))
    assert confirmed["pending_operations"][-1]["status"] == "confirmed"

    superseded = payload(run(
        "operation", retry_run, "supersede", "--operation-id", predecessor_id,
        "--successor-operation-id", successor_id, "--reason", "PRE_EFFECT_FAILURE",
        "--expect-revision", "4", env=env,
    ))
    assert superseded["pending_operations"][0]["status"] == "superseded"
    assert superseded["pending_operations"][0]["successor_operation_id"] == successor_id
    replayed_supersede = payload(run(
        "operation", retry_run, "supersede", "--operation-id", predecessor_id,
        "--successor-operation-id", successor_id, "--reason", "PRE_EFFECT_FAILURE",
        "--expect-revision", "999", env=env,
    ))
    assert replayed_supersede["revision"] == superseded["revision"]
    terminal = payload(run(
        "complete", retry_run, "--message", "safe successor confirmed", "--expect-revision", "5", env=env,
    ))
    assert terminal["state"]["status"] == "dormant"

    # Abandonment is a separate typed terminal resolution and also preserves
    # the failed operation's evidence without blocking completion.
    abandon_run = init(env, repository, "pre-effect-abandon")
    abandon_started = start(env, abandon_run, 0, request_id="request-abandon")
    abandon_id = abandon_started["pending_operations"][0]["operation_id"]
    fail_before_effect(env, abandon_run, abandon_id, 1, request_id="request-abandon")
    abandoned = payload(run(
        "operation", abandon_run, "abandon", "--operation-id", abandon_id,
        "--reason", "PRE_EFFECT_FAILURE", "--expect-revision", "2", env=env,
    ))
    assert abandoned["pending_operations"][0]["status"] == "abandoned"
    assert payload(run("complete", abandon_run, "--message", "no effect existed", "--expect-revision", "3", env=env))["state"]["status"] == "dormant"

    # An unproven failure remains conservative, but recovery may attach a
    # later response identity even though the original failure had none.
    ambiguous_run = init(env, repository, "ambiguous-recovery")
    ambiguous_started = start(env, ambiguous_run, 0, request_id="request-ambiguous")
    ambiguous_id = ambiguous_started["pending_operations"][0]["operation_id"]
    run(
        "frontier", ambiguous_run, "result", "--operation-id", ambiguous_id,
        "--request-id", "request-ambiguous", "--transport-status", "transport_error",
        "--effect-stage", "transport_started", "--expect-revision", "1", env=env,
    )
    assert payload(run("reconcile", ambiguous_run, env=env))["action"] == "RECOVER_FRONTIER_EXCHANGE"
    no_proof = run(
        "operation", ambiguous_run, "abandon", "--operation-id", ambiguous_id,
        "--reason", "not proven", "--expect-revision", "2", env=env, ok=False,
    )
    assert "PRE_EFFECT_ABSENCE_PROOF_REQUIRED" in no_proof.stderr
    recovered = payload(run(
        "frontier", ambiguous_run, "recover", "--operation-id", ambiguous_id,
        "--request-id", "request-ambiguous", "--message-id", "conversation-turn-13",
        "--transport-status", "success", "--frontier-decision", "ACCEPT",
        "--effect-json", '{"request_id":"request-ambiguous","message_id":"conversation-turn-13"}',
        "--receipt-json", '{"request_id":"request-ambiguous","message_id":"conversation-turn-13"}',
        "--expect-revision", "2", env=env,
    ))
    assert recovered["pending_operations"][0]["effective_status"] == "confirmed"

print("frontier pre-effect recovery test: PASS")
