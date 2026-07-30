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
    assert state["schema"] == "myrmex.frontier-state/v1"
    assert state["schema_version"] == 1
    assert state["revision"] == 0
    assert state["push_status"] == "not_requested"
    assert state["attempts"] == {"scouts": 0, "writers": 0, "verifiers": 0, "corrections": 0}
    assert state["delegation_ledger"] == []
    assert state["no_progress_cycles"] == 0

    run("lock", run_id, "--owner", "test-owner", env=env)
    run("lock", run_id, "--owner", "other-owner", env=env, ok=False)
    run("lock", run_id, "--owner", "test-owner", env=env)

    recorded = json.loads(run(
        "delegation", run_id, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "bounded implementation", "--task-id", "task-1",
        "--work-unit-id", "unit-1", "--status", "success", env=env,
    ).stdout)
    assert recorded["revision"] == 1
    assert recorded["attempts"]["writers"] == 1
    assert recorded["delegation_ledger"][0]["work_unit_id"] == "unit-1"

    defects = json.loads(run(
        "defects", run_id, "--defects-json", '["missing assertion", "missing test"]', env=env,
    ).stdout)
    assert defects["revision"] == 2
    assert defects["defect_history"][0]["remaining"] == ["missing assertion", "missing test"]

    progress = json.loads(run(
        "defects", run_id, "--corrected-json", '["missing assertion"]',
        "--remaining-json", '["missing test"]', "--new-json", "[]",
        "--expect-revision", "2", env=env,
    ).stdout)
    assert progress["revision"] == 3
    assert progress["no_progress_cycles"] == 0
    assert progress["defect_history"][-1]["progress"] == "reduced"

    patched = json.loads(run(
        "patch", run_id, "--expect-revision", "3", "--set", "phase=collecting-context",
        env=env,
    ).stdout)
    assert patched["revision"] == 4
    run("patch", run_id, "--expect-revision", "3", "--set", "phase=requesting-plan", env=env, ok=False)

    correction_one = json.loads(run(
        "delegation", run_id, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "fix verified defects", "--work-unit-id", "unit-1",
        "--status", "success", "--correction", "--expect-revision", "4", env=env,
    ).stdout)
    assert correction_one["attempts"]["corrections"] == 1
    correction_two = json.loads(run(
        "delegation", run_id, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "fix remaining verified defects", "--work-unit-id", "unit-1",
        "--status", "success", "--correction", "--expect-revision", "5", env=env,
    ).stdout)
    assert correction_two["attempts"]["corrections"] == 2

    blocked = json.loads(run(
        "delegation", run_id, "--agent", "myrmex-worker", "--role", "writer",
        "--reason", "third correction must stop", "--work-unit-id", "unit-1",
        "--status", "blocked", "--correction", "--expect-revision", "6", env=env, ok=False,
    ).stdout)
    assert blocked["status"] == "blocked"
    assert blocked["blocker"] == "BLOCKED_CORRECTION_BUDGET"
    assert blocked["attempts"]["corrections"] == 2

    run("unlock", run_id, "--owner", "other-owner", env=env, ok=False)
    run("unlock", run_id, "--owner", "test-owner", env=env)
    second = run(
        "init", "--run-id", "myrmex-no-progress", "--objective", "No progress test",
        "--repository-root", td, "--mode", "autonomous", "--scope", "narrow",
        env=env,
    ).stdout.strip()
    run("defects", second, "--defects-json", '["same defect"]', env=env)
    unchanged = json.loads(run(
        "defects", second, "--remaining-json", '["same defect"]', "--expect-revision", "1", env=env,
    ).stdout)
    assert unchanged["no_progress_cycles"] == 1
    blocked_progress = json.loads(run(
        "defects", second, "--remaining-json", '["same defect"]', "--expect-revision", "2",
        env=env, ok=False,
    ).stdout)
    assert blocked_progress["blocker"] == "BLOCKED_NO_PROGRESS"

    # A configured agent delegates without an API key visible to the parent;
    # only a real provider error is an execution failure and evidence is redacted.
    provider = run(
        "init", "--run-id", "myrmex-provider-outcomes", "--objective", "Provider outcomes",
        "--repository-root", td, "--mode", "autonomous", "--scope", "narrow", env=env,
    ).stdout.strip()
    informational = json.loads(run(
        "delegation", provider, "--agent", "myrmex-scout", "--role", "scout", "--reason", "configured routing",
        "--task-id", "credential-hidden", "--status", "success", "--outcome", "CREDENTIAL_NOT_VISIBLE_TO_ORCHESTRATOR", env=env,
    ).stdout)
    assert informational["status"] == "active"
    failed_provider = json.loads(run(
        "delegation", provider, "--agent", "myrmex-scout", "--role", "scout", "--reason", "Task returned provider error",
        "--task-id", "provider-error", "--status", "failed", "--outcome", "PROVIDER_INVOCATION_FAILED",
        "--evidence-json", '{"message":"Bearer abcdefghijklmnopqrstuvwxyz","api_key":"must-not-persist"}', "--expect-revision", "1", env=env,
    ).stdout)
    evidence = failed_provider["delegation_ledger"][-1]["evidence"]
    assert evidence["api_key"] == "[REDACTED]" and "Bearer" not in evidence["message"]
    unresolved = run(
        "init", "--run-id", "myrmex-unresolved-agent", "--objective", "Unresolved agent", "--repository-root", td,
        "--mode", "autonomous", "--scope", "narrow", env=env,
    ).stdout.strip()
    blocked_agent = json.loads(run(
        "delegation", unresolved, "--agent", "myrmex-worker", "--role", "writer", "--reason", "resolver blocked",
        "--status", "blocked", "--outcome", "AGENT_MODEL_UNRESOLVED", env=env,
    ).stdout)
    assert blocked_agent["status"] == "blocked" and blocked_agent["blocker"] == "AGENT_MODEL_UNRESOLVED"

    # Join records every task before launch, accepts terminal results in any
    # order, consolidates once, and cannot advance the next gate twice.
    joined = run(
        "init", "--run-id", "myrmex-delegation-join", "--objective", "Join test", "--repository-root", td,
        "--mode", "autonomous", "--scope", "narrow", env=env,
    ).stdout.strip()
    run("delegation-batch", joined, "start", "--batch-id", "scouts", "--task-ids-json", '["task-1","task-2","task-3"]', env=env)
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
    run("delegation-batch", missing, "start", "--batch-id", "scouts", "--task-ids-json", '["task-1","task-2"]', env=env)
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

    contradiction = run(
        "init", "--run-id", "myrmex-join-conflict", "--objective", "Contradictory join result", "--repository-root", td,
        "--mode", "autonomous", "--scope", "narrow", env=env,
    ).stdout.strip()
    run("delegation-batch", contradiction, "start", "--batch-id", "scouts", "--task-ids-json", '["task-1","task-2"]', env=env)
    conflict = json.loads(run(
        "delegation-batch", contradiction, "collect", "--batch-id", "scouts", "--expect-revision", "1",
        "--results-json", '[{"task_id":"task-1","status":"success","payload":{"claims":[{"subject":"base","value":"a"}]}},{"task_id":"task-2","status":"success","payload":{"claims":[{"subject":"base","value":"b"}]}}]', env=env, ok=False,
    ).stdout)
    assert conflict["blocker"] == "BLOCKED_CONTRADICTORY_DELEGATION_RESULTS"

    terminal = run(
        "init", "--run-id", "myrmex-terminal-state", "--objective", "Terminal state", "--repository-root", td,
        "--mode", "autonomous", "--scope", "narrow", env=env,
    ).stdout.strip()
    run("lock", terminal, "--owner", "terminal-owner", env=env)
    completed = json.loads(run(
        "complete", terminal, "--message", "objective complete", "--unlock-owner", "terminal-owner", env=env,
    ).stdout)
    assert completed["state"]["phase"] == completed["state"]["status"] == "dormant"
    events = [json.loads(line) for line in (Path(td) / "state" / "runs" / terminal / "events.jsonl").read_text().splitlines()]
    assert [event["type"] for event in events].index("objective.complete") < [event["type"] for event in events].index("run.unlocked")

    legacy = run(
        "init", "--run-id", "myrmex-legacy-dormant", "--objective", "Legacy state", "--repository-root", td,
        "--mode", "autonomous", "--scope", "narrow", env=env,
    ).stdout.strip()
    legacy_path = Path(td) / "state" / "runs" / legacy / "state.json"
    legacy_data = json.loads(legacy_path.read_text())
    legacy_data.update({"phase": "dormant", "status": "active", "blocker": "stale"})
    legacy_data.pop("delegation_batches")
    legacy_path.write_text(json.dumps(legacy_data))
    migrated = json.loads(run("migrate", legacy, "--expect-revision", "0", env=env).stdout)
    assert migrated["migrated"] is True and migrated["state"]["phase"] == migrated["state"]["status"] == "dormant"
    invalid_pair = run(
        "patch", legacy, "--set", "phase=collecting-context", "--set", "status=dormant", env=env, ok=False,
    )
    assert "invalid phase/status combination" in invalid_pair.stderr

    doctor = json.loads(run("doctor", env=env).stdout)
    assert doctor["ok"] is True and doctor["runs"] == 9

    schema = json.loads((ROOT / "contracts" / "frontier-state-v1.schema.json").read_text())
    missing = sorted(set(schema["required"]) - set(blocked))
    assert not missing, missing
    try:
        import jsonschema  # type: ignore
    except ImportError:
        pass
    else:
        jsonschema.Draft202012Validator(schema).validate(blocked)

print("state CLI test: PASS")
