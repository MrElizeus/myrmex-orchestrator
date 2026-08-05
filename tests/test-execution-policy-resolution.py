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


def run(*args: str, env: dict[str, str], ok: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run([str(STATE), *args], capture_output=True, text=True, env=env, timeout=30)
    if ok and result.returncode != 0:
        raise AssertionError(f"command failed: {args}\nstdout={result.stdout}\nstderr={result.stderr}")
    if not ok and result.returncode == 0:
        raise AssertionError(f"command unexpectedly succeeded: {args}\nstdout={result.stdout}")
    return result


def payload(result: subprocess.CompletedProcess[str]) -> dict:
    return json.loads(result.stdout)


with tempfile.TemporaryDirectory(prefix="myrmex-execution-policy-") as td:
    env = dict(os.environ, MYRMEX_STATE_HOME=str(Path(td) / "state"), PYTHONDONTWRITEBYTECODE="1")
    repo = str(Path(td) / "repo")
    Path(repo).mkdir()

    # Ambiguous prompts are represented by an unresolved run. State never
    # guesses `auto`; reconciliation asks the orchestrator to use OpenCode Ask.
    run_id = run(
        "init", "--run-id", "policy-unresolved", "--objective", "ambiguous request",
        "--repository-root", repo, "--mode", "autonomous", "--scope", "narrow",
        env=env,
    ).stdout.strip()
    state = payload(run("show", run_id, env=env))
    assert state["execution"]["requested_policy"] == "unresolved"
    assert state["execution"]["effective_route"] == "unresolved"
    assert state["execution"]["resolved_at"] is None
    assert payload(run("reconcile", run_id, env=env))["action"] == "REQUEST_EXECUTION_POLICY"

    blocked_frontier = run(
        "frontier", run_id, "start", "--request-id", "req-before-policy",
        "--task-id", "task-before-policy", "--expect-revision", "0", env=env, ok=False,
    )
    assert "BLOCKED_EXECUTION_POLICY_UNRESOLVED" in blocked_frontier.stderr
    blocked_delegation = run(
        "delegation-preflight", run_id, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "must not launch", "--task-id", "task-delegation-before-policy",
        "--work-unit-id", "WU-01", "--workspace", repo, "--expect-revision", "0",
        env=env, ok=False,
    )
    assert "BLOCKED_EXECUTION_POLICY_UNRESOLVED" in blocked_delegation.stderr

    source_digest = hashlib.sha256(b"user selected auto").hexdigest()
    resolved = payload(run(
        "route", "set", run_id, "--policy", "auto", "--authority", "user",
        "--request-id", "req-policy-answer", "--source-digest", source_digest,
        "--expect-revision", "0", env=env,
    ))
    assert resolved["execution"]["requested_policy"] == "auto"
    assert resolved["execution"]["resolved_at"]
    assert resolved["execution"]["source_digest"] == source_digest
    assert payload(run("reconcile", run_id, env=env))["action"] == "CONTINUE_PHASE"

    replay = payload(run(
        "route", run_id, "set", "--policy", "auto", "--authority", "user",
        "--request-id", "req-policy-answer", "--source-digest", source_digest,
        "--expect-revision", "0", env=env,
    ))
    assert replay["revision"] == resolved["revision"]

    # Explicit prompts persist their policy at init and never enter the Ask gate.
    for policy, route in (
        ("auto", "auto"),
        ("direct-only", "direct"),
        ("frontier-gated", "frontier"),
    ):
        explicit_id = f"policy-{policy}"
        created = run(
            "init", "--run-id", explicit_id, "--objective", explicit_id,
            "--repository-root", repo, "--mode", "autonomous", "--scope", "narrow",
            "--execution-policy", policy, "--execution-authority", ("user" if policy == "direct-only" else "prompt"),
            "--execution-request-id", f"req-{policy}", env=env,
        ).stdout.strip()
        explicit = payload(run("show", created, env=env))
        assert explicit["execution"]["requested_policy"] == policy
        assert explicit["execution"]["effective_route"] == route
        assert explicit["execution"]["resolved_at"] is not None
        assert payload(run("reconcile", created, env=env))["action"] != "REQUEST_EXECUTION_POLICY"

    # Frontier-gated allows the planning exchange but prevents delegation until
    # a confirmed ACCEPT plan exists.
    gated = "policy-frontier-gated"
    denied_before_plan = run(
        "delegation-preflight", gated, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "must await plan", "--task-id", "task-before-plan",
        "--work-unit-id", "WU-01", "--workspace", repo, "--expect-revision", "0",
        env=env, ok=False,
    )
    assert "BLOCKED_FRONTIER_PLAN_REQUIRED_BY_EXECUTION_POLICY" in denied_before_plan.stderr
    started_plan = payload(run(
        "frontier", gated, "start", "--request-id", "req-gated-plan",
        "--task-id", "task-gated-plan", "--intent-json", '{"purpose":"planning"}',
        "--expect-revision", "0", env=env,
    ))
    plan_id = started_plan["pending_operations"][0]["operation_id"]
    payload(run(
        "frontier", gated, "result", "--operation-id", plan_id,
        "--request-id", "req-gated-plan", "--message-id", "turn-gated-plan",
        "--transport-status", "success", "--frontier-decision", "ACCEPT",
        "--response-type", "plan", "--plan-json", '{"work_unit_id":"WU-01"}',
        "--effect-json", '{"request_id":"req-gated-plan","message_id":"turn-gated-plan"}',
        "--receipt-json", '{"request_id":"req-gated-plan","message_id":"turn-gated-plan"}',
        "--expect-revision", "1", env=env,
    ))
    allowed = payload(run(
        "delegation-preflight", gated, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "approved work", "--task-id", "task-after-plan",
        "--work-unit-id", "WU-01", "--workspace", repo, "--expect-revision", "2",
        env=env,
    ))
    assert allowed["delegation_ledger"][-1]["task_id"] == "task-after-plan"

    # --- Immutability enforcement ---

    # 3. Trying auto → direct-only must fail: policy already resolved
    already_resolved = run(
        "route", "set", run_id, "--policy", "direct-only", "--authority", "user",
        "--request-id", "req-change-attempt", "--expect-revision", str(resolved["revision"]),
        env=env, ok=False,
    )
    assert "BLOCKED_EXECUTION_POLICY_ALREADY_RESOLVED" in already_resolved.stderr, (
        f"Expected BLOCKED_EXECUTION_POLICY_ALREADY_RESOLVED, got: {already_resolved.stderr}"
    )
    after_blocked = payload(run("show", run_id, env=env))
    assert after_blocked["execution"]["requested_policy"] == "auto"
    assert after_blocked["revision"] == resolved["revision"]

    # 4. Changing only authority must also fail (not an exact replay)
    auth_change = run(
        "route", "set", run_id, "--policy", "auto", "--authority", "frontier",
        "--request-id", "req-policy-answer", "--source-digest", source_digest,
        "--expect-revision", str(resolved["revision"]),
        env=env, ok=False,
    )
    assert "BLOCKED_EXECUTION_POLICY_ALREADY_RESOLVED" in auth_change.stderr, (
        f"Expected BLOCKED_EXECUTION_POLICY_ALREADY_RESOLVED for authority change, got: {auth_change.stderr}"
    )

    # 5. delegated policy: allows delegation preflight, blocks frontier start
    delegated_id = "run-delegated-policy"
    run(
        "init", "--run-id", delegated_id, "--objective", delegated_id,
        "--repository-root", repo, "--mode", "autonomous", "--scope", "narrow",
        "--execution-policy", "delegated", "--execution-authority", "prompt",
        "--execution-request-id", "req-delegated", env=env,
    )
    allowed_delegation = payload(run(
        "delegation-preflight", delegated_id, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "delegated run", "--task-id", "task-delegated-01",
        "--work-unit-id", "WU-DEL-01", "--workspace", repo, "--expect-revision", "0",
        env=env,
    ))
    assert allowed_delegation["delegation_ledger"][-1]["task_id"] == "task-delegated-01"
    blocked_frontier_delegated = run(
        "frontier", delegated_id, "start", "--request-id", "req-frontier-on-delegated",
        "--task-id", "task-frontier-delegated", "--expect-revision", "1",
        env=env, ok=False,
    )
    assert "BLOCKED_FRONTIER_FORBIDDEN_BY_EXECUTION_POLICY" in blocked_frontier_delegated.stderr, (
        f"Expected BLOCKED_FRONTIER_FORBIDDEN_BY_EXECUTION_POLICY, got: {blocked_frontier_delegated.stderr}"
    )

    # 6. direct-only: blocks both delegation and frontier start
    direct_id = "run-direct-only-policy"
    run(
        "init", "--run-id", direct_id, "--objective", direct_id,
        "--repository-root", repo, "--mode", "autonomous", "--scope", "narrow",
        "--execution-policy", "direct-only", "--execution-authority", "user",
        "--execution-request-id", "req-direct-only", env=env,
    )
    blocked_delegation_direct = run(
        "delegation-preflight", direct_id, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "must be blocked", "--task-id", "task-direct-block",
        "--work-unit-id", "WU-DIRECT-01", "--workspace", repo, "--expect-revision", "0",
        env=env, ok=False,
    )
    assert "BLOCKED_DELEGATION_FORBIDDEN_BY_EXECUTION_POLICY" in blocked_delegation_direct.stderr, (
        f"Expected BLOCKED_DELEGATION_FORBIDDEN_BY_EXECUTION_POLICY, got: {blocked_delegation_direct.stderr}"
    )
    blocked_frontier_direct = run(
        "frontier", direct_id, "start", "--request-id", "req-frontier-direct",
        "--task-id", "task-frontier-direct", "--expect-revision", "0",
        env=env, ok=False,
    )
    assert "BLOCKED_DELEGATION_FORBIDDEN_BY_EXECUTION_POLICY" in blocked_frontier_direct.stderr, (
        f"Expected BLOCKED_DELEGATION_FORBIDDEN_BY_EXECUTION_POLICY for direct-only frontier, got: {blocked_frontier_direct.stderr}"
    )

    # 7. legacy frontier policy: permits frontier start, blocks delegation before plan, allows after ACCEPT
    legacy_frontier_id = "run-legacy-frontier-policy"
    run(
        "init", "--run-id", legacy_frontier_id, "--objective", legacy_frontier_id,
        "--repository-root", repo, "--mode", "autonomous", "--scope", "narrow",
        "--execution-policy", "frontier", "--execution-authority", "prompt",
        "--execution-request-id", "req-legacy-frontier", env=env,
    )
    # 7.1. Blocks delegation before plan
    denied_before_legacy_plan = run(
        "delegation-preflight", legacy_frontier_id, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "must await legacy plan", "--task-id", "task-before-legacy-plan",
        "--work-unit-id", "WU-LF-01", "--workspace", repo, "--expect-revision", "0",
        env=env, ok=False,
    )
    assert "BLOCKED_FRONTIER_PLAN_REQUIRED_BY_EXECUTION_POLICY" in denied_before_legacy_plan.stderr, (
        f"Expected BLOCKED_FRONTIER_PLAN_REQUIRED_BY_EXECUTION_POLICY, got: {denied_before_legacy_plan.stderr}"
    )
    # 7.2. Allows frontier start
    started_legacy_plan = payload(run(
        "frontier", legacy_frontier_id, "start", "--request-id", "req-legacy-gated-plan",
        "--task-id", "task-legacy-gated-plan", "--intent-json", '{"purpose":"planning"}',
        "--expect-revision", "0", env=env,
    ))
    legacy_plan_id = started_legacy_plan["pending_operations"][0]["operation_id"]
    # Confirm it
    payload(run(
        "frontier", legacy_frontier_id, "result", "--operation-id", legacy_plan_id,
        "--request-id", "req-legacy-gated-plan", "--message-id", "turn-legacy-gated-plan",
        "--transport-status", "success", "--frontier-decision", "ACCEPT",
        "--response-type", "plan", "--plan-json", '{"work_unit_id":"WU-LF-01"}',
        "--effect-json", '{"request_id":"req-legacy-gated-plan","message_id":"turn-legacy-gated-plan"}',
        "--receipt-json", '{"request_id":"req-legacy-gated-plan","message_id":"turn-legacy-gated-plan"}',
        "--expect-revision", "1", env=env,
    ))
    # 7.3. Allows delegation after ACCEPT
    allowed_legacy = payload(run(
        "delegation-preflight", legacy_frontier_id, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "approved legacy work", "--task-id", "task-after-legacy-plan",
        "--work-unit-id", "WU-LF-01", "--workspace", repo, "--expect-revision", "2",
        env=env,
    ))
    assert allowed_legacy["delegation_ledger"][-1]["task_id"] == "task-after-legacy-plan"

print("execution policy resolution test: PASS")
