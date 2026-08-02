#!/usr/bin/env python3
from __future__ import annotations

import json
import hashlib
import importlib.util
from importlib.machinery import SourceFileLoader
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "bin" / "myrmex-state"
POLICY_RESOLVER = ROOT / "scripts" / "resolve-delivery-policy.py"

state_loader = SourceFileLoader("myrmex_state", str(STATE))
state_spec = importlib.util.spec_from_loader("myrmex_state", state_loader)
assert state_spec is not None and state_spec.loader is not None
state_module = importlib.util.module_from_spec(state_spec)
state_spec.loader.exec_module(state_module)
canonical_sensitive = {
    key: f"{key}-value"
    for key in (
        "secret", "client_secret", "token", "access_token", "authorization",
        "password", "credential", "cookie", "api_key", "private_key",
    )
}
sanitized_evidence = state_module.sanitize_evidence({
    **canonical_sensitive,
    "authorization_id": "auth-012345678901234567890123",
    "ordinary": "ordinary secret text",
    "message": "Bearer abcdefghijklmnopqrstuvwxyz",
    "identifier-map-secret": "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "refs/heads/fix/memory-secret-metric-hardening": {
        "before": ["deadbeef", ""],
    },
    "refs/remotes/origin/memory-secret-metric-hardening": {"token": "nested-token"},
    "/tmp/memory-secret-metric-hardening": {"password": "nested-password"},
    "https://example.invalid/memory-secret-metric-hardening": {"private_key": "nested-key"},
})
assert all(sanitized_evidence[key] == "[REDACTED]" for key in canonical_sensitive)
assert sanitized_evidence["authorization_id"] == "auth-012345678901234567890123"
assert sanitized_evidence["ordinary"] == "ordinary secret text"
assert sanitized_evidence["message"] == "[REDACTED]"
assert sanitized_evidence["identifier-map-secret"] == "[REDACTED]"
assert sanitized_evidence["refs/heads/fix/memory-secret-metric-hardening"] == {
    "before": ["deadbeef", ""],
}
assert sanitized_evidence["refs/remotes/origin/memory-secret-metric-hardening"] == {"token": "[REDACTED]"}
assert sanitized_evidence["/tmp/memory-secret-metric-hardening"] == {"password": "[REDACTED]"}
assert sanitized_evidence["https://example.invalid/memory-secret-metric-hardening"] == {
    "private_key": "[REDACTED]",
}


def run(*args: str, env: dict[str, str], ok: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run([str(STATE), *args], capture_output=True, text=True, env=env, timeout=20)
    if ok and result.returncode != 0:
        raise AssertionError(f"command failed: {args}\nstdout={result.stdout}\nstderr={result.stderr}")
    if not ok and result.returncode == 0:
        raise AssertionError(f"command unexpectedly succeeded: {args}")
    return result


def finish_correction(
    run_id: str, *, env: dict[str, str], revision: int, task_id: str, work_unit_id: str,
    reason: str, request_id: str, scope_digest: str, candidate_sha: str,
    status: str = "success", evidence_json: str | None = None,
) -> dict:
    run(
        "correction", "start", run_id, "--reason", reason, "--task-id", task_id,
        "--work-unit-id", work_unit_id, "--workspace", str(Path(env["MYRMEX_STATE_HOME"]).parent),
        "--source-request-id", request_id, "--scope-digest", scope_digest,
        "--source-candidate-sha", candidate_sha, "--expect-revision", str(revision), env=env,
    )
    terminal = [
        "delegation", run_id, "--agent", "myrmex-worker", "--role", "writer", "--reason", reason,
        "--task-id", task_id, "--work-unit-id", work_unit_id,
        "--workspace", str(Path(env["MYRMEX_STATE_HOME"]).parent), "--status", status,
        "--expect-revision", str(revision + 1),
    ]
    if evidence_json is not None:
        terminal.extend(["--evidence-json", evidence_json])
    return json.loads(run(*terminal, env=env).stdout)


with tempfile.TemporaryDirectory(prefix="myrmex-state-test-") as td:
    env = dict(os.environ, MYRMEX_STATE_HOME=str(Path(td) / "state"), PYTHONDONTWRITEBYTECODE="1")
    init = run(
        "init", "--objective", "State test", "--repository-root", td,
        "--mode", "autonomous", "--scope", "narrow", "--commit-policy", "ask", "--push-policy", "deny",
        env=env,
    )
    run_id = init.stdout.strip()
    state = json.loads(run("show", run_id, env=env).stdout)
    assert state["schema"] == "myrmex.frontier-state/v2"
    assert state["schema_version"] == 2
    assert state["revision"] == 0
    assert state["push_status"] == "not_requested"
    assert state["execution"] == {
        "requested_policy": "auto", "effective_route": "auto", "authority": "system",
        "request_id": None, "set_at": state["created_at"], "locked": False,
    }
    assert state["attempts"] == {"scouts": 0, "writers": 0, "verifiers": 0, "corrections": 0}
    assert state["delegation_ledger"] == []
    assert state["no_progress_cycles"] == 0
    assert state["work_units"] == {} and state["remediation"]["total_corrections_used"] == 0

    # A persisted direct-only policy survives ordinary reads/migration and
    # rejects both stateful delegation entry points and Frontier checks.
    direct = run(
        "init", "--run-id", "myrmex-direct-policy", "--objective", "Direct policy test",
        "--repository-root", td, "--mode", "autonomous", "--scope", "narrow", env=env,
    ).stdout.strip()
    direct_only = json.loads(run(
        "route", "set", direct, "--policy", "direct-only", "--authority", "user",
        "--request-id", "req-user-direct-1", "--expect-revision", "0", env=env,
    ).stdout)
    assert direct_only["execution"]["effective_route"] == "direct"
    assert direct_only["execution"]["locked"] is True
    repeated_direct = json.loads(run(
        "route", direct, "set", "--policy", "direct-only", "--authority", "user",
        "--request-id", "req-user-direct-1", "--expect-revision", "0", env=env,
    ).stdout)
    assert repeated_direct["revision"] == 1
    run(
        "route", "set", direct, "--policy", "direct-only", "--authority", "user",
        "--request-id", "req-user-direct-rewrite", "--expect-revision", "1", env=env, ok=False,
    )
    run(
        "route", "set", direct, "--policy", "direct-only", "--authority", "frontier",
        "--request-id", "req-frontier-direct", "--expect-revision", "1", env=env, ok=False,
    )
    run(
        "delegation", direct, "--agent", "myrmex-worker", "--role", "writer", "--reason", "must not start",
        "--status", "started", "--expect-revision", "1", env=env, ok=False,
    )
    run(
        "delegation-batch", direct, "start", "--batch-id", "forbidden", "--task-ids-json", '["task-1"]',
        "--expect-revision", "1", env=env, ok=False,
    )
    run("route", "assert", direct, "--action", "frontier", "--expect-revision", "1", env=env, ok=False)
    run(
        "route", direct, "set", "--policy", "auto", "--authority", "user",
        "--request-id", "req-user-auto-1", "--expect-revision", "1", env=env, ok=False,
    )
    persisted_direct = json.loads(run("migrate", direct, "--expect-revision", "1", env=env).stdout)
    assert persisted_direct["state"]["execution"]["requested_policy"] == "direct-only"

    # The switch into direct-only is rejected while a persisted child task is
    # still active; it must first be consolidated or recovered.
    pending = run(
        "init", "--run-id", "myrmex-route-pending", "--objective", "Route pending test",
        "--repository-root", td, "--mode", "autonomous", "--scope", "narrow", env=env,
    ).stdout.strip()
    run(
        "delegation", pending, "--agent", "myrmex-worker", "--role", "writer", "--reason", "pending child",
        "--task-id", "task-pending", "--work-unit-id", "WU-pending", "--workspace", td,
        "--status", "started", "--expect-revision", "0", env=env,
    )
    run(
        "route", pending, "set", "--policy", "direct-only", "--authority", "user",
        "--request-id", "req-user-direct-pending", "--expect-revision", "1", env=env, ok=False,
    )

    # A start must have a durable task identity, and old anonymous started
    # records still block a switch to direct-only rather than being forgotten.
    anonymous = run(
        "init", "--run-id", "myrmex-route-anonymous", "--objective", "Anonymous route test",
        "--repository-root", td, "--mode", "autonomous", "--scope", "narrow", env=env,
    ).stdout.strip()
    run(
        "delegation", anonymous, "--agent", "myrmex-worker", "--role", "writer", "--reason", "missing identity",
        "--status", "started", "--expect-revision", "0", env=env, ok=False,
    )
    anonymous_path = Path(td) / "state" / "runs" / anonymous / "state.json"
    anonymous_state = json.loads(anonymous_path.read_text())
    anonymous_state["delegation_ledger"].append({"status": "started", "task_id": None})
    anonymous_path.write_text(json.dumps(anonymous_state))
    anonymous_route = run(
        "route", "set", anonymous, "--policy", "direct-only", "--authority", "user",
        "--request-id", "req-user-direct-anonymous", "--expect-revision", "0", env=env, ok=False,
    )
    assert "anonymous active delegation records" in anonymous_route.stderr

    run("lock", run_id, "--owner", "test-owner", env=env)
    run("lock", run_id, "--owner", "other-owner", env=env, ok=False)
    run("lock", run_id, "--owner", "test-owner", env=env)

    run(
        "delegation-preflight", run_id, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "bounded implementation", "--task-id", "task-1", "--work-unit-id", "unit-1",
        "--workspace", td, "--expect-revision", "0", env=env,
    )
    recorded = json.loads(run(
        "delegation", run_id, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "bounded implementation", "--task-id", "task-1",
        "--work-unit-id", "unit-1", "--workspace", td, "--status", "success", "--expect-revision", "1", env=env,
    ).stdout)
    assert recorded["revision"] == 2
    assert recorded["attempts"]["writers"] == 1
    assert recorded["delegation_ledger"][0]["work_unit_id"] == "unit-1"
    assert recorded["delegation_ledger"][0]["attempt"] == 1
    replayed = json.loads(run(
        "delegation", run_id, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "retry is harmless", "--task-id", "task-1", "--work-unit-id", "unit-1",
        "--workspace", td, "--status", "success", "--expect-revision", "99", env=env,
    ).stdout)
    assert replayed["revision"] == 2 and len(replayed["delegation_ledger"]) == 1
    run(
        "delegation", run_id, "--agent", "myrmex-worker", "--role", "writer", "--reason", "conflict",
        "--task-id", "task-1", "--work-unit-id", "unit-1", "--workspace", td, "--status", "failed",
        "--expect-revision", "2", env=env, ok=False,
    )

    defects = json.loads(run(
        "defects", run_id, "--work-unit-id", "WU-02", "--verification-request-id", "req-verify-wu02-1",
        "--candidate-sha", "a" * 40, "--scope-digest", "a" * 64,
        "--defects-json", '["missing assertion", "missing test"]', "--expect-revision", "2", env=env,
    ).stdout)
    assert defects["revision"] == 3
    assert defects["defect_history"][0]["remaining"] == ["missing assertion", "missing test"]

    progress = json.loads(run(
        "defects", run_id, "--work-unit-id", "WU-02", "--verification-request-id", "req-verify-wu02-2",
        "--candidate-sha", "b" * 40, "--scope-digest", "b" * 64,
        "--corrected-json", '["missing assertion"]', "--remaining-json", '["missing test"]', "--new-json", "[]",
        "--expect-revision", "3", env=env,
    ).stdout)
    assert progress["revision"] == 4
    assert progress["no_progress_cycles"] == 0
    assert progress["defect_history"][-1]["progress"] == "reduced"

    run(
        "patch", run_id, "--expect-revision", "4", "--set", "phase=collecting-context",
        env=env, ok=False,
    )
    run(
        "patch", run_id, "--expect-revision", "4", "--set", "budgets.max_corrections_per_work_unit=99",
        env=env, ok=False,
    )
    transitioned = json.loads(run(
        "transition", run_id, "--expect-revision", "4", "--to-phase", "collecting-context",
        "--reason", "context collection is the next safe step",
        env=env,
    ).stdout)
    assert transitioned["revision"] == 5 and transitioned["phase"] == "collecting-context"
    run(
        "transition", run_id, "--expect-revision", "4", "--to-phase", "requesting-plan",
        "--reason", "stale revision must fail", env=env, ok=False,
    )
    run(
        "transition", run_id, "--expect-revision", "5", "--to-phase", "pushing",
        "--reason", "phase skips must fail", env=env, ok=False,
    )
    # External receipts are now owned by the typed operation lifecycle; the
    # generic compatibility patch remains available only for non-critical
    # metadata and cannot bypass completion/recovery gates.
    run(
        "patch", run_id, "--expect-revision", "5",
        "--json-patch", '{"receipts.github_pr":{"number":1,"url":"https://example.test/pr/1}}',
        env=env, ok=False,
    )
    metadata = json.loads(run(
        "patch", run_id, "--expect-revision", "5",
        "--json-patch", '{"notes":"non-critical metadata"}',
        env=env,
    ).stdout)
    assert metadata["revision"] == 6 and metadata["notes"] == "non-critical metadata"

    # Correction capacity is scoped to a WU.  Every correction is charged by
    # its preflight, while its terminal result only closes that same attempt.
    correction_args = {
        "run_id": run_id, "env": env, "task_id": "task-correction-1", "work_unit_id": "WU-02",
        "reason": "fix verified defects", "request_id": "req-verify-wu02-2",
        "scope_digest": "b" * 64, "candidate_sha": "b" * 40,
    }
    first_start = run(
        "correction", "start", run_id, "--reason", correction_args["reason"], "--task-id", correction_args["task_id"],
        "--work-unit-id", correction_args["work_unit_id"], "--workspace", td,
        "--source-request-id", correction_args["request_id"], "--scope-digest", correction_args["scope_digest"],
        "--source-candidate-sha", correction_args["candidate_sha"], "--expect-revision", "6", env=env,
    ).stdout
    first_start_state = json.loads(first_start)
    replay_start = json.loads(run(
        "correction", "start", run_id, "--reason", correction_args["reason"], "--task-id", correction_args["task_id"],
        "--work-unit-id", correction_args["work_unit_id"], "--workspace", td,
        "--source-request-id", correction_args["request_id"], "--scope-digest", correction_args["scope_digest"],
        "--source-candidate-sha", correction_args["candidate_sha"], "--expect-revision", "99", env=env,
    ).stdout)
    assert first_start_state["revision"] == replay_start["revision"] == 7
    assert first_start_state["attempts"]["writers"] == 2
    assert first_start_state["attempts"]["corrections"] == 1
    assert first_start_state["pending_operations"][-1]["intent"]["correction"]["work_unit_id"] == "WU-02"
    assert first_start_state["delegation_ledger"][-1]["operation_id"] == first_start_state["pending_operations"][-1]["operation_id"]
    correction_one = finish_correction(**correction_args, revision=6)
    assert correction_one["work_units"]["WU-02"]["corrections_used"] == 1
    assert correction_one["attempts"]["writers"] == 2 and correction_one["attempts"]["corrections"] == 1
    assert correction_one["delegation_ledger"][-1]["attempt"] == 2
    correction_two = finish_correction(
        run_id, env=env, revision=8, task_id="task-correction-2", work_unit_id="WU-02",
        reason="fix remaining verified defects", request_id="req-verify-wu02-3",
        scope_digest="c" * 64, candidate_sha="c" * 40,
    )
    assert correction_two["attempts"]["corrections"] == 2
    wu03_first = finish_correction(
        run_id, env=env, revision=10, task_id="task-correction-3", work_unit_id="WU-03",
        reason="first independent WU-03 correction", request_id="req-verify-wu03-1",
        scope_digest="d" * 64, candidate_sha="d" * 40,
    )
    assert wu03_first["work_units"]["WU-03"]["corrections_used"] == 1
    assert wu03_first["remediation"]["total_corrections_used"] == 3

    blocked = json.loads(run(
        "correction", "start", run_id, "--reason", "third WU-02 correction must stop",
        "--task-id", "task-correction-4", "--work-unit-id", "WU-02", "--workspace", td,
        "--source-request-id", "req-verify-wu02-4", "--scope-digest", "e" * 64,
        "--source-candidate-sha", "e" * 40, "--expect-revision", "12", env=env, ok=False,
    ).stdout)
    assert blocked["revision"] == 13
    assert blocked["status"] == "blocked" and blocked["blocker"] == "BLOCKED_CORRECTION_BUDGET"
    assert blocked["remediation"]["blocked"]["work_unit_id"] == "WU-02"
    run("patch", run_id, "--set", "status=active", env=env, ok=False)
    run(
        "transition", run_id, "--expect-revision", "13", "--to-phase", "implementing",
        "--reason", "generic transitions cannot clear a correction blocker", env=env, ok=False,
    )
    # An authorization cannot clear WU-02 with WU-03 scope.  The matching typed
    # grant reopens the exact blocker and gives exactly one extra attempt.
    run(
        "correction", "authorize", run_id, "--work-unit-id", "WU-03", "--authority", "frontier",
        "--request-id", "req-frontier-wu03", "--scope-digest", "d" * 64,
        "--source-candidate-sha", "d" * 40, "--max-additional-attempts", "1",
        "--expect-revision", "13", env=env, ok=False,
    )
    run(
        "correction", "authorize", run_id, "--work-unit-id", "WU-02", "--authority", "frontier",
        "--request-id", "req-frontier-wu02", "--scope-digest", "e" * 64,
        "--source-candidate-sha", "e" * 40, "--max-additional-attempts", "1",
        "--expect-revision", "12", env=env, ok=False,
    )
    authorized = json.loads(run(
        "correction", "authorize", run_id, "--work-unit-id", "WU-02", "--authority", "frontier",
        "--request-id", "req-frontier-wu02", "--scope-digest", "e" * 64,
        "--source-candidate-sha", "e" * 40, "--max-additional-attempts", "1",
        "--expect-revision", "13", env=env,
    ).stdout)
    assert authorized["status"] == "active" and authorized["remediation"]["grants"][0]["granted_attempts"] == 1
    replay = json.loads(run(
        "correction", "authorize", run_id, "--work-unit-id", "WU-02", "--authority", "frontier",
        "--request-id", "req-frontier-wu02", "--scope-digest", "e" * 64,
        "--source-candidate-sha", "e" * 40, "--max-additional-attempts", "1",
        "--expect-revision", "14", env=env,
    ).stdout)
    assert replay["revision"] == 14 and len(replay["remediation"]["grants"]) == 1
    stale_scope = json.loads(run(
        "correction", "start", run_id, "--reason", "stale candidate and changed defect scope cannot consume grant",
        "--task-id", "task-correction-stale", "--work-unit-id", "WU-02", "--workspace", td,
        "--source-request-id", "req-verify-wu02-stale", "--scope-digest", "f" * 64, "--source-candidate-sha", "f" * 40,
        "--expect-revision", "14", env=env, ok=False,
    ).stdout)
    assert stale_scope["blocker"] == "BLOCKED_CORRECTION_BUDGET"
    assert stale_scope["remediation"]["grants"][0]["consumed_attempts"] == 0
    old_grant_replay = run(
        "correction", "authorize", run_id, "--work-unit-id", "WU-02", "--authority", "frontier",
        "--request-id", "req-frontier-wu02", "--scope-digest", "e" * 64,
        "--source-candidate-sha", "e" * 40, "--max-additional-attempts", "1",
        "--expect-revision", "15", env=env, ok=False,
    )
    assert "CORRECTION_AUTHORIZATION_IDENTITY_CONFLICT" in old_grant_replay.stderr
    reactivated = json.loads(run(
        "correction", "authorize", run_id, "--work-unit-id", "WU-02", "--authority", "frontier",
        "--request-id", "req-frontier-wu02-cycle-2", "--scope-digest", "f" * 64,
        "--source-candidate-sha", "f" * 40, "--verification-request-id", "req-verify-wu02-stale",
        "--defect-revision", "2", "--max-additional-attempts", "1",
        "--expect-revision", "15", env=env,
    ).stdout)
    assert reactivated["revision"] == 16 and reactivated["status"] == "active"
    grant_attempt = finish_correction(
        run_id, env=env, revision=16, task_id="task-correction-5", work_unit_id="WU-02",
        reason="one authorized WU-02 correction", request_id="req-verify-wu02-stale",
        scope_digest="f" * 64, candidate_sha="f" * 40,
    )
    assert grant_attempt["work_units"]["WU-02"]["corrections_used"] == 3
    assert grant_attempt["remediation"]["grants"][1]["consumed_attempts"] == 1
    extra_blocked = json.loads(run(
        "correction", "start", run_id, "--reason", "grant cannot be reused", "--task-id", "task-correction-6",
        "--work-unit-id", "WU-02", "--workspace", td, "--source-request-id", "req-verify-wu02-5",
        "--scope-digest", "f" * 64, "--source-candidate-sha", "f" * 40,
        "--expect-revision", "18", env=env, ok=False,
    ).stdout)
    assert extra_blocked["blocker"] == "BLOCKED_CORRECTION_BUDGET"

    run("unlock", run_id, "--owner", "other-owner", env=env, ok=False)
    run("unlock", run_id, "--owner", "test-owner", env=env)
    second = run(
        "init", "--run-id", "myrmex-no-progress", "--objective", "No progress test",
        "--repository-root", td, "--mode", "autonomous", "--scope", "narrow",
        env=env,
    ).stdout.strip()
    run(
        "defects", second, "--work-unit-id", "WU-no-progress", "--verification-request-id", "req-no-progress-1",
        "--candidate-sha", "1" * 40, "--scope-digest", "1" * 64, "--defects-json", '["same defect"]',
        "--expect-revision", "0", env=env,
    )
    unchanged = json.loads(run(
        "defects", second, "--work-unit-id", "WU-no-progress", "--verification-request-id", "req-no-progress-2",
        "--candidate-sha", "2" * 40, "--scope-digest", "2" * 64,
        "--remaining-json", '["same defect"]', "--expect-revision", "1", env=env,
    ).stdout)
    assert unchanged["no_progress_cycles"] == 1
    blocked_progress = json.loads(run(
        "defects", second, "--work-unit-id", "WU-no-progress", "--verification-request-id", "req-no-progress-3",
        "--candidate-sha", "3" * 40, "--scope-digest", "3" * 64,
        "--remaining-json", '["same defect"]', "--expect-revision", "2",
        env=env, ok=False,
    ).stdout)
    assert blocked_progress["blocker"] == "BLOCKED_NO_PROGRESS"
    run(
        "correction", "authorize", second, "--work-unit-id", "WU-no-progress", "--authority", "verifier",
        "--request-id", "req-no-progress-3", "--scope-digest", "3" * 64,
        "--source-candidate-sha", "3" * 40, "--max-additional-attempts", "1",
        "--expect-revision", "3", env=env, ok=False,
    )

    # A configured run-wide ceiling remains a separate cost circuit breaker;
    # a WU authorization cannot clear it.
    capped = run(
        "init", "--run-id", "myrmex-global-correction-cap", "--objective", "Global correction cap",
        "--repository-root", td, "--mode", "autonomous", "--scope", "narrow", "--max-total-corrections", "1",
        env=env,
    ).stdout.strip()
    run(
        "correction", "start", capped, "--reason", "WU one", "--task-id", "task-cap-1",
        "--work-unit-id", "WU-01", "--workspace", td, "--source-request-id", "req-cap-1",
        "--scope-digest", "4" * 64, "--source-candidate-sha", "4" * 40, "--expect-revision", "0", env=env,
    )
    global_blocked = json.loads(run(
        "correction", "start", capped, "--reason", "WU two", "--task-id", "task-cap-2",
        "--work-unit-id", "WU-02", "--workspace", td, "--source-request-id", "req-cap-2",
        "--scope-digest", "5" * 64, "--source-candidate-sha", "5" * 40, "--expect-revision", "1", env=env, ok=False,
    ).stdout)
    assert global_blocked["blocker"] == "BLOCKED_TOTAL_CORRECTION_BUDGET"
    run(
        "correction", "authorize", capped, "--work-unit-id", "WU-02", "--authority", "frontier",
        "--request-id", "req-cap-authorize", "--scope-digest", "5" * 64,
        "--source-candidate-sha", "5" * 40, "--max-additional-attempts", "1",
        "--expect-revision", "2", env=env, ok=False,
    )

    # A configured agent delegates without an API key visible to the parent;
    # only a real provider error is an execution failure and evidence is redacted.
    provider = run(
        "init", "--run-id", "myrmex-provider-outcomes", "--objective", "Provider outcomes",
        "--repository-root", td, "--mode", "autonomous", "--scope", "narrow", env=env,
    ).stdout.strip()
    run(
        "delegation-preflight", provider, "--agent", "myrmex-scout", "--role", "scout", "--reason", "configured routing",
        "--task-id", "credential-hidden", "--work-unit-id", "WU-provider", "--workspace", td,
        "--expect-revision", "0", env=env,
    )
    informational = json.loads(run(
        "delegation", provider, "--agent", "myrmex-scout", "--role", "scout", "--reason", "configured routing",
        "--task-id", "credential-hidden", "--work-unit-id", "WU-provider", "--workspace", td,
        "--status", "success", "--outcome", "CREDENTIAL_NOT_VISIBLE_TO_ORCHESTRATOR", "--expect-revision", "1", env=env,
    ).stdout)
    assert informational["status"] == "active"
    run(
        "delegation-preflight", provider, "--agent", "myrmex-scout", "--role", "scout", "--reason", "Task returned provider error",
        "--task-id", "provider-error", "--work-unit-id", "WU-provider-error", "--workspace", td,
        "--expect-revision", "2", env=env,
    )
    failed_provider = json.loads(run(
        "delegation", provider, "--agent", "myrmex-scout", "--role", "scout", "--reason", "Task returned provider error",
        "--task-id", "provider-error", "--work-unit-id", "WU-provider-error", "--workspace", td,
        "--status", "failed", "--outcome", "PROVIDER_INVOCATION_FAILED",
        "--evidence-json", '{"message":"Bearer abcdefghijklmnopqrstuvwxyz","api_key":"must-not-persist"}', "--expect-revision", "3", env=env,
    ).stdout)
    evidence = failed_provider["delegation_ledger"][-1]["evidence"]
    assert evidence["api_key"] == "[REDACTED]" and "Bearer" not in evidence["message"]
    explicitly_blocked = json.loads(run(
        "transition", provider, "--expect-revision", "4", "--to-phase", "blocked",
        "--blocker", "HUMAN_DECISION_REQUIRED", "--reason", "an explicit human decision is required", env=env,
    ).stdout)
    assert explicitly_blocked["status"] == "blocked" and explicitly_blocked["blocker"] == "HUMAN_DECISION_REQUIRED"
    unresolved = run(
        "init", "--run-id", "myrmex-unresolved-agent", "--objective", "Unresolved agent", "--repository-root", td,
        "--mode", "autonomous", "--scope", "narrow", env=env,
    ).stdout.strip()
    blocked_agent = json.loads(run(
        "delegation-preflight", unresolved, "--agent", "myrmex-worker", "--role", "writer", "--reason", "resolver blocked",
        "--task-id", "unresolved-task", "--work-unit-id", "WU-unresolved", "--workspace", td, "--expect-revision", "0", env=env,
    ).stdout)
    blocked_agent = json.loads(run(
        "delegation", unresolved, "--agent", "myrmex-worker", "--role", "writer", "--reason", "resolver blocked",
        "--task-id", "unresolved-task", "--work-unit-id", "WU-unresolved", "--workspace", td,
        "--status", "blocked", "--outcome", "AGENT_MODEL_UNRESOLVED", "--expect-revision", "1", env=env,
    ).stdout)
    assert blocked_agent["status"] == "blocked" and blocked_agent["blocker"] == "AGENT_MODEL_UNRESOLVED"

    blocked_result = run(
        "init", "--run-id", "myrmex-blocked-terminal-result", "--objective", "Blocked terminal result",
        "--repository-root", td, "--mode", "autonomous", "--scope", "narrow", env=env,
    ).stdout.strip()
    run(
        "delegation-preflight", blocked_result, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "result arrives after blocker", "--task-id", "blocked-task", "--work-unit-id", "WU-blocked",
        "--workspace", td, "--expect-revision", "0", env=env,
    )
    run(
        "transition", blocked_result, "--expect-revision", "1", "--to-phase", "blocked",
        "--blocker", "HUMAN_DECISION_REQUIRED", "--reason", "block before child returns", env=env,
    )
    blocked_terminal = json.loads(run(
        "delegation", blocked_result, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "child returned", "--task-id", "blocked-task", "--work-unit-id", "WU-blocked",
        "--workspace", td, "--status", "success", "--expect-revision", "2", env=env,
    ).stdout)
    assert blocked_terminal["revision"] == 3 and blocked_terminal["status"] == "blocked"
    assert blocked_terminal["pending_operations"][0]["status"] == "confirmed"
    blocked_state_path = Path(td) / "state" / "runs" / blocked_result / "state.json"
    blocked_events_path = blocked_state_path.parent / "events.jsonl"
    blocked_state_before_replay = blocked_state_path.read_bytes()
    blocked_events_before_replay = blocked_events_path.read_bytes()
    blocked_replay = json.loads(run(
        "delegation", blocked_result, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "replay after blocker", "--task-id", "blocked-task", "--work-unit-id", "WU-blocked",
        "--workspace", td, "--status", "success", "--expect-revision", "99", env=env,
    ).stdout)
    assert blocked_replay["revision"] == 3
    assert blocked_state_path.read_bytes() == blocked_state_before_replay
    assert blocked_events_path.read_bytes() == blocked_events_before_replay

    terminal_result = run(
        "init", "--run-id", "myrmex-cancelled-terminal-result", "--objective", "Cancelled terminal result",
        "--repository-root", td, "--mode", "autonomous", "--scope", "narrow", env=env,
    ).stdout.strip()
    run(
        "delegation-preflight", terminal_result, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "result arrives after cancellation", "--task-id", "cancelled-task", "--work-unit-id", "WU-cancelled",
        "--workspace", td, "--expect-revision", "0", env=env,
    )
    run("cancel", terminal_result, "--reason", "parent cancelled", "--expect-revision", "1", env=env)
    cancelled_terminal = json.loads(run(
        "delegation", terminal_result, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "child returned after cancellation", "--task-id", "cancelled-task", "--work-unit-id", "WU-cancelled",
        "--workspace", td, "--status", "success", "--expect-revision", "2", env=env,
    ).stdout)
    assert cancelled_terminal["revision"] == 3 and cancelled_terminal["status"] == "cancelled"
    terminal_state_path = Path(td) / "state" / "runs" / terminal_result / "state.json"
    terminal_events_path = terminal_state_path.parent / "events.jsonl"
    terminal_state_before_replay = terminal_state_path.read_bytes()
    terminal_events_before_replay = terminal_events_path.read_bytes()
    cancelled_replay = json.loads(run(
        "delegation", terminal_result, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "replay after cancellation", "--task-id", "cancelled-task", "--work-unit-id", "WU-cancelled",
        "--workspace", td, "--status", "success", "--expect-revision", "99", env=env,
    ).stdout)
    assert cancelled_replay["revision"] == 3
    assert terminal_state_path.read_bytes() == terminal_state_before_replay
    assert terminal_events_path.read_bytes() == terminal_events_before_replay

    # Join records every task before launch, accepts terminal results in any
    # order, consolidates once, and cannot advance the next gate twice.
    joined = run(
        "init", "--run-id", "myrmex-delegation-join", "--objective", "Join test", "--repository-root", td,
        "--mode", "autonomous", "--scope", "narrow", env=env,
    ).stdout.strip()
    run("delegation-batch", joined, "start", "--batch-id", "scouts", "--task-ids-json", '["task-1","task-2","task-3"]', "--expect-revision", "0", env=env)
    collected = json.loads(run(
        "delegation-batch", joined, "collect", "--batch-id", "scouts", "--expect-revision", "1",
        "--results-json", '[{"task_id":"task-3","status":"success","payload":{"claims":[{"subject":"topology","value":"linear"}]}},{"task_id":"task-1","status":"failed","payload":{"summary":"read-only failure"}},{"task_id":"task-2","status":"success","payload":{"claims":[{"subject":"topology","value":"linear"}]}}]',
        env=env,
    ).stdout)
    assert collected["phase"] == "consolidating-evidence" and collected["delegation_batches"][0]["status"] == "consolidating-evidence"
    proceeded = json.loads(run(
        "delegation-batch", joined, "proceed", "--batch-id", "scouts", "--next-phase", "verifying", "--expect-revision", "2", env=env,
    ).stdout)
    assert proceeded["phase"] == "verifying" and proceeded["delegation_batches"][0]["status"] == "proceeded"
    run("delegation-batch", joined, "proceed", "--batch-id", "scouts", "--next-phase", "verifying", "--expect-revision", "3", env=env, ok=False)

    missing = run(
        "init", "--run-id", "myrmex-join-missing", "--objective", "Missing join result", "--repository-root", td,
        "--mode", "autonomous", "--scope", "narrow", env=env,
    ).stdout.strip()
    run("delegation-batch", missing, "start", "--batch-id", "scouts", "--task-ids-json", '["task-1","task-2"]', "--expect-revision", "0", env=env)
    recovery = json.loads(run(
        "delegation-batch", missing, "collect", "--batch-id", "scouts", "--recover-missing", "--expect-revision", "1",
        "--results-json", '[{"task_id":"task-1","status":"success","payload":{}}]', env=env, ok=False,
    ).stdout)
    assert recovery["status"] == "active" and recovery["delegation_batches"][0]["missing_task_ids"] == ["task-2"]
    missing_blocked = json.loads(run(
        "delegation-batch", missing, "collect", "--batch-id", "scouts", "--expect-revision", "2",
        "--results-json", '[{"task_id":"task-1","status":"success","payload":{}}]', env=env, ok=False,
    ).stdout)
    assert missing_blocked["blocker"] == "BLOCKED_MISSING_DELEGATION_RESULT"
    late_result_recovered = json.loads(run(
        "delegation-batch", missing, "collect", "--batch-id", "scouts", "--expect-revision", "3",
        "--results-json", '[{"task_id":"task-1","status":"success","payload":{}},{"task_id":"task-2","status":"success","payload":{}}]', env=env,
    ).stdout)
    assert late_result_recovered["status"] == "active" and late_result_recovered["phase"] == "consolidating-evidence"

    contradiction = run(
        "init", "--run-id", "myrmex-join-conflict", "--objective", "Contradictory join result", "--repository-root", td,
        "--mode", "autonomous", "--scope", "narrow", env=env,
    ).stdout.strip()
    run("delegation-batch", contradiction, "start", "--batch-id", "scouts", "--task-ids-json", '["task-1","task-2"]', "--expect-revision", "0", env=env)
    conflict = json.loads(run(
        "delegation-batch", contradiction, "collect", "--batch-id", "scouts", "--expect-revision", "1",
        "--results-json", '[{"task_id":"task-1","status":"success","payload":{"claims":[{"subject":"base","value":"a"}]}},{"task_id":"task-2","status":"success","payload":{"claims":[{"subject":"base","value":"b"}]}}]', env=env, ok=False,
    ).stdout)
    assert conflict["blocker"] == "BLOCKED_CONTRADICTORY_DELEGATION_RESULTS"

    unrelated_blocker = run(
        "init", "--run-id", "myrmex-batch-unrelated-blocker", "--objective", "Batch blocker scope",
        "--repository-root", td, "--mode", "autonomous", "--scope", "narrow", env=env,
    ).stdout.strip()
    run(
        "delegation-batch", unrelated_blocker, "start", "--batch-id", "scouts", "--task-ids-json", '["task-1"]',
        "--expect-revision", "0", env=env,
    )
    run(
        "transition", unrelated_blocker, "--expect-revision", "1", "--to-phase", "blocked",
        "--blocker", "HUMAN_DECISION_REQUIRED", "--reason", "a separate gate is pending", env=env,
    )
    run(
        "delegation-batch", unrelated_blocker, "collect", "--batch-id", "scouts", "--expect-revision", "2",
        "--results-json", '[{"task_id":"task-1","status":"success","payload":{}}]', env=env, ok=False,
    )
    assert json.loads(run("show", unrelated_blocker, env=env).stdout)["blocker"] == "HUMAN_DECISION_REQUIRED"

    # Recovery is a read-only derivation: it chooses exactly one next action
    # from durable operation state before any external transport is repeated.
    recovery_run = run(
        "init", "--run-id", "myrmex-recovery-ledger", "--objective", "Recovery ledger",
        "--repository-root", td, "--mode", "autonomous", "--scope", "narrow", env=env,
    ).stdout.strip()
    recovery_path = Path(td) / "state" / "runs" / recovery_run / "state.json"
    recovery_before = recovery_path.read_bytes()
    initial_reconcile = json.loads(run("reconcile", recovery_run, env=env).stdout)
    assert initial_reconcile["action"] == "CONTINUE_PHASE"
    assert recovery_path.read_bytes() == recovery_before
    frontier_intent = json.loads(run(
        "frontier", recovery_run, "start", "--request-id", "req-frontier-ledger-1", "--task-id", "frontier-task-1",
        "--message-id", "msg-frontier-ledger-1", "--intent-json", '{"purpose":"plan"}',
        "--expect-revision", "0", env=env,
    ).stdout)
    frontier_operation = frontier_intent["pending_operations"][0]["operation_id"]
    assert json.loads(run("reconcile", recovery_run, env=env).stdout)["action"] == "RECOVER_FRONTIER_EXCHANGE"
    run(
        "operation", recovery_run, "observe", "--operation-id", frontier_operation,
        "--effect-json", '{"transport":"browser","transport_status":"success","frontier_decision":"ACCEPT","request_id":"req-frontier-ledger-1","message_id":"msg-frontier-ledger-1"}',
        "--expect-revision", "1", env=env,
    )
    assert json.loads(run("reconcile", recovery_run, env=env).stdout)["action"] == "WAIT_FRONTIER"
    run(
        "operation", recovery_run, "receipt", "--operation-id", frontier_operation,
        "--receipt-json", '{"status":"PLAN_RECEIVED","transport_status":"success","frontier_decision":"ACCEPT","request_id":"req-frontier-ledger-1","message_id":"msg-frontier-ledger-1"}',
        "--expect-revision", "2", env=env,
    )
    # Re-observing an already-discovered effect is idempotent and does not
    # regress a receipt-recorded operation or consume a revision.
    same_effect = json.loads(run(
        "operation", recovery_run, "observe", "--operation-id", frontier_operation,
        "--effect-json", '{"transport":"browser","transport_status":"success","frontier_decision":"ACCEPT","request_id":"req-frontier-ledger-1","message_id":"msg-frontier-ledger-1"}',
        "--expect-revision", "3", env=env,
    ).stdout)
    assert same_effect["revision"] == 3 and same_effect["pending_operations"][0]["status"] == "receipt-recorded"
    frontier_confirmed = json.loads(run(
        "operation", recovery_run, "confirm", "--operation-id", frontier_operation,
        "--status", "confirmed", "--reason", "Frontier response was verified", "--expect-revision", "3", env=env,
    ).stdout)
    assert frontier_confirmed["revision"] == 4
    # A terminal Frontier operation no longer leaves the legacy task ID as a
    # permanent direct-route blocker, but direct-only still rejects new starts.
    terminal_frontier_route = json.loads(run(
        "route", "set", recovery_run, "--policy", "direct-only", "--authority", "user",
        "--request-id", "req-user-direct-after-frontier", "--expect-revision", "4", env=env,
    ).stdout)
    assert terminal_frontier_route["revision"] == 5
    run(
        "frontier", recovery_run, "start", "--request-id", "req-frontier-forbidden", "--task-id", "forbidden-task",
        "--expect-revision", "5", env=env, ok=False,
    )
    run(
        "frontier", direct, "start", "--request-id", "req-frontier-direct", "--task-id", "direct-task",
        "--expect-revision", "1", env=env, ok=False,
    )

    # A Task launch persists work-unit, task, workspace, and operation identity
    # first.  A normal terminal delegation report then closes that operation so
    # recovery and complete cannot redispatch the same child.
    no_preflight = run(
        "init", "--run-id", "myrmex-no-preflight", "--objective", "No preflight", "--repository-root", td,
        "--mode", "autonomous", "--scope", "narrow", env=env,
    ).stdout.strip()
    run(
        "delegation", no_preflight, "--agent", "myrmex-worker", "--role", "writer", "--reason", "must reject",
        "--task-id", "no-preflight-task", "--work-unit-id", "WU-no-preflight", "--workspace", td,
        "--status", "success", "--expect-revision", "0", env=env, ok=False,
    )
    no_preflight_state = json.loads(run("show", no_preflight, env=env).stdout)
    assert no_preflight_state["revision"] == 0 and no_preflight_state["delegation_ledger"] == []
    missing_correction_workspace = run(
        "init", "--run-id", "myrmex-correction-workspace", "--objective", "Correction workspace", "--repository-root", td,
        "--mode", "autonomous", "--scope", "narrow", env=env,
    ).stdout.strip()
    run(
        "correction", "start", missing_correction_workspace, "--reason", "missing workspace", "--task-id", "missing-workspace",
        "--work-unit-id", "WU-missing-workspace", "--source-request-id", "req-missing-workspace",
        "--scope-digest", "a" * 64, "--source-candidate-sha", "a" * 40, "--expect-revision", "0", env=env, ok=False,
    )
    assert json.loads(run("show", missing_correction_workspace, env=env).stdout)["revision"] == 0
    preflight_run = run(
        "init", "--run-id", "myrmex-delegation-preflight", "--objective", "Delegation preflight",
        "--repository-root", td, "--mode", "autonomous", "--scope", "narrow", env=env,
    ).stdout.strip()
    run(
        "delegation-preflight", preflight_run, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "bad workspace must not launch", "--task-id", "task-preflight-bad", "--work-unit-id", "WU-preflight",
        "--workspace", str(Path(td) / "does-not-exist"), "--expect-revision", "0", env=env, ok=False,
    )
    preflight = json.loads(run(
        "delegation-preflight", preflight_run, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "bounded implementation", "--task-id", "task-preflight-1", "--work-unit-id", "WU-preflight",
        "--workspace", td, "--expect-revision", "0", env=env,
    ).stdout)
    preflight_operation = preflight["pending_operations"][0]["operation_id"]
    assert preflight["delegation_ledger"][0]["operation_id"] == preflight_operation
    concurrent_writer = run(
        "delegation-preflight", preflight_run, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "must not overlap same WU", "--task-id", "task-preflight-2", "--work-unit-id", "WU-preflight",
        "--workspace", td, "--expect-revision", "1", env=env, ok=False,
    )
    assert "BLOCKED_CONCURRENT_WRITER_WORK_UNIT" in concurrent_writer.stderr
    task_clash = run(
        "delegation-preflight", preflight_run, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "must not reuse live task", "--task-id", "task-preflight-1", "--work-unit-id", "WU-other",
        "--workspace", td, "--expect-revision", "1", env=env, ok=False,
    )
    assert "BLOCKED_DELEGATION_TASK_ALREADY_ACTIVE" in task_clash.stderr
    run(
        "operation", preflight_run, "intent", "--kind", "delegation", "--idempotency-key", "task:alternate",
        "--intent-json", '{"task_id":"alternate"}', "--expect-revision", "1", env=env, ok=False,
    )
    assert json.loads(run("reconcile", preflight_run, env=env).stdout)["action"] == "COLLECT_DELEGATIONS"
    run("complete", preflight_run, "--message", "must reject open work", "--expect-revision", "1", env=env, ok=False)
    task_result = json.loads(run(
        "delegation", preflight_run, "--agent", "myrmex-worker", "--role", "writer", "--reason", "Task returned",
        "--task-id", "task-preflight-1", "--work-unit-id", "WU-preflight", "--workspace", td, "--status", "success",
        "--evidence-json", '{"tests":"passed"}', "--expect-revision", "1", env=env,
    ).stdout)
    assert task_result["pending_operations"][0]["status"] == "confirmed"
    assert json.loads(run("reconcile", preflight_run, env=env).stdout)["action"] != "COLLECT_DELEGATIONS"
    closed_wu = json.loads(run(
        "work-unit", preflight_run, "complete", "--work-unit-id", "WU-preflight",
        "--evidence-json", '{"verification":"passed"}', "--expect-revision", "2", env=env,
    ).stdout)
    assert closed_wu["work_units"]["WU-preflight"]["status"] == "complete"
    preflight_complete = json.loads(run(
        "complete", preflight_run, "--message", "bounded work verified", "--expect-revision", "3", env=env,
    ).stdout)
    assert preflight_complete["state"]["status"] == "dormant"

    # Explicit delivery gates reject partial/unknown CI results rather than
    # allowing a confirmed side effect to masquerade as successful completion.
    incomplete_delivery = run(
        "init", "--run-id", "myrmex-incomplete-delivery", "--objective", "Incomplete delivery",
        "--repository-root", td, "--mode", "autonomous", "--scope", "narrow", env=env,
    ).stdout.strip()
    ci_intent = json.loads(run(
        "operation", incomplete_delivery, "intent", "--kind", "ci", "--idempotency-key", "ci:run-1",
        "--intent-json", '{"required":true}', "--expect-revision", "0", env=env,
    ).stdout)
    ci_operation = ci_intent["pending_operations"][0]["operation_id"]
    run("complete", incomplete_delivery, "--message", "pending CI", "--expect-revision", "1", env=env, ok=False)
    run(
        "operation", incomplete_delivery, "observe", "--operation-id", ci_operation,
        "--effect-json", '{"job":"ci"}', "--expect-revision", "1", env=env,
    )
    run(
        "operation", incomplete_delivery, "receipt", "--operation-id", ci_operation,
        "--receipt-json", '{"status":"partial"}', "--expect-revision", "2", env=env,
    )
    run(
        "operation", incomplete_delivery, "confirm", "--operation-id", ci_operation,
        "--status", "confirmed", "--reason", "remote job ended", "--expect-revision", "3", env=env,
    )
    incomplete_ci = run(
        "complete", incomplete_delivery, "--message", "partial CI", "--expect-revision", "4", env=env, ok=False,
    )
    assert "required ci operation" in incomplete_ci.stderr

    # Delivery is an ordered typed protocol: a confirmed approved tracking
    # issue is the only identity that can authorize a PR intent, and the PR
    # body generator derives its link from that persisted receipt.
    delivery_run = run(
        "init", "--run-id", "myrmex-delivery-flow", "--objective", "Delivery flow",
        "--repository-root", td, "--mode", "autonomous", "--scope", "narrow", env=env,
    ).stdout.strip()
    issue_body = Path(td) / "tracking-body.md"
    issue_body.write_text("Authorized tracking scope\n")
    issue_receipt = Path(td) / "tracking-receipt.json"
    policy_result = subprocess.run(
        [
            "python3", str(POLICY_RESOLVER), "--repository-root", td, "--mode", "autonomous",
            "--installation-profile", str(ROOT / "profiles" / "myrmex-defaults.json"),
        ], capture_output=True, text=True, timeout=20,
    )
    assert policy_result.returncode == 0, policy_result.stderr
    delivery_policy = json.loads(policy_result.stdout)
    tracking_policy = delivery_policy["delivery"]["tracking_issue"]
    tracking_intent = {
        "required": True,
        "repo": "acme/myrmex",
        "title": "tracking: delivery flow",
        "body_file": str(issue_body),
        "receipt_file": str(issue_receipt),
        "objective_id": "delivery-objective",
        "scope_digest": "a" * 64,
        "approval_marker": "status:approved",
        "reuse_matching_approved": tracking_policy["reuse_matching_approved"],
        "create_when_missing": tracking_policy["create_when_missing"],
        "ask_on_ambiguous_match": tracking_policy["ask_on_ambiguous_match"],
        "creation_policy": "authorized",
        "ensure_approval": True,
        "policy_digest": delivery_policy["policy_digest"],
        "policy": delivery_policy,
    }
    missing_policy_intent = dict(tracking_intent)
    missing_policy_intent.pop("policy")
    missing_policy_intent.pop("policy_digest")
    missing_policy = run(
        "operation", delivery_run, "intent", "--kind", "tracking_issue",
        "--idempotency-key", "tracking-issue:missing-policy", "--intent-json", json.dumps(missing_policy_intent),
        "--expect-revision", "0", env=env, ok=False,
    )
    assert "requires a resolved delivery policy" in missing_policy.stderr
    bad_digest_intent = dict(tracking_intent)
    bad_digest_intent["policy_digest"] = "b" * 64
    bad_digest = run(
        "operation", delivery_run, "intent", "--kind", "tracking_issue",
        "--idempotency-key", "tracking-issue:bad-policy-digest", "--intent-json", json.dumps(bad_digest_intent),
        "--expect-revision", "0", env=env, ok=False,
    )
    assert "TRACKING_ISSUE_POLICY_DIGEST_MISMATCH" in bad_digest.stderr
    deny_config = Path(td) / "deny-policy.json"
    deny_config.write_text(json.dumps({"delivery": {"tracking_issue": {"create_when_missing": False}}}))
    deny_result = subprocess.run(
        [
            "python3", str(POLICY_RESOLVER), "--repository-root", td, "--mode", "autonomous",
            "--installation-profile", str(ROOT / "profiles" / "myrmex-defaults.json"),
            "--repository-config", str(deny_config),
        ], capture_output=True, text=True, timeout=20,
    )
    assert deny_result.returncode == 0, deny_result.stderr
    deny_policy = json.loads(deny_result.stdout)
    deny_intent = dict(tracking_intent)
    deny_intent.update({"policy": deny_policy, "policy_digest": deny_policy["policy_digest"]})
    deny_bypass = run(
        "operation", delivery_run, "intent", "--kind", "tracking_issue",
        "--idempotency-key", "tracking-issue:deny-bypass", "--intent-json", json.dumps(deny_intent),
        "--expect-revision", "0", env=env, ok=False,
    )
    assert "TRACKING_ISSUE_POLICY_INTENT_MISMATCH" in deny_bypass.stderr
    forged_allow_policy = json.loads(json.dumps(deny_policy))
    forged_allow_policy["delivery"]["tracking_issue"]["create_when_missing"] = True
    forged_allow_policy["decision"] = {
        "on_missing_tracking_issue": "create",
        "on_ambiguous_match": "ask",
        "creation_policy": "authorized",
    }
    forged_without_digest = {key: value for key, value in forged_allow_policy.items() if key != "policy_digest"}
    forged_allow_policy["policy_digest"] = hashlib.sha256(json.dumps(
        forged_without_digest, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    forged_allow_intent = dict(tracking_intent)
    forged_allow_intent.update({
        "create_when_missing": True,
        "policy": forged_allow_policy,
        "policy_digest": forged_allow_policy["policy_digest"],
    })
    forged_allow = run(
        "operation", delivery_run, "intent", "--kind", "tracking_issue",
        "--idempotency-key", "tracking-issue:forged-allow", "--intent-json", json.dumps(forged_allow_intent),
        "--expect-revision", "0", env=env, ok=False,
    )
    assert "TRACKING_ISSUE_POLICY_RESOLUTION_MISMATCH" in forged_allow.stderr
    premature_pr = run(
        "operation", delivery_run, "intent", "--kind", "pull_request",
        "--idempotency-key", "pull-request:premature", "--intent-json", json.dumps({
            "required": True, "repo": "acme/myrmex", "head": "fix/delivery", "base": "main",
            "title": "fix: delivery", "body_file": str(issue_body), "receipt_file": str(issue_receipt),
            "tracking_issue_operation_id": "op-" + "b" * 24,
        }), "--expect-revision", "0", env=env, ok=False,
    )
    assert "operation not found" in premature_pr.stderr
    tracking = json.loads(run(
        "operation", delivery_run, "intent", "--kind", "tracking_issue",
        "--idempotency-key", "tracking-issue:delivery-objective:" + "a" * 64,
        "--intent-json", json.dumps(tracking_intent), "--expect-revision", "0", env=env,
    ).stdout)
    tracking_operation = tracking["pending_operations"][-1]
    tracking_effect = {
        "status": "ISSUE_APPROVED", "repo": "acme/myrmex", "number": 17,
        "url": "https://github.com/acme/myrmex/issues/17",
        "identity_marker": tracking_operation["intent"]["identity_marker"],
        "approval_marker": "status:approved", "approved": True,
    }
    run(
        "operation", delivery_run, "observe", "--operation-id", tracking_operation["operation_id"],
        "--effect-json", json.dumps(tracking_effect), "--expect-revision", "1", env=env,
    )
    run(
        "operation", delivery_run, "receipt", "--operation-id", tracking_operation["operation_id"],
        "--receipt-json", json.dumps(tracking_effect), "--expect-revision", "2", env=env,
    )
    approved = json.loads(run(
        "operation", delivery_run, "confirm", "--operation-id", tracking_operation["operation_id"],
        "--status", "confirmed", "--reason", "tracking issue approval receipt verified",
        "--expect-revision", "3", env=env,
    ).stdout)
    assert approved["pending_operations"][0]["status"] == "confirmed"
    pr_template = Path(td) / "pr-template.md"
    pr_template.write_text("Summary of the change\n")
    pr_body = Path(td) / "pr-body.md"
    body_artifact = json.loads(run(
        "delivery", delivery_run, "pr-body", "--tracking-operation-id", tracking_operation["operation_id"],
        "--template-file", str(pr_template), "--output-file", str(pr_body), env=env,
    ).stdout)
    issue_url = "https://github.com/acme/myrmex/issues/17"
    assert body_artifact["tracking_issue_url"] == issue_url
    assert issue_url in pr_body.read_text()
    body_before_replay = pr_body.read_bytes()
    replay_body = json.loads(run(
        "delivery", delivery_run, "pr-body", "--tracking-issue-operation-id", tracking_operation["operation_id"],
        "--body-file", str(pr_template), "--output-file", str(pr_body), env=env,
    ).stdout)
    assert replay_body["body_digest"] == body_artifact["body_digest"] and pr_body.read_bytes() == body_before_replay
    pr_intent = {
        "required": True, "repo": "acme/myrmex", "head": "fix/delivery", "base": "main",
        "title": "fix: delivery", "body_file": str(pr_body), "receipt_file": str(Path(td) / "pr-receipt.json"),
        "tracking_issue_operation_id": tracking_operation["operation_id"],
        "body_digest": body_artifact["body_digest"],
    }
    prefix_body = Path(td) / "prefix-pr-body.md"
    prefix_body.write_text("See https://github.com/acme/myrmex/issues/170 for context.\n")
    prefix_intent = dict(pr_intent)
    prefix_intent.update({
        "body_file": str(prefix_body),
        "body_digest": hashlib.sha256(prefix_body.read_bytes()).hexdigest(),
    })
    prefix_rejected = run(
        "operation", delivery_run, "intent", "--kind", "pull_request",
        "--idempotency-key", "pull-request:prefix-issue-url", "--intent-json", json.dumps(prefix_intent),
        "--expect-revision", "4", env=env, ok=False,
    )
    assert "PULL_REQUEST_BODY_MISSING_TRACKING_ISSUE" in prefix_rejected.stderr
    stale_marker_body = Path(td) / "stale-marker-pr-body.md"
    stale_marker_body.write_text("<!-- myrmex:tracking-issue-url=https://github.com/acme/myrmex/issues/170 -->\n")
    stale_marker_intent = dict(pr_intent)
    stale_marker_intent.update({
        "body_file": str(stale_marker_body),
        "body_digest": hashlib.sha256(stale_marker_body.read_bytes()).hexdigest(),
    })
    stale_marker_rejected = run(
        "operation", delivery_run, "intent", "--kind", "pull_request",
        "--idempotency-key", "pull-request:stale-issue-marker", "--intent-json", json.dumps(stale_marker_intent),
        "--expect-revision", "4", env=env, ok=False,
    )
    assert "PULL_REQUEST_BODY_TRACKING_ISSUE_MISMATCH" in stale_marker_rejected.stderr
    missing_body_digest = dict(pr_intent)
    missing_body_digest.pop("body_digest")
    missing_digest_result = run(
        "operation", delivery_run, "intent", "--kind", "pull_request",
        "--idempotency-key", "pull-request:missing-body-digest", "--intent-json", json.dumps(missing_body_digest),
        "--expect-revision", "4", env=env, ok=False,
    )
    assert "requires body_digest" in missing_digest_result.stderr
    pr_body.write_text(pr_body.read_text() + "tampered\n")
    tampered_before_intent = run(
        "operation", delivery_run, "intent", "--kind", "pull_request",
        "--idempotency-key", "pull-request:tampered-before-intent", "--intent-json", json.dumps(pr_intent),
        "--expect-revision", "4", env=env, ok=False,
    )
    assert "PULL_REQUEST_BODY_DIGEST_MISMATCH" in tampered_before_intent.stderr
    pr_body.write_bytes(body_before_replay)
    pr_state = json.loads(run(
        "operation", delivery_run, "intent", "--kind", "pull_request",
        "--idempotency-key", "pull-request:fix/delivery:main", "--intent-json", json.dumps(pr_intent),
        "--expect-revision", "4", env=env,
    ).stdout)
    pr_operation = pr_state["pending_operations"][-1]
    replayed_pr = json.loads(run(
        "operation", delivery_run, "intent", "--kind", "pull_request",
        "--idempotency-key", "pull-request:fix/delivery:main", "--intent-json", json.dumps(pr_intent),
        "--expect-revision", "99", env=env,
    ).stdout)
    assert replayed_pr["revision"] == 5 and len(replayed_pr["pending_operations"]) == 2
    pr_effect = {
        "status": "PR_CREATED_LABEL_PENDING", "repo": "acme/myrmex", "head": "fix/delivery",
        "base": "main", "number": 23, "url": "https://github.com/acme/myrmex/pull/23",
    }
    pr_body.write_text(pr_body.read_text() + "tampered after intent\n")
    tampered_effect = run(
        "operation", delivery_run, "observe", "--operation-id", pr_operation["operation_id"],
        "--effect-json", json.dumps(pr_effect), "--expect-revision", "5", env=env, ok=False,
    )
    assert "PULL_REQUEST_BODY_DIGEST_MISMATCH" in tampered_effect.stderr
    pr_body.write_bytes(body_before_replay)
    run(
        "operation", delivery_run, "observe", "--operation-id", pr_operation["operation_id"],
        "--effect-json", json.dumps(pr_effect), "--expect-revision", "5", env=env,
    )
    mismatched_pr_receipt = {**pr_effect, "number": 24, "url": "https://github.com/acme/myrmex/pull/24", "status": "PR_CREATED"}
    mismatched_identity = run(
        "operation", delivery_run, "receipt", "--operation-id", pr_operation["operation_id"],
        "--receipt-json", json.dumps(mismatched_pr_receipt), "--expect-revision", "6", env=env, ok=False,
    )
    assert "PULL_REQUEST_EFFECT_RECEIPT_IDENTITY_MISMATCH" in mismatched_identity.stderr
    pr_body.write_text(pr_body.read_text() + "tampered before receipt\n")
    tampered_receipt = run(
        "operation", delivery_run, "receipt", "--operation-id", pr_operation["operation_id"],
        "--receipt-json", json.dumps({**pr_effect, "status": "PR_CREATED"}), "--expect-revision", "6", env=env, ok=False,
    )
    assert "PULL_REQUEST_BODY_DIGEST_MISMATCH" in tampered_receipt.stderr
    pr_body.write_bytes(body_before_replay)
    run(
        "operation", delivery_run, "receipt", "--operation-id", pr_operation["operation_id"],
        "--receipt-json", json.dumps({**pr_effect, "status": "PR_CREATED"}), "--expect-revision", "6", env=env,
    )
    confirmed_pr = json.loads(run(
        "operation", delivery_run, "confirm", "--operation-id", pr_operation["operation_id"],
        "--status", "confirmed", "--reason", "PR receipt verified", "--expect-revision", "7", env=env,
    ).stdout)
    assert [item["kind"] for item in confirmed_pr["pending_operations"]] == ["tracking_issue", "pull_request"]
    assert all(item["status"] == "confirmed" for item in confirmed_pr["pending_operations"])

    # Legacy-looking success aliases cannot satisfy either delivery gate.
    alias_revision = 8
    for index, alias in enumerate(["APPROVED", "REUSED", "SUCCESS", "CONFIRMED"]):
        alias_intent = dict(tracking_intent)
        alias_intent.update({"objective_id": f"alias-{index}", "scope_digest": ("b" + str(index)) * 32})
        alias_state = json.loads(run(
            "operation", delivery_run, "intent", "--kind", "tracking_issue",
            "--idempotency-key", f"tracking-issue:alias-{index}", "--intent-json", json.dumps(alias_intent),
            "--expect-revision", str(alias_revision), env=env,
        ).stdout)
        alias_operation = alias_state["pending_operations"][-1]
        alias_effect = dict(tracking_effect)
        alias_effect.update({"status": alias, "identity_marker": alias_operation["intent"]["identity_marker"]})
        run(
            "operation", delivery_run, "observe", "--operation-id", alias_operation["operation_id"],
            "--effect-json", json.dumps(alias_effect), "--expect-revision", str(alias_revision + 1), env=env,
        )
        run(
            "operation", delivery_run, "receipt", "--operation-id", alias_operation["operation_id"],
            "--receipt-json", json.dumps(alias_effect), "--expect-revision", str(alias_revision + 2), env=env,
        )
        alias_confirmation = run(
            "operation", delivery_run, "confirm", "--operation-id", alias_operation["operation_id"],
            "--status", "confirmed", "--reason", "alias must not approve issue",
            "--expect-revision", str(alias_revision + 3), env=env, ok=False,
        )
        assert "PULL_REQUEST_TRACKING_ISSUE_NOT_APPROVED" in alias_confirmation.stderr
        alias_revision += 3

    for index, alias in enumerate(["APPROVED", "REUSED", "SUCCESS", "CONFIRMED"]):
        alias_pr_intent = dict(pr_intent)
        alias_pr_state = json.loads(run(
            "operation", delivery_run, "intent", "--kind", "pull_request",
            "--idempotency-key", f"pull-request:alias-{index}", "--intent-json", json.dumps(alias_pr_intent),
            "--expect-revision", str(alias_revision), env=env,
        ).stdout)
        alias_pr_operation = alias_pr_state["pending_operations"][-1]
        alias_pr_effect = dict(pr_effect)
        alias_pr_effect["status"] = alias
        run(
            "operation", delivery_run, "observe", "--operation-id", alias_pr_operation["operation_id"],
            "--effect-json", json.dumps(alias_pr_effect), "--expect-revision", str(alias_revision + 1), env=env,
        )
        run(
            "operation", delivery_run, "receipt", "--operation-id", alias_pr_operation["operation_id"],
            "--receipt-json", json.dumps(alias_pr_effect), "--expect-revision", str(alias_revision + 2), env=env,
        )
        alias_pr_confirmation = run(
            "operation", delivery_run, "confirm", "--operation-id", alias_pr_operation["operation_id"],
            "--status", "confirmed", "--reason", "alias must not confirm PR",
            "--expect-revision", str(alias_revision + 3), env=env, ok=False,
        )
        assert "PULL_REQUEST_RECEIPT_NOT_SUCCESSFUL" in alias_pr_confirmation.stderr
        alias_revision += 3

    parent_gate = run(
        "init", "--run-id", "myrmex-parent-gate", "--objective", "Parent gate", "--parent-objective", "continuous objective",
        "--repository-root", td, "--mode", "autonomous", "--scope", "continuous", env=env,
    ).stdout.strip()
    parent_incomplete = run(
        "complete", parent_gate, "--message", "cannot skip parent gate", "--expect-revision", "0", env=env, ok=False,
    )
    assert "parent objective has not passed a parent gate" in parent_incomplete.stderr

    # A plain local-commit authorization is denied until a typed standing
    # parent authorization establishes governed mode.  The standing command
    # records only accepted Frontier WU evidence and is idempotent.
    deny_commit = run(
        "init", "--run-id", "myrmex-commit-policy-deny", "--objective", "deny commit policy",
        "--repository-root", td, "--branch", "main", "--mode", "autonomous", "--scope", "narrow",
        "--commit-policy", "deny", "--push-policy", "deny", env=env,
    ).stdout.strip()
    denied_authorization = run(
        "authorization", deny_commit, "create", "--authority", "user", "--request-id", "ordinary-denied",
        "--repository-root", td, "--branch", "main", "--expected-head", "b" * 40,
        "--allowed-path", "ordinary.txt", "--message", "feat: ordinary", "--expect-revision", "0", env=env, ok=False,
    )
    assert "typed governed commit policy" in denied_authorization.stderr
    assert json.loads(run("show", deny_commit, env=env).stdout)["revision"] == 0

    governed_parent = run(
        "init", "--run-id", "myrmex-governed-parent", "--objective", "governed parent",
        "--parent-objective", "standing governed parent", "--repository-root", td, "--branch", "main",
        "--mode", "autonomous", "--scope", "continuous", "--commit-policy", "authorized", "--push-policy", "deny", env=env,
    ).stdout.strip()
    legacy_authorization = json.loads(run(
        "authorization", governed_parent, "create", "--authority", "user", "--request-id", "legacy-before-governed",
        "--repository-root", td, "--branch", "main", "--expected-head", "b" * 40,
        "--allowed-path", "legacy.txt", "--message", "feat: legacy", "--expect-revision", "0", env=env,
    ).stdout)["authorizations"][0]
    run(
        "delegation-preflight", governed_parent, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "accepted WU", "--task-id", "task-governed", "--work-unit-id", "WU-governed",
        "--workspace", td, "--expect-revision", "1", env=env,
    )
    run(
        "delegation", governed_parent, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "accepted WU", "--task-id", "task-governed", "--work-unit-id", "WU-governed",
        "--workspace", td, "--status", "success", "--expect-revision", "2", env=env,
    )
    accepted_start = json.loads(run(
        "frontier", governed_parent, "start", "--request-id", "accepted-wu-request",
        "--task-id", "accepted-wu-task", "--message-id", "accepted-wu-message", "--expect-revision", "3", env=env,
    ).stdout)
    accepted_operation = accepted_start["pending_operations"][-1]["operation_id"]
    accepted_evidence = json.dumps({
        "request_id": "accepted-wu-request", "message_id": "accepted-wu-message",
        "transport_status": "success", "frontier_decision": "ACCEPT",
        "response_type": "sub_objective_complete", "work_unit_id": "WU-governed",
        "repository_root": str(Path(td).resolve()), "branch": "main", "expected_head": "b" * 40,
        "candidate_diff_sha": "a" * 64, "allowed_paths": ["governed.txt"],
        "commit_message": "feat: governed",
    })
    standing = json.loads(run(
        "frontier", governed_parent, "result", "--operation-id", accepted_operation,
        "--request-id", "accepted-wu-request", "--message-id", "accepted-wu-message",
        "--transport-status", "success", "--frontier-decision", "ACCEPT",
        "--response-type", "sub_objective_complete", "--effect-json", accepted_evidence,
        "--receipt-json", accepted_evidence, "--expect-revision", "4", env=env,
    ).stdout)
    standing = json.loads(run(
        "commit-policy", "authorize", governed_parent, "--authority", "user",
        "--source-request-id", "accepted-wu-request", "--source-operation-id", accepted_operation,
        "--work-unit-id", "WU-governed", "--expect-revision", "5", env=env,
    ).stdout)
    assert standing["revision"] == 6 and standing["commit_policy"] == "governed"
    run(
        "work-unit", governed_parent, "complete", "--work-unit-id", "WU-governed",
        "--evidence-json", '{"verification":"passed"}', "--expect-revision", "6", env=env,
    )
    assert standing["commit_policy_authorization"]["authority"] == "user"
    assert legacy_authorization["status"] == "open"
    revoked_legacy = standing["authorizations"][0]
    assert revoked_legacy["authorization_id"] == legacy_authorization["authorization_id"]
    assert revoked_legacy["status"] == "revoked" and revoked_legacy["consumed_at"] is not None
    standing_path = Path(env["MYRMEX_STATE_HOME"]) / "runs" / governed_parent / "state.json"
    standing_before = standing_path.read_bytes()
    replayed_standing = json.loads(run(
        "commit-policy", "authorize", governed_parent, "--authority", "user",
        "--source-request-id", "accepted-wu-request", "--source-operation-id", accepted_operation,
        "--work-unit-id", "WU-governed", "--expect-revision", "99", env=env,
    ).stdout)
    assert replayed_standing["revision"] == 7 and standing_path.read_bytes() == standing_before
    conflict_standing = run(
        "commit-policy", "authorize", governed_parent, "--authority", "frontier",
        "--source-request-id", "accepted-wu-request", "--source-operation-id", accepted_operation,
        "--work-unit-id", "WU-governed", "--expect-revision", "7", env=env, ok=False,
    )
    assert "human authority=user" in conflict_standing.stderr
    legacy_intent = run(
        "operation", governed_parent, "intent", "--kind", "local_commit",
        "--idempotency-key", f"local-commit:{legacy_authorization['authorization_id']}:user:legacy-before-governed",
        "--authorization-id", legacy_authorization["authorization_id"], "--intent-json", "{}",
        "--expect-revision", "7", env=env, ok=False,
    )
    assert "GOVERNED_LOCAL_COMMIT_GRANT_REQUIRED" in legacy_intent.stderr
    governed_grant = json.loads(run(
        "authorization", governed_parent, "create", "--authority", "user", "--request-id", "governed-grant",
        "--repository-root", td, "--branch", "main", "--expected-head", "b" * 40,
        "--allowed-path", "governed.txt", "--message", "feat: governed", "--work-unit-id", "WU-governed",
        "--candidate-diff-sha", "a" * 64, "--source-operation-id", accepted_operation,
        "--source-request-id", "accepted-wu-request", "--expect-revision", "7", env=env,
    ).stdout)
    assert governed_grant["authorizations"][-1]["grant_kind"] == "governed"
    patch_standing = run(
        "patch", governed_parent, "--set", "commit_policy_authorization.authority=frontier",
        "--expect-revision", "8", env=env, ok=False,
    )
    assert "protected state path" in patch_standing.stderr
    cancelled_governed = json.loads(run(
        "cancel", governed_parent, "--reason", "parent cancelled", "--expect-revision", "8", env=env,
    ).stdout)
    assert cancelled_governed["commit_policy_authorization"]["status"] == "revoked"
    run(
        "authorization", governed_parent, "create", "--authority", "user", "--request-id", "after-cancel",
        "--repository-root", td, "--branch", "main", "--expected-head", "b" * 40,
        "--allowed-path", "governed.txt", "--message", "feat: governed", "--work-unit-id", "WU-governed",
        "--candidate-diff-sha", "a" * 64, "--source-operation-id", accepted_operation,
        "--source-request-id", "accepted-wu-request", "--expect-revision", "9", env=env, ok=False,
    )

    complete_governed = run(
        "init", "--run-id", "myrmex-governed-complete", "--objective", "governed complete",
        "--parent-objective", "standing governed parent", "--repository-root", td, "--branch", "main",
        "--mode", "autonomous", "--scope", "continuous", "--commit-policy", "deny", "--push-policy", "deny", env=env,
    ).stdout.strip()
    run(
        "delegation-preflight", complete_governed, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "accepted WU", "--task-id", "task-complete", "--work-unit-id", "WU-complete",
        "--workspace", td, "--expect-revision", "0", env=env,
    )
    run(
        "delegation", complete_governed, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "accepted WU", "--task-id", "task-complete", "--work-unit-id", "WU-complete",
        "--workspace", td, "--status", "success", "--expect-revision", "1", env=env,
    )
    complete_start = json.loads(run(
        "frontier", complete_governed, "start", "--request-id", "complete-wu-request",
        "--task-id", "complete-wu-task", "--message-id", "complete-wu-message", "--expect-revision", "2", env=env,
    ).stdout)
    complete_operation = complete_start["pending_operations"][-1]["operation_id"]
    complete_evidence = json.dumps({
        "request_id": "complete-wu-request", "message_id": "complete-wu-message",
        "transport_status": "success", "frontier_decision": "ACCEPT",
        "response_type": "sub_objective_complete", "work_unit_id": "WU-complete",
    })
    run(
        "frontier", complete_governed, "result", "--operation-id", complete_operation,
        "--request-id", "complete-wu-request", "--message-id", "complete-wu-message",
        "--transport-status", "success", "--frontier-decision", "ACCEPT",
        "--response-type", "sub_objective_complete", "--effect-json", complete_evidence,
        "--receipt-json", complete_evidence, "--expect-revision", "3", env=env,
    )
    run(
        "commit-policy", "authorize", complete_governed, "--authority", "user",
        "--source-request-id", "complete-wu-request", "--source-operation-id", complete_operation,
        "--work-unit-id", "WU-complete", "--expect-revision", "4", env=env,
    )
    run(
        "work-unit", complete_governed, "complete", "--work-unit-id", "WU-complete",
        "--evidence-json", '{"verification":"passed"}', "--expect-revision", "5", env=env,
    )
    parent_complete_start = json.loads(run(
        "frontier", complete_governed, "start", "--request-id", "complete-parent-request",
        "--task-id", "complete-parent-task", "--message-id", "complete-parent-message", "--expect-revision", "6", env=env,
    ).stdout)
    parent_complete_operation = parent_complete_start["pending_operations"][-1]["operation_id"]
    parent_complete_evidence = json.dumps({
        "request_id": "complete-parent-request", "message_id": "complete-parent-message",
        "transport_status": "success", "frontier_decision": "ACCEPT",
        "response_type": "parent_objective_complete",
    })
    run(
        "frontier", complete_governed, "result", "--operation-id", parent_complete_operation,
        "--request-id", "complete-parent-request", "--message-id", "complete-parent-message",
        "--transport-status", "success", "--frontier-decision", "ACCEPT",
        "--response-type", "parent_objective_complete", "--effect-json", parent_complete_evidence,
        "--receipt-json", parent_complete_evidence, "--expect-revision", "7", env=env,
    )
    completed_governed = json.loads(run(
        "complete", complete_governed, "--message", "parent complete", "--expect-revision", "8", env=env,
    ).stdout)
    assert completed_governed["state"]["commit_policy_authorization"]["status"] == "revoked"
    run(
        "authorization", complete_governed, "create", "--authority", "user", "--request-id", "after-complete",
        "--repository-root", td, "--branch", "main", "--expected-head", "b" * 40,
        "--allowed-path", "governed.txt", "--message", "feat: governed", "--work-unit-id", "WU-complete",
        "--candidate-diff-sha", "a" * 64, "--source-operation-id", complete_operation,
        "--source-request-id", "complete-wu-request", "--expect-revision", "9", env=env, ok=False,
    )

    # A confirmed Frontier plan is not standing commit-policy evidence; only
    # SUB_OBJECTIVE_COMPLETE/ACCEPT may establish the parent authorization.
    plan_parent = run(
        "init", "--run-id", "myrmex-governed-plan-rejected", "--objective", "governed plan rejection",
        "--parent-objective", "standing governed parent", "--repository-root", td, "--branch", "main",
        "--mode", "autonomous", "--scope", "continuous", "--commit-policy", "deny", "--push-policy", "deny", env=env,
    ).stdout.strip()
    run(
        "delegation-preflight", plan_parent, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "completed WU", "--task-id", "task-plan-parent", "--work-unit-id", "WU-plan-parent",
        "--workspace", td, "--expect-revision", "0", env=env,
    )
    run(
        "delegation", plan_parent, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "completed WU", "--task-id", "task-plan-parent", "--work-unit-id", "WU-plan-parent",
        "--workspace", td, "--status", "success", "--expect-revision", "1", env=env,
    )
    run(
        "work-unit", plan_parent, "complete", "--work-unit-id", "WU-plan-parent",
        "--evidence-json", '{"verification":"passed"}', "--expect-revision", "2", env=env,
    )
    plan_started = json.loads(run(
        "frontier", plan_parent, "start", "--request-id", "plan-request",
        "--task-id", "plan-task", "--message-id", "plan-message", "--expect-revision", "3", env=env,
    ).stdout)
    plan_operation = plan_started["pending_operations"][-1]["operation_id"]
    plan = {"work_unit_id": "WU-plan", "summary": "accepted next WU"}
    plan_evidence = json.dumps({
        "request_id": "plan-request", "message_id": "plan-message",
        "transport_status": "success", "frontier_decision": "ACCEPT",
        "response_type": "plan", "proposed_plan": plan,
    })
    run(
        "frontier", plan_parent, "result", "--operation-id", plan_operation,
        "--request-id", "plan-request", "--message-id", "plan-message",
        "--transport-status", "success", "--frontier-decision", "ACCEPT", "--response-type", "plan",
        "--plan-json", json.dumps(plan), "--effect-json", plan_evidence, "--receipt-json", plan_evidence,
        "--expect-revision", "4", env=env,
    )
    run(
        "delegation-preflight", plan_parent, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "approved next WU", "--task-id", "task-plan-next", "--work-unit-id", "WU-plan",
        "--workspace", td, "--expect-revision", "5", env=env,
    )
    rejected_plan_authorization = run(
        "commit-policy", "authorize", plan_parent, "--authority", "user",
        "--source-request-id", "plan-request", "--source-operation-id", plan_operation,
        "--work-unit-id", "WU-plan", "--expect-revision", "6", env=env, ok=False,
    )
    assert "GOVERNED_AUTHORIZATION_SOURCE_NOT_ACCEPTED" in rejected_plan_authorization.stderr
    assert json.loads(run("show", plan_parent, env=env).stdout)["revision"] == 6
    run(
        "delegation", plan_parent, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "completed next WU", "--task-id", "task-plan-next", "--work-unit-id", "WU-plan",
        "--workspace", td, "--status", "success", "--expect-revision", "6", env=env,
    )
    run(
        "work-unit", plan_parent, "complete", "--work-unit-id", "WU-plan",
        "--evidence-json", '{"verification":"passed"}', "--expect-revision", "7", env=env,
    )
    parent_gate_accept = json.loads(run(
        "frontier", plan_parent, "start", "--request-id", "parent-gate-accept-request",
        "--task-id", "parent-gate-accept-task", "--message-id", "parent-gate-accept-message",
        "--expect-revision", "8", env=env,
    ).stdout)
    parent_gate_accept_operation = parent_gate_accept["pending_operations"][-1]["operation_id"]
    parent_gate_scope = {
        "repository_root": str(Path(td).resolve()), "branch": "main", "expected_head": "b" * 40,
        "candidate_diff_sha": "d" * 64, "allowed_paths": ["plan.txt"], "commit_message": "feat: plan",
    }
    parent_gate_accept_evidence = json.dumps({
        "request_id": "parent-gate-accept-request", "message_id": "parent-gate-accept-message",
        "transport_status": "success", "frontier_decision": "ACCEPT",
        "response_type": "sub_objective_complete", "work_unit_id": "WU-plan",
        **parent_gate_scope,
    })
    run(
        "frontier", plan_parent, "result", "--operation-id", parent_gate_accept_operation,
        "--request-id", "parent-gate-accept-request", "--message-id", "parent-gate-accept-message",
        "--transport-status", "success", "--frontier-decision", "ACCEPT",
        "--response-type", "sub_objective_complete", "--effect-json", parent_gate_accept_evidence,
        "--receipt-json", parent_gate_accept_evidence, "--expect-revision", "9", env=env,
    )
    parent_gate_state_path = Path(env["MYRMEX_STATE_HOME"]) / "runs" / plan_parent / "state.json"
    parent_gate_events_path = parent_gate_state_path.parent / "events.jsonl"
    parent_gate_state_before_reject = parent_gate_state_path.read_bytes()
    parent_gate_events_before_reject = parent_gate_events_path.read_bytes()
    rejected_parent_gate_accept = run(
        "commit-policy", "authorize", plan_parent, "--authority", "user",
        "--source-request-id", "parent-gate-accept-request", "--source-operation-id", parent_gate_accept_operation,
        "--work-unit-id", "WU-plan", "--expect-revision", "10", env=env, ok=False,
    )
    assert "GOVERNED_AUTHORIZATION_SOURCE_NOT_ACCEPTED" in rejected_parent_gate_accept.stderr
    assert parent_gate_state_path.read_bytes() == parent_gate_state_before_reject
    assert parent_gate_events_path.read_bytes() == parent_gate_events_before_reject

    grant_plan_parent = run(
        "init", "--run-id", "myrmex-governed-grant-plan", "--objective", "governed grant plan rejection",
        "--parent-objective", "standing governed parent", "--repository-root", td, "--branch", "main",
        "--mode", "autonomous", "--scope", "continuous", "--commit-policy", "deny", "--push-policy", "deny", env=env,
    ).stdout.strip()
    run(
        "delegation-preflight", grant_plan_parent, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "completed WU", "--task-id", "task-grant-plan-one", "--work-unit-id", "WU-grant-plan-one",
        "--workspace", td, "--expect-revision", "0", env=env,
    )
    run(
        "delegation", grant_plan_parent, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "completed WU", "--task-id", "task-grant-plan-one", "--work-unit-id", "WU-grant-plan-one",
        "--workspace", td, "--status", "success", "--expect-revision", "1", env=env,
    )
    grant_sub_started = json.loads(run(
        "frontier", grant_plan_parent, "start", "--request-id", "grant-sub-request",
        "--task-id", "grant-sub-task", "--message-id", "grant-sub-message", "--expect-revision", "2", env=env,
    ).stdout)
    grant_sub_operation = grant_sub_started["pending_operations"][-1]["operation_id"]
    grant_scope = {
        "repository_root": str(Path(td).resolve()), "branch": "main", "expected_head": "b" * 40,
        "candidate_diff_sha": "c" * 64, "allowed_paths": ["plan.txt"], "commit_message": "feat: plan",
    }
    grant_sub_evidence = json.dumps({
        "request_id": "grant-sub-request", "message_id": "grant-sub-message",
        "transport_status": "success", "frontier_decision": "ACCEPT",
        "response_type": "sub_objective_complete", "work_unit_id": "WU-grant-plan-one",
        **grant_scope,
    })
    run(
        "frontier", grant_plan_parent, "result", "--operation-id", grant_sub_operation,
        "--request-id", "grant-sub-request", "--message-id", "grant-sub-message",
        "--transport-status", "success", "--frontier-decision", "ACCEPT", "--response-type", "sub_objective_complete",
        "--effect-json", grant_sub_evidence, "--receipt-json", grant_sub_evidence, "--expect-revision", "3", env=env,
    )
    run(
        "commit-policy", "authorize", grant_plan_parent, "--authority", "user",
        "--source-request-id", "grant-sub-request", "--source-operation-id", grant_sub_operation,
        "--work-unit-id", "WU-grant-plan-one", "--expect-revision", "4", env=env,
    )
    run(
        "work-unit", grant_plan_parent, "complete", "--work-unit-id", "WU-grant-plan-one",
        "--evidence-json", '{"verification":"passed"}', "--expect-revision", "5", env=env,
    )
    grant_plan_started = json.loads(run(
        "frontier", grant_plan_parent, "start", "--request-id", "grant-plan-request",
        "--task-id", "grant-plan-task", "--message-id", "grant-plan-message", "--expect-revision", "6", env=env,
    ).stdout)
    grant_plan_operation = grant_plan_started["pending_operations"][-1]["operation_id"]
    grant_plan = {"work_unit_id": "WU-grant-plan-two", "summary": "next WU"}
    grant_plan_evidence = json.dumps({
        "request_id": "grant-plan-request", "message_id": "grant-plan-message",
        "transport_status": "success", "frontier_decision": "ACCEPT", "response_type": "plan",
        "proposed_plan": grant_plan, **grant_scope,
    })
    run(
        "frontier", grant_plan_parent, "result", "--operation-id", grant_plan_operation,
        "--request-id", "grant-plan-request", "--message-id", "grant-plan-message",
        "--transport-status", "success", "--frontier-decision", "ACCEPT", "--response-type", "plan",
        "--plan-json", json.dumps(grant_plan), "--effect-json", grant_plan_evidence,
        "--receipt-json", grant_plan_evidence, "--expect-revision", "7", env=env,
    )
    run(
        "delegation-preflight", grant_plan_parent, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "approved next WU", "--task-id", "task-grant-plan-two", "--work-unit-id", "WU-grant-plan-two",
        "--workspace", td, "--expect-revision", "8", env=env,
    )
    grant_plan_rejected = run(
        "authorization", grant_plan_parent, "create", "--authority", "user", "--request-id", "grant-from-plan",
        "--repository-root", td, "--branch", "main", "--expected-head", "b" * 40,
        "--allowed-path", "plan.txt", "--message", "feat: plan", "--work-unit-id", "WU-grant-plan-two",
        "--candidate-diff-sha", "c" * 64, "--source-operation-id", grant_plan_operation,
        "--source-request-id", "grant-plan-request", "--expect-revision", "9", env=env, ok=False,
    )
    assert "GOVERNED_AUTHORIZATION_SOURCE_IDENTITY_MISMATCH" in grant_plan_rejected.stderr
    assert json.loads(run("show", grant_plan_parent, env=env).stdout)["revision"] == 9
    wrong_wu_source = run(
        "authorization", grant_plan_parent, "create", "--authority", "user", "--request-id", "grant-wrong-wu",
        "--repository-root", td, "--branch", "main", "--expected-head", "b" * 40,
        "--allowed-path", "plan.txt", "--message", "feat: plan", "--work-unit-id", "WU-grant-plan-two",
        "--candidate-diff-sha", "c" * 64, "--source-operation-id", grant_sub_operation,
        "--source-request-id", "grant-sub-request", "--expect-revision", "9", env=env, ok=False,
    )
    assert "governed local_commit authorization source identity mismatch" in wrong_wu_source.stderr
    assert json.loads(run("show", grant_plan_parent, env=env).stdout)["revision"] == 9

    terminal = run(
        "init", "--run-id", "myrmex-terminal-state", "--objective", "Terminal state", "--repository-root", td,
        "--mode", "autonomous", "--scope", "narrow", env=env,
    ).stdout.strip()
    run("lock", terminal, "--owner", "terminal-owner", env=env)
    completed = json.loads(run(
        "complete", terminal, "--message", "objective complete", "--unlock-owner", "terminal-owner", "--expect-revision", "0", env=env,
    ).stdout)
    assert completed["state"]["phase"] == completed["state"]["status"] == "dormant"
    events = [json.loads(line) for line in (Path(td) / "state" / "runs" / terminal / "events.jsonl").read_text().splitlines()]
    assert [event["type"] for event in events].index("objective.complete") < [event["type"] for event in events].index("run.unlocked")
    # Completion is terminal: no new work or generic metadata mutation can
    # silently revive a dormant run.
    run(
        "delegation", terminal, "--agent", "myrmex-worker", "--role", "writer", "--reason", "must not revive",
        "--task-id", "late-task", "--status", "started", "--expect-revision", "1", env=env, ok=False,
    )
    run(
        "delegation-batch", terminal, "start", "--batch-id", "late-batch", "--task-ids-json", '["late-task"]',
        "--expect-revision", "1", env=env, ok=False,
    )
    run(
        "defects", terminal, "--work-unit-id", "WU-late", "--verification-request-id", "req-late",
        "--candidate-sha", "6" * 40, "--scope-digest", "6" * 64, "--defects-json", '["late"]',
        "--expect-revision", "1", env=env, ok=False,
    )
    run("patch", terminal, "--json-patch", '{"notes":"late"}', "--expect-revision", "1", env=env, ok=False)
    run("complete", terminal, "--message", "duplicate completion", "--expect-revision", "1", env=env, ok=False)
    run("cancel", terminal, "--reason", "cannot cancel completed", "--expect-revision", "1", env=env, ok=False)

    legacy = run(
        "init", "--run-id", "myrmex-legacy-dormant", "--objective", "Legacy state", "--repository-root", td,
        "--mode", "autonomous", "--scope", "narrow", env=env,
    ).stdout.strip()
    legacy_path = Path(td) / "state" / "runs" / legacy / "state.json"
    legacy_data = json.loads(legacy_path.read_text())
    legacy_data.update({
        "schema": "myrmex.frontier-state/v1", "schema_version": 1,
        "phase": "dormant", "status": "active", "blocker": "stale",
        "budgets": {
            "max_wait_seconds": 900, "max_plan_iterations": 6,
            "max_worker_corrections": 2, "max_failover_chats": 1,
        },
    })
    legacy_data["attempts"]["corrections"] = 1
    legacy_data.pop("execution")
    legacy_data.pop("active_work_unit_id")
    legacy_data.pop("work_units")
    legacy_data.pop("remediation")
    legacy_data.pop("delegation_batches")
    legacy_path.write_text(json.dumps(legacy_data))
    raw_v1 = legacy_path.read_bytes()
    run("lock", legacy, "--owner", "legacy-owner", env=env, ok=False)
    run("event", legacy, "--type", "legacy.should-not-write", env=env, ok=False)
    run("cancel", legacy, "--reason", "must migrate first", "--expect-revision", "0", env=env, ok=False)
    assert legacy_path.read_bytes() == raw_v1
    migrated = json.loads(run("migrate", legacy, "--expect-revision", "0", env=env).stdout)
    assert migrated["migrated"] is True and migrated["state"]["phase"] == migrated["state"]["status"] == "dormant"
    assert migrated["state"]["execution"]["requested_policy"] == "auto"
    assert migrated["state"]["work_units"]["__legacy__"]["corrections_used"] == 1
    assert Path(migrated["backup"]).is_file()
    invalid_pair = run(
        "patch", legacy, "--set", "phase=collecting-context", "--set", "status=dormant",
        "--expect-revision", "1", env=env, ok=False,
    )
    assert "patch requires an active run" in invalid_pair.stderr

    doctor = json.loads(run("doctor", env=env).stdout)
    assert doctor["ok"] is True and doctor["runs"] == 28

    schema = json.loads((ROOT / "contracts" / "frontier-state-v2.schema.json").read_text())
    missing = sorted(set(schema["required"]) - set(extra_blocked))
    assert not missing, missing
    try:
        import jsonschema  # type: ignore
    except ImportError:
        pass
    else:
        jsonschema.Draft202012Validator(schema).validate(extra_blocked)

print("state CLI test: PASS")
