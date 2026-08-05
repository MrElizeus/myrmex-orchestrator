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

print("execution policy resolution test: PASS")
