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
FIXTURE = json.loads((ROOT / "tests/fixtures/eigengrid-blocked-run-v2.json").read_text())
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


def start_failure(env: dict[str, str], run_id: str, request_id: str, revision: int) -> tuple[str, int]:
    started = payload(run(
        "frontier", run_id, "start", "--request-id", request_id,
        "--task-id", f"task-{request_id[-2:]}",
        "--intent-json", '{"purpose":"controlled-alpha-plan","request_payload":"WU-08-WU-13"}',
        "--expect-revision", str(revision), env=env,
    ))
    operation_id = started["pending_operations"][-1]["operation_id"]
    failed = payload(run(
        "frontier", run_id, "result", "--operation-id", operation_id,
        "--request-id", request_id, "--transport-status", "transport_error",
        "--effect-stage", "none", "--pre-effect-absence-proven",
        "--effect-json", json.dumps(NO_EFFECT), "--receipt-json", json.dumps(NO_EFFECT),
        "--expect-revision", str(revision + 1), env=env,
    ))
    assert failed["pending_operations"][-1]["status"] == "failed"
    return operation_id, revision + 2


with tempfile.TemporaryDirectory(prefix="myrmex-eigengrid-incident-") as td:
    env = dict(os.environ, MYRMEX_STATE_HOME=str(Path(td) / "state"), PYTHONDONTWRITEBYTECODE="1")
    repo = Path(td) / "eigengrid"
    repo.mkdir()
    run_id = run(
        "init", "--run-id", "eigengrid-incident-copy", "--objective", "complete controlled alpha",
        "--repository-root", str(repo), "--branch", "feat/eigengrid-controlled-alpha",
        "--mode", "autonomous", "--scope", "narrow", "--execution-policy", "auto",
        "--commit-policy", "authorized", "--push-policy", "deny", env=env,
    ).stdout.strip()

    aliases = {item["source_alias"]: item for item in FIXTURE["operations"]}
    first_id, revision = start_failure(
        env, run_id, aliases["op-2c1012cc021dc284eddae80a"]["request_id"], 0,
    )
    second_id, revision = start_failure(
        env, run_id, aliases["op-7f4f34c7b0e5a2fb6a3fb008"]["request_id"], revision,
    )

    plan = aliases["op-856f85a3e3c12a292fb0bccd"]
    started_plan = payload(run(
        "frontier", run_id, "start", "--request-id", plan["request_id"],
        "--task-id", "task-plan-04",
        "--intent-json", '{"purpose":"controlled-alpha-plan","request_payload":"WU-08-WU-13"}',
        "--expect-revision", str(revision), env=env,
    ))
    plan_id = started_plan["pending_operations"][-1]["operation_id"]
    confirmed_plan = payload(run(
        "frontier", run_id, "result", "--operation-id", plan_id,
        "--request-id", plan["request_id"], "--message-id", plan["message_id"],
        "--transport-status", "success", "--frontier-decision", "ACCEPT",
        "--response-type", "plan", "--plan-json", '{"work_unit_id":"WU-08"}',
        "--effect-json", json.dumps({"request_id": plan["request_id"], "message_id": plan["message_id"]}),
        "--receipt-json", json.dumps({"request_id": plan["request_id"], "message_id": plan["message_id"]}),
        "--expect-revision", str(revision + 1), env=env,
    ))
    revision += 2
    assert confirmed_plan["pending_operations"][-1]["status"] == "confirmed"

    delegation = aliases["op-a9983d527610cefbe1887884"]
    run(
        "delegation-preflight", run_id, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "WU-08 candidate", "--task-id", delegation["task_id"],
        "--work-unit-id", delegation["work_unit_id"], "--workspace", str(repo),
        "--expect-revision", str(revision), env=env,
    )
    run(
        "delegation", run_id, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "WU-08 candidate", "--task-id", delegation["task_id"],
        "--work-unit-id", delegation["work_unit_id"], "--workspace", str(repo),
        "--status", "failed", "--expect-revision", str(revision + 1), env=env,
    )
    revision += 2

    authorization = payload(run(
        "authorization", run_id, "create", "--authority", "frontier",
        "--request-id", "req-wu08-commit", "--repository-root", str(repo),
        "--branch", "feat/eigengrid-controlled-alpha", "--expected-head", "586a5e9",
        "--allowed-path", "apps/cli/src/tui/App.tsx", "--message", "feat(tui): operational dashboard",
        "--expect-revision", str(revision), env=env,
    ))
    revision += 1
    assert authorization["authorizations"][0]["status"] == "open"
    assert authorization["authorizations"][0]["consumed_uses"] == 0

    run("transition", run_id, "--to-phase", "collecting-context", "--reason", "context", "--expect-revision", str(revision), env=env)
    revision += 1
    run("transition", run_id, "--to-phase", "implementing", "--reason", "candidate staged", "--expect-revision", str(revision), env=env)
    revision += 1
    run("transition", run_id, "--to-phase", "verifying", "--reason", "candidate verification", "--expect-revision", str(revision), env=env)
    revision += 1

    digest = hashlib.sha256(json.dumps(NO_EFFECT, sort_keys=True).encode()).hexdigest()
    blocked = payload(run(
        "transition", run_id, "--to-phase", "blocked", "--reason", "legacy Frontier deadlock",
        "--blocker", f"{FIXTURE['blocker_code']}: missing original identity",
        "--recovery-code", FIXTURE["blocker_code"],
        "--recovery-operation-id", first_id, "--recovery-operation-id", second_id,
        "--recovery-resume-phase", FIXTURE["resume_phase"],
        "--recovery-evidence-digest", digest, "--expect-revision", str(revision), env=env,
    ))
    revision += 1
    assert blocked["status"] == FIXTURE["status"]

    frontier_count_before = sum(
        item.get("kind") == "frontier_exchange" for item in blocked["pending_operations"]
    )
    assert frontier_count_before == 3

    first_action = payload(run("reconcile", run_id, env=env))
    assert first_action["action"] == "FINALIZE_FRONTIER_SUPERSESSION"
    assert first_action["operation_ids"] == [first_id, plan_id]
    first_resolution = payload(run(
        "recovery", run_id, "resolve-frontier", "--operation-id", first_id,
        "--successor-operation-id", plan_id, "--disposition", "supersede",
        "--reason", "PRE_EFFECT_FAILURE", "--expect-revision", str(revision), env=env,
    ))
    revision += 1
    assert first_resolution["status"] == "blocked"
    assert first_resolution["recovery"]["remaining_operation_ids"] == [second_id]

    second_action = payload(run("reconcile", run_id, env=env))
    assert second_action["action"] == "FINALIZE_FRONTIER_SUPERSESSION"
    assert second_action["operation_ids"] == [second_id, plan_id]
    resolved = payload(run(
        "recovery", run_id, "resolve-frontier", "--operation-id", second_id,
        "--successor-operation-id", plan_id, "--disposition", "supersede",
        "--reason", "PRE_EFFECT_FAILURE", "--expect-revision", str(revision), env=env,
    ))
    revision += 1
    assert resolved["status"] == FIXTURE["expected"]["final_status"]
    assert resolved["phase"] == FIXTURE["expected"]["final_phase"]
    assert resolved["blocker"] is None
    assert resolved["authorizations"][0]["status"] == "open"
    assert resolved["authorizations"][0]["consumed_uses"] == 0
    assert resolved["commit_sha"] is None
    assert resolved["push_status"] == "not_requested"
    assert len(resolved["pending_operations"]) == 4
    assert sum(item.get("kind") == "frontier_exchange" for item in resolved["pending_operations"]) == frontier_count_before
    authoritative = next(item for item in resolved["pending_operations"] if item["operation_id"] == plan_id)
    assert authoritative["request_id"] == FIXTURE["expected"]["authoritative_request_id"]
    assert authoritative["message_id"] == FIXTURE["expected"]["authoritative_message_id"]
    assert authoritative["status"] == "confirmed"
    assert [item["status"] for item in resolved["pending_operations"][:2]] == ["superseded", "superseded"]

    final_action = payload(run("reconcile", run_id, env=env))
    assert final_action["action"] == "RUN_LOCAL_VERIFICATION"
    replay = payload(run(
        "recovery", run_id, "resolve-frontier", "--operation-id", second_id,
        "--successor-operation-id", plan_id, "--disposition", "supersede",
        "--reason", "PRE_EFFECT_FAILURE", "--expect-revision", str(revision - 1), env=env,
    ))
    assert replay["revision"] == resolved["revision"]
    assert len(replay["pending_operations"]) == 4

print("EigenGrid blocked-run incident regression: PASS")
