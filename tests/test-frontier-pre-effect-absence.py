#!/usr/bin/env python3
"""Focused hardening tests for Frontier pre-effect absence and immutable purpose.

Covers: complete false evidence (positive), any-true, missing fields,
contradictory/effect-receipt disagreement, timeout, message_id, wrong operation
kind, changed retry purpose, and matching/mismatching purpose digest.

These tests only touch a throwaway MYRMEX_STATE_HOME under the system temporary
directory; they never read or write the repository's protected runtime dirs.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "bin" / "myrmex-state"
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def run(*args: str, env: dict[str, str], ok: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run([str(STATE), *args], capture_output=True, text=True, env=env, timeout=20)
    if ok and result.returncode != 0:
        raise AssertionError(f"command failed: {args}\nstdout={result.stdout}\nstderr={result.stderr}")
    if not ok and result.returncode == 0:
        raise AssertionError(f"command unexpectedly succeeded: {args}\nstdout={result.stdout}")
    return result


def payload(result: subprocess.CompletedProcess[str]) -> dict:
    return json.loads(result.stdout)


FALSE_EVIDENCE = '{"browser_tab_opened":false,"outbound_request_observed":false,"request_sent":false}'


def init(env: dict[str, str], repository: str, run_id: str) -> str:
    return run(
        "init", "--run-id", run_id, "--objective", run_id,
        "--repository-root", repository, "--mode", "autonomous", "--scope", "narrow", "--execution-policy", "auto",
        env=env,
    ).stdout.strip()


def start(
    env: dict[str, str], run_id: str, revision: int, request_id: str, task_id: str,
    intent_json: str = "{}",
) -> dict:
    args = [
        "frontier", run_id, "start", "--request-id", request_id, "--task-id", task_id,
        "--intent-json", intent_json, "--expect-revision", str(revision),
    ]
    return payload(run(*args, env=env))


def record_result(
    env: dict[str, str], run_id: str, operation_id: str, revision: int, request_id: str,
    *, transport: str, stage: str, flag: bool = False,
    effect_json: str = FALSE_EVIDENCE, receipt_json: str = FALSE_EVIDENCE,
    message_id: str | None = None, ok: bool = True,
) -> subprocess.CompletedProcess[str]:
    args = [
        "frontier", run_id, "result", "--operation-id", operation_id,
        "--request-id", request_id, "--transport-status", transport,
        "--effect-stage", stage, "--expect-revision", str(revision),
        "--effect-json", effect_json, "--receipt-json", receipt_json,
    ]
    if flag:
        args.append("--pre-effect-absence-proven")
    if message_id is not None:
        args.extend(["--message-id", message_id])
    return run(*args, env=env, ok=ok)


def operation(env: dict[str, str], run_id: str, operation_id: str, revision: int) -> dict:
    state_data = payload(run("show", run_id, env=env))
    for item in state_data["pending_operations"]:
        if item["operation_id"] == operation_id:
            return item
    raise AssertionError(f"operation not found: {operation_id}")


def assert_ambiguous_failure(result: subprocess.CompletedProcess[str], run_id: str, operation_id: str, env: dict[str, str]) -> None:
    """Incomplete or contradictory evidence must never yield none or a retry."""
    data = payload(result)
    recorded = operation(env, run_id, operation_id, 0)
    assert recorded["status"] == "failed"
    assert recorded["effect_stage"] != "none"
    assert recorded["pre_effect_absence_proven"] is not True
    assert payload(run("reconcile", run_id, env=env))["action"] == "RECOVER_FRONTIER_EXCHANGE"
    assert data["revision"] >= 0


with tempfile.TemporaryDirectory(prefix="myrmex-pre-effect-absence-") as td:
    repository = str(Path(td) / "repo")
    Path(repository).mkdir()
    env = dict(os.environ, MYRMEX_STATE_HOME=str(Path(td) / "state"), PYTHONDONTWRITEBYTECODE="1")

    # --- Positive: complete false evidence proves pre-effect absence. -------
    proven_run = init(env, repository, "absence-proven")
    proven_started = start(env, proven_run, 0, "req-proven", "task-proven")
    proven_id = proven_started["pending_operations"][0]["operation_id"]
    proven_digest = proven_started["pending_operations"][0]["purpose_digest"]
    assert isinstance(proven_digest, str) and SHA256.fullmatch(proven_digest)
    failed = payload(run(
        "frontier", proven_run, "result", "--operation-id", proven_id,
        "--request-id", "req-proven", "--transport-status", "transport_error",
        "--effect-stage", "none", "--pre-effect-absence-proven",
        "--effect-json", FALSE_EVIDENCE, "--receipt-json", FALSE_EVIDENCE,
        "--expect-revision", "1", env=env,
    ))
    predecessor = failed["pending_operations"][0]
    assert predecessor["status"] == "failed"
    assert predecessor["effect_stage"] == "none"
    assert predecessor["pre_effect_absence_proven"] is True
    assert predecessor["purpose_digest"] == proven_digest
    assert payload(run("reconcile", proven_run, env=env))["action"] == "RETRY_FRONTIER_EXCHANGE"

    # --- Retry rejects any --intent-json change to the immutable purpose. ---
    immutable_purpose = run(
        "frontier", proven_run, "retry", "--operation-id", proven_id,
        "--request-id", "req-proven", "--pre-effect-absence-proven",
        "--intent-json", '{"purpose":"parent_gate"}', "--expect-revision", "2", env=env, ok=False,
    )
    assert "FRONTIER_RETRY_IMMUTABLE_INTENT_FIELDS: purpose" in immutable_purpose.stderr
    immutable_payload = run(
        "frontier", proven_run, "retry", "--operation-id", proven_id,
        "--request-id", "req-proven", "--pre-effect-absence-proven",
        "--intent-json", '{"outbound_message":"changed request"}', "--expect-revision", "2", env=env, ok=False,
    )
    assert "FRONTIER_RETRY_IMMUTABLE_INTENT_FIELDS: outbound_message" in immutable_payload.stderr
    immutable_digest = run(
        "frontier", proven_run, "retry", "--operation-id", proven_id,
        "--request-id", "req-proven", "--pre-effect-absence-proven",
        "--intent-json", '{"purpose_digest":"%s"}' % ("0" * 64), "--expect-revision", "2", env=env, ok=False,
    )
    assert "FRONTIER_RETRY_IMMUTABLE_INTENT_FIELDS: purpose_digest" in immutable_digest.stderr

    # --- Retry may only change transport identity fields. ------------------
    retried = payload(run(
        "frontier", proven_run, "retry", "--operation-id", proven_id,
        "--request-id", "req-proven", "--pre-effect-absence-proven",
        "--intent-json", '{"task_id":"task-proven-retry"}', "--expect-revision", "2", env=env,
    ))
    successor_id = retried["pending_operations"][-1]["operation_id"]
    successor = retried["pending_operations"][-1]
    assert successor_id != proven_id
    assert successor["predecessor_operation_id"] == proven_id
    assert successor["purpose_digest"] == proven_digest

    confirmed = payload(run(
        "frontier", proven_run, "result", "--operation-id", successor_id,
        "--request-id", "req-proven", "--message-id", "turn-proven-1",
        "--transport-status", "success", "--frontier-decision", "ACCEPT",
        "--effect-json", '{"request_id":"req-proven","message_id":"turn-proven-1"}',
        "--receipt-json", '{"request_id":"req-proven","message_id":"turn-proven-1"}',
        "--expect-revision", "3", env=env,
    ))
    assert confirmed["pending_operations"][-1]["status"] == "confirmed"
    superseded = payload(run(
        "operation", proven_run, "supersede", "--operation-id", proven_id,
        "--successor-operation-id", successor_id, "--reason", "PRE_EFFECT_FAILURE",
        "--expect-revision", "4", env=env,
    ))
    assert superseded["pending_operations"][0]["status"] == "superseded"
    assert superseded["pending_operations"][0]["successor_operation_id"] == successor_id

    # --- Any true marker keeps the exchange ambiguous. ---------------------
    true_run = init(env, repository, "absence-true")
    true_started = start(env, true_run, 0, "req-true", "task-true")
    true_id = true_started["pending_operations"][0]["operation_id"]
    true_result = record_result(
        env, true_run, true_id, 1, "req-true", transport="transport_error", stage="none", flag=True,
        effect_json='{"browser_tab_opened":true,"outbound_request_observed":false,"request_sent":false}',
        receipt_json='{"browser_tab_opened":true,"outbound_request_observed":false,"request_sent":false}',
    )
    assert_ambiguous_failure(true_result, true_run, true_id, env)

    # --- Missing required fields keep the exchange ambiguous. --------------
    missing_run = init(env, repository, "absence-missing")
    missing_started = start(env, missing_run, 0, "req-missing", "task-missing")
    missing_id = missing_started["pending_operations"][0]["operation_id"]
    missing_result = record_result(
        env, missing_run, missing_id, 1, "req-missing", transport="transport_error", stage="none", flag=True,
        effect_json='{"browser_tab_opened":false}',
        receipt_json='{"browser_tab_opened":false}',
    )
    assert_ambiguous_failure(missing_result, missing_run, missing_id, env)

    # --- Contradictory browser evidence keeps the exchange ambiguous. ------
    contradiction_run = init(env, repository, "absence-contradiction")
    contradiction_started = start(env, contradiction_run, 0, "req-contradiction", "task-contradiction")
    contradiction_id = contradiction_started["pending_operations"][0]["operation_id"]
    contradiction_result = record_result(
        env, contradiction_run, contradiction_id, 1, "req-contradiction",
        transport="transport_error", stage="none", flag=True,
        effect_json='{"browser_tab_opened":false,"outbound_request_observed":false,"request_sent":false}',
        receipt_json='{"browser_tab_opened":true,"outbound_request_observed":false,"request_sent":false}',
    )
    assert_ambiguous_failure(contradiction_result, contradiction_run, contradiction_id, env)

    # --- Receipt/effect disagreement keeps the exchange ambiguous. ---------
    disagreement_run = init(env, repository, "absence-disagreement")
    disagreement_started = start(env, disagreement_run, 0, "req-disagreement", "task-disagreement")
    disagreement_id = disagreement_started["pending_operations"][0]["operation_id"]
    disagreement_result = record_result(
        env, disagreement_run, disagreement_id, 1, "req-disagreement",
        transport="transport_error", stage="none", flag=True,
        effect_json='{"browser_tab_opened":false,"outbound_request_observed":false,"request_sent":false}',
        receipt_json='{"browser_tab_opened":false,"outbound_request_observed":true,"request_sent":false}',
    )
    assert_ambiguous_failure(disagreement_result, disagreement_run, disagreement_id, env)

    # --- Timeout never implies none, even with complete evidence. ----------
    timeout_run = init(env, repository, "absence-timeout")
    timeout_started = start(env, timeout_run, 0, "req-timeout", "task-timeout")
    timeout_id = timeout_started["pending_operations"][0]["operation_id"]
    timeout_result = record_result(
        env, timeout_run, timeout_id, 1, "req-timeout", transport="timeout", stage="none", flag=True,
    )
    assert "timeout cannot classify an exchange as pre-effect absence" in timeout_result.stdout
    timeout_recorded = operation(env, timeout_run, timeout_id, 0)
    assert timeout_recorded["effect_stage"] != "none"
    assert timeout_recorded["pre_effect_absence_proven"] is not True
    assert payload(run("reconcile", timeout_run, env=env))["action"] == "RECOVER_FRONTIER_EXCHANGE"

    # --- A message identity makes any no-effect claim ambiguous. -----------
    message_run = init(env, repository, "absence-message")
    message_started = start(env, message_run, 0, "req-message", "task-message")
    message_id = message_started["pending_operations"][0]["operation_id"]
    message_result = record_result(
        env, message_run, message_id, 1, "req-message", transport="transport_error", stage="none", flag=True,
        message_id="turn-message-1",
    )
    message_recorded = operation(env, message_run, message_id, 0)
    assert message_recorded["effect_stage"] != "none"
    assert message_recorded["pre_effect_absence_proven"] is not True
    assert any("message_id" in error for error in message_recorded["effect"].get("validation_errors", []))
    assert payload(run("reconcile", message_run, env=env))["action"] == "RECOVER_FRONTIER_EXCHANGE"

    message_evidence_run = init(env, repository, "absence-message-evidence")
    message_evidence_started = start(env, message_evidence_run, 0, "req-message-evidence", "task-message-evidence")
    message_evidence_id = message_evidence_started["pending_operations"][0]["operation_id"]
    message_evidence_result = record_result(
        env, message_evidence_run, message_evidence_id, 1, "req-message-evidence",
        transport="transport_error", stage="none", flag=True,
        effect_json='{"browser_tab_opened":false,"outbound_request_observed":false,"request_sent":false,"message_id":"turn-message-2"}',
        receipt_json='{"browser_tab_opened":false,"outbound_request_observed":false,"request_sent":false,"message_id":"turn-message-2"}',
    )
    assert_ambiguous_failure(message_evidence_result, message_evidence_run, message_evidence_id, env)

    # --- The authority flag cannot manufacture evidence by itself. ---------
    flag_only_run = init(env, repository, "absence-flag-only")
    flag_only_started = start(env, flag_only_run, 0, "req-flag-only", "task-flag-only")
    flag_only_id = flag_only_started["pending_operations"][0]["operation_id"]
    flag_only_result = record_result(
        env, flag_only_run, flag_only_id, 1, "req-flag-only", transport="transport_error", stage="none", flag=True,
        effect_json="{}", receipt_json="{}",
    )
    assert_ambiguous_failure(flag_only_result, flag_only_run, flag_only_id, env)

    # --- The authority flag is still required even with complete evidence. --
    no_flag_run = init(env, repository, "absence-no-flag")
    no_flag_started = start(env, no_flag_run, 0, "req-no-flag", "task-no-flag")
    no_flag_id = no_flag_started["pending_operations"][0]["operation_id"]
    no_flag_result = record_result(
        env, no_flag_run, no_flag_id, 1, "req-no-flag", transport="transport_error", stage="none", flag=False,
    )
    assert_ambiguous_failure(no_flag_result, no_flag_run, no_flag_id, env)

    # --- Mismatching purpose digest rejects the successor. -----------------
    mismatch_run = init(env, repository, "absence-digest-mismatch")
    mismatch_started = start(
        env, mismatch_run, 0, "req-mismatch", "task-mismatch",
        intent_json='{"outbound_message":"original request"}',
    )
    mismatch_id = mismatch_started["pending_operations"][0]["operation_id"]
    mismatch_digest = mismatch_started["pending_operations"][0]["purpose_digest"]
    run(
        "frontier", mismatch_run, "result", "--operation-id", mismatch_id,
        "--request-id", "req-mismatch", "--transport-status", "transport_error",
        "--effect-stage", "none", "--pre-effect-absence-proven",
        "--effect-json", FALSE_EVIDENCE, "--receipt-json", FALSE_EVIDENCE,
        "--expect-revision", "1", env=env,
    )
    other_started = start(
        env, mismatch_run, 2, "req-other", "task-other",
        intent_json='{"outbound_message":"different request"}',
    )
    other_id = other_started["pending_operations"][-1]["operation_id"]
    other_digest = other_started["pending_operations"][-1]["purpose_digest"]
    assert other_digest != mismatch_digest
    run(
        "frontier", mismatch_run, "result", "--operation-id", other_id,
        "--request-id", "req-other", "--message-id", "turn-other-1",
        "--transport-status", "success", "--frontier-decision", "ACCEPT",
        "--effect-json", '{"request_id":"req-other","message_id":"turn-other-1"}',
        "--receipt-json", '{"request_id":"req-other","message_id":"turn-other-1"}',
        "--expect-revision", "3", env=env,
    )
    mismatch_path = Path(env["MYRMEX_STATE_HOME"]) / "runs" / mismatch_run / "state.json"
    mismatch_bytes = mismatch_path.read_bytes()
    rejected_supersede = run(
        "operation", mismatch_run, "supersede", "--operation-id", mismatch_id,
        "--successor-operation-id", other_id, "--reason", "PRE_EFFECT_FAILURE",
        "--expect-revision", "4", env=env, ok=False,
    )
    assert "OPERATION_SUCCESSOR_PURPOSE_DIGEST_MISMATCH" in rejected_supersede.stderr
    assert mismatch_path.read_bytes() == mismatch_bytes
    # A start-with-predecessor successor must also preserve the digest.
    rejected_start_successor = run(
        "frontier", mismatch_run, "start", "--request-id", "req-mismatch-2", "--task-id", "task-mismatch-2",
        "--predecessor-operation-id", mismatch_id, "--pre-effect-absence-proven",
        "--intent-json", '{"outbound_message":"different request"}', "--expect-revision", "4", env=env, ok=False,
    )
    assert "OPERATION_SUCCESSOR_PURPOSE_DIGEST_MISMATCH" in rejected_start_successor.stderr
    assert mismatch_path.read_bytes() == mismatch_bytes

    # --- Non-Frontier operations cannot use the pre-effect mechanism. ------
    wrong_kind_run = init(env, repository, "absence-wrong-kind")
    ci_started = payload(run(
        "operation", wrong_kind_run, "intent", "--kind", "ci", "--idempotency-key", "ci:wrong-kind-absence",
        "--intent-json", '{"required":true}', "--expect-revision", "0", env=env,
    ))
    ci_id = ci_started["pending_operations"][0]["operation_id"]
    run(
        "operation", wrong_kind_run, "confirm", "--operation-id", ci_id,
        "--status", "failed", "--reason", "ci runner failed", "--expect-revision", "1", env=env,
    )
    wrong_kind_path = Path(env["MYRMEX_STATE_HOME"]) / "runs" / wrong_kind_run / "state.json"
    wrong_kind_bytes = wrong_kind_path.read_bytes()
    retry_wrong_kind = run(
        "frontier", wrong_kind_run, "retry", "--operation-id", ci_id,
        "--request-id", "req-ci", "--pre-effect-absence-proven",
        "--expect-revision", "2", env=env, ok=False,
    )
    assert "FRONTIER_RETRY_WRONG_KIND" in retry_wrong_kind.stderr
    abandon_wrong_kind = run(
        "operation", wrong_kind_run, "abandon", "--operation-id", ci_id,
        "--reason", "must not use Frontier proof", "--pre-effect-absence-proven",
        "--expect-revision", "2", env=env, ok=False,
    )
    assert "PRE_EFFECT_ABSENCE_PROOF_FRONTIER_ONLY" in abandon_wrong_kind.stderr
    assert wrong_kind_path.read_bytes() == wrong_kind_bytes

print("frontier pre-effect absence test: PASS")
