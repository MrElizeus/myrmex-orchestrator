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
        raise AssertionError(f"command unexpectedly succeeded: {args}")
    return result


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
        "--task-id", "task-pending", "--status", "started", "--expect-revision", "0", env=env,
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

    recorded = json.loads(run(
        "delegation", run_id, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "bounded implementation", "--task-id", "task-1",
        "--work-unit-id", "unit-1", "--status", "success", "--expect-revision", "0", env=env,
    ).stdout)
    assert recorded["revision"] == 1
    assert recorded["attempts"]["writers"] == 1
    assert recorded["delegation_ledger"][0]["work_unit_id"] == "unit-1"

    defects = json.loads(run(
        "defects", run_id, "--work-unit-id", "WU-02", "--verification-request-id", "req-verify-wu02-1",
        "--candidate-sha", "a" * 40, "--scope-digest", "a" * 64,
        "--defects-json", '["missing assertion", "missing test"]', "--expect-revision", "1", env=env,
    ).stdout)
    assert defects["revision"] == 2
    assert defects["defect_history"][0]["remaining"] == ["missing assertion", "missing test"]

    progress = json.loads(run(
        "defects", run_id, "--work-unit-id", "WU-02", "--verification-request-id", "req-verify-wu02-2",
        "--candidate-sha", "b" * 40, "--scope-digest", "b" * 64,
        "--corrected-json", '["missing assertion"]', "--remaining-json", '["missing test"]', "--new-json", "[]",
        "--expect-revision", "2", env=env,
    ).stdout)
    assert progress["revision"] == 3
    assert progress["no_progress_cycles"] == 0
    assert progress["defect_history"][-1]["progress"] == "reduced"

    run(
        "patch", run_id, "--expect-revision", "3", "--set", "phase=collecting-context",
        env=env, ok=False,
    )
    run(
        "patch", run_id, "--expect-revision", "3", "--set", "budgets.max_corrections_per_work_unit=99",
        env=env, ok=False,
    )
    transitioned = json.loads(run(
        "transition", run_id, "--expect-revision", "3", "--to-phase", "collecting-context",
        "--reason", "context collection is the next safe step",
        env=env,
    ).stdout)
    assert transitioned["revision"] == 4 and transitioned["phase"] == "collecting-context"
    run(
        "transition", run_id, "--expect-revision", "3", "--to-phase", "requesting-plan",
        "--reason", "stale revision must fail", env=env, ok=False,
    )
    run(
        "transition", run_id, "--expect-revision", "4", "--to-phase", "pushing",
        "--reason", "phase skips must fail", env=env, ok=False,
    )
    # External receipts are now owned by the typed operation lifecycle; the
    # generic compatibility patch remains available only for non-critical
    # metadata and cannot bypass completion/recovery gates.
    run(
        "patch", run_id, "--expect-revision", "4",
        "--json-patch", '{"receipts.github_pr":{"number":1,"url":"https://example.test/pr/1}}',
        env=env, ok=False,
    )
    metadata = json.loads(run(
        "patch", run_id, "--expect-revision", "4",
        "--json-patch", '{"notes":"non-critical metadata"}',
        env=env,
    ).stdout)
    assert metadata["revision"] == 5 and metadata["notes"] == "non-critical metadata"

    # Correction capacity is scoped to a WU.  Two WU-02 corrections do not
    # consume WU-03's first correction; only a third WU-02 attempt blocks it.
    correction_one = json.loads(run(
        "delegation", run_id, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "fix verified defects", "--work-unit-id", "WU-02",
        "--status", "success", "--correction", "--source-request-id", "req-verify-wu02-2",
        "--scope-digest", "b" * 64, "--source-candidate-sha", "b" * 40,
        "--expect-revision", "5", env=env,
    ).stdout)
    assert correction_one["work_units"]["WU-02"]["corrections_used"] == 1
    correction_two = json.loads(run(
        "delegation", run_id, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "fix remaining verified defects", "--work-unit-id", "WU-02",
        "--status", "success", "--correction", "--source-request-id", "req-verify-wu02-3",
        "--scope-digest", "c" * 64, "--source-candidate-sha", "c" * 40,
        "--expect-revision", "6", env=env,
    ).stdout)
    assert correction_two["attempts"]["corrections"] == 2
    wu03_first = json.loads(run(
        "delegation", run_id, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "first independent WU-03 correction", "--work-unit-id", "WU-03",
        "--status", "success", "--correction", "--source-request-id", "req-verify-wu03-1",
        "--scope-digest", "d" * 64, "--source-candidate-sha", "d" * 40,
        "--expect-revision", "7", env=env,
    ).stdout)
    assert wu03_first["work_units"]["WU-03"]["corrections_used"] == 1
    assert wu03_first["remediation"]["total_corrections_used"] == 3

    blocked = json.loads(run(
        "delegation", run_id, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "third WU-02 correction must stop", "--work-unit-id", "WU-02",
        "--status", "blocked", "--correction", "--source-request-id", "req-verify-wu02-4",
        "--scope-digest", "e" * 64, "--source-candidate-sha", "e" * 40,
        "--expect-revision", "8", env=env, ok=False,
    ).stdout)
    assert blocked["status"] == "blocked" and blocked["blocker"] == "BLOCKED_CORRECTION_BUDGET"
    assert blocked["remediation"]["blocked"]["work_unit_id"] == "WU-02"
    run("patch", run_id, "--set", "status=active", env=env, ok=False)
    run(
        "transition", run_id, "--expect-revision", "9", "--to-phase", "implementing",
        "--reason", "generic transitions cannot clear a correction blocker", env=env, ok=False,
    )
    # An authorization cannot clear WU-02 with WU-03 scope, but the matching
    # typed grant reopens the run and gives exactly one extra attempt.
    run(
        "correction", "authorize", run_id, "--work-unit-id", "WU-03", "--authority", "frontier",
        "--request-id", "req-frontier-wu03", "--scope-digest", "d" * 64,
        "--source-candidate-sha", "d" * 40, "--max-additional-attempts", "1",
        "--expect-revision", "9", env=env, ok=False,
    )
    run(
        "correction", "authorize", run_id, "--work-unit-id", "WU-02", "--authority", "frontier",
        "--request-id", "req-frontier-wu02", "--scope-digest", "e" * 64,
        "--source-candidate-sha", "e" * 40, "--max-additional-attempts", "1",
        "--expect-revision", "8", env=env, ok=False,
    )
    authorized = json.loads(run(
        "correction", "authorize", run_id, "--work-unit-id", "WU-02", "--authority", "frontier",
        "--request-id", "req-frontier-wu02", "--scope-digest", "e" * 64,
        "--source-candidate-sha", "e" * 40, "--max-additional-attempts", "1",
        "--expect-revision", "9", env=env,
    ).stdout)
    assert authorized["status"] == "active" and authorized["remediation"]["grants"][0]["granted_attempts"] == 1
    replay = json.loads(run(
        "correction", "authorize", run_id, "--work-unit-id", "WU-02", "--authority", "frontier",
        "--request-id", "req-frontier-wu02", "--scope-digest", "e" * 64,
        "--source-candidate-sha", "e" * 40, "--max-additional-attempts", "1",
        "--expect-revision", "10", env=env,
    ).stdout)
    assert replay["revision"] == 10 and len(replay["remediation"]["grants"]) == 1
    stale_scope = json.loads(run(
        "delegation", run_id, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "stale candidate and changed defect scope cannot consume grant", "--work-unit-id", "WU-02",
        "--status", "success", "--correction", "--source-request-id", "req-verify-wu02-stale",
        "--scope-digest", "f" * 64, "--source-candidate-sha", "f" * 40,
        "--expect-revision", "10", env=env, ok=False,
    ).stdout)
    assert stale_scope["blocker"] == "BLOCKED_CORRECTION_BUDGET"
    assert stale_scope["remediation"]["grants"][0]["consumed_attempts"] == 0
    reactivated = json.loads(run(
        "correction", "authorize", run_id, "--work-unit-id", "WU-02", "--authority", "frontier",
        "--request-id", "req-frontier-wu02", "--scope-digest", "e" * 64,
        "--source-candidate-sha", "e" * 40, "--max-additional-attempts", "1",
        "--expect-revision", "11", env=env,
    ).stdout)
    assert reactivated["revision"] == 12 and reactivated["status"] == "active"
    grant_attempt = json.loads(run(
        "delegation", run_id, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "one authorized WU-02 correction", "--work-unit-id", "WU-02",
        "--status", "success", "--correction", "--source-request-id", "req-verify-wu02-4",
        "--scope-digest", "e" * 64, "--source-candidate-sha", "e" * 40,
        "--expect-revision", "12", env=env,
    ).stdout)
    assert grant_attempt["work_units"]["WU-02"]["corrections_used"] == 3
    assert grant_attempt["remediation"]["grants"][0]["consumed_attempts"] == 1
    extra_blocked = json.loads(run(
        "delegation", run_id, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "grant cannot be reused", "--work-unit-id", "WU-02",
        "--status", "blocked", "--correction", "--source-request-id", "req-verify-wu02-5",
        "--scope-digest", "f" * 64, "--source-candidate-sha", "f" * 40,
        "--expect-revision", "13", env=env, ok=False,
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
        "--work-unit-id", "WU-01", "--source-request-id", "req-cap-1",
        "--scope-digest", "4" * 64, "--source-candidate-sha", "4" * 40, "--expect-revision", "0", env=env,
    )
    global_blocked = json.loads(run(
        "delegation", capped, "--agent", "myrmex-worker", "--role", "writer", "--reason", "WU two",
        "--work-unit-id", "WU-02", "--status", "success", "--correction", "--source-request-id", "req-cap-2",
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
    informational = json.loads(run(
        "delegation", provider, "--agent", "myrmex-scout", "--role", "scout", "--reason", "configured routing",
        "--task-id", "credential-hidden", "--status", "success", "--outcome", "CREDENTIAL_NOT_VISIBLE_TO_ORCHESTRATOR", "--expect-revision", "0", env=env,
    ).stdout)
    assert informational["status"] == "active"
    failed_provider = json.loads(run(
        "delegation", provider, "--agent", "myrmex-scout", "--role", "scout", "--reason", "Task returned provider error",
        "--task-id", "provider-error", "--status", "failed", "--outcome", "PROVIDER_INVOCATION_FAILED",
        "--evidence-json", '{"message":"Bearer abcdefghijklmnopqrstuvwxyz","api_key":"must-not-persist"}', "--expect-revision", "1", env=env,
    ).stdout)
    evidence = failed_provider["delegation_ledger"][-1]["evidence"]
    assert evidence["api_key"] == "[REDACTED]" and "Bearer" not in evidence["message"]
    explicitly_blocked = json.loads(run(
        "transition", provider, "--expect-revision", "2", "--to-phase", "blocked",
        "--blocker", "HUMAN_DECISION_REQUIRED", "--reason", "an explicit human decision is required", env=env,
    ).stdout)
    assert explicitly_blocked["status"] == "blocked" and explicitly_blocked["blocker"] == "HUMAN_DECISION_REQUIRED"
    unresolved = run(
        "init", "--run-id", "myrmex-unresolved-agent", "--objective", "Unresolved agent", "--repository-root", td,
        "--mode", "autonomous", "--scope", "narrow", env=env,
    ).stdout.strip()
    blocked_agent = json.loads(run(
        "delegation", unresolved, "--agent", "myrmex-worker", "--role", "writer", "--reason", "resolver blocked",
        "--status", "blocked", "--outcome", "AGENT_MODEL_UNRESOLVED", "--expect-revision", "0", env=env,
    ).stdout)
    assert blocked_agent["status"] == "blocked" and blocked_agent["blocker"] == "AGENT_MODEL_UNRESOLVED"

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
        "--intent-json", '{"purpose":"plan"}', "--expect-revision", "0", env=env,
    ).stdout)
    frontier_operation = frontier_intent["pending_operations"][0]["operation_id"]
    assert json.loads(run("reconcile", recovery_run, env=env).stdout)["action"] == "RECOVER_FRONTIER_EXCHANGE"
    run(
        "operation", recovery_run, "observe", "--operation-id", frontier_operation,
        "--effect-json", '{"transport":"browser","request_id":"req-frontier-ledger-1"}',
        "--expect-revision", "1", env=env,
    )
    assert json.loads(run("reconcile", recovery_run, env=env).stdout)["action"] == "WAIT_FRONTIER"
    run(
        "operation", recovery_run, "receipt", "--operation-id", frontier_operation,
        "--receipt-json", '{"status":"PLAN_RECEIVED"}', "--expect-revision", "2", env=env,
    )
    # Re-observing an already-discovered effect is idempotent and does not
    # regress a receipt-recorded operation or consume a revision.
    same_effect = json.loads(run(
        "operation", recovery_run, "observe", "--operation-id", frontier_operation,
        "--effect-json", '{"transport":"browser","request_id":"req-frontier-ledger-1"}',
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
        "--task-id", "task-preflight-1", "--work-unit-id", "WU-preflight", "--status", "success",
        "--evidence-json", '{"tests":"passed"}', "--expect-revision", "1", env=env,
    ).stdout)
    assert task_result["pending_operations"][0]["status"] == "confirmed"
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

    parent_gate = run(
        "init", "--run-id", "myrmex-parent-gate", "--objective", "Parent gate", "--parent-objective", "continuous objective",
        "--repository-root", td, "--mode", "autonomous", "--scope", "continuous", env=env,
    ).stdout.strip()
    parent_incomplete = run(
        "complete", parent_gate, "--message", "cannot skip parent gate", "--expect-revision", "0", env=env, ok=False,
    )
    assert "parent objective has not passed a parent gate" in parent_incomplete.stderr

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
    assert doctor["ok"] is True and doctor["runs"] == 18

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
