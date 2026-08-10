#!/usr/bin/env python3
"""Focused regression test for observed malformed Frontier correction intents."""
from __future__ import annotations

import copy
import json
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "bin" / "myrmex-state"


def run(*args: str, env: dict[str, str], ok: bool = True) -> dict:
    result = subprocess.run([str(STATE), *args], capture_output=True, text=True, env=env, timeout=30)
    if ok and result.returncode:
        raise AssertionError(f"{args}: {result.stderr}")
    if not ok and result.returncode == 0:
        raise AssertionError(f"unexpected success: {args}")
    return json.loads(result.stdout) if result.stdout else {}


with tempfile.TemporaryDirectory(prefix="myrmex-observed-successor-") as td:
    env = dict(os.environ, MYRMEX_STATE_HOME=str(Path(td) / "state"), PYTHONDONTWRITEBYTECODE="1")
    repo = str(Path(td) / "repo")
    Path(repo).mkdir()
    run_id = subprocess.run(
        [str(STATE), "init", "--run-id", "observed-successor", "--objective", "plan",
         "--repository-root", repo, "--mode", "autonomous", "--scope", "narrow", "--execution-policy", "auto"],
        capture_output=True, text=True, env=env, check=True,
    ).stdout.strip()
    started = run("frontier", run_id, "start", "--request-id", "req-old", "--task-id", "task-old",
                  "--chat-url", "https://frontier.example/chat/1", "--intent-json",
                  '{"purpose":"planning","request_payload":"same-purpose"}', "--expect-revision", "0", env=env)
    predecessor_id = started["pending_operations"][0]["operation_id"]
    failed = run("frontier", run_id, "result", "--operation-id", predecessor_id, "--request-id", "req-old",
                 "--message-id", "turn-old", "--transport-status", "malformed", "--effect-stage",
                 "response_observed", "--effect-json", '{"request_id":"req-old","message_id":"turn-old"}',
                 "--receipt-json", '{"request_id":"req-old","message_id":"turn-old"}', "--expect-revision", "1", env=env)
    original = copy.deepcopy(failed["pending_operations"][0])
    recovered = run("frontier", run_id, "recover", "--operation-id", predecessor_id, "--request-id", "req-old",
                    "--message-id", "turn-old", "--transport-status", "success", "--frontier-decision", "BLOCKED",
                    "--response-type", "unknown", "--effect-stage", "response_observed",
                    "--effect-json", '{"request_id":"req-old","message_id":"turn-old"}',
                    "--receipt-json", '{"request_id":"req-old","message_id":"turn-old"}', "--expect-revision", "2", env=env)
    assert recovered["pending_operations"][0]["effective_response_type"] == "unknown"
    # Successors cannot revive an active run or accept purpose/chat mutations.
    run("frontier", run_id, "successor", "--operation-id", predecessor_id, "--request-id", "req-new",
        "--task-id", "task-new", "--expect-revision", "3", env=env, ok=False)
    run("frontier", run_id, "successor", "--operation-id", predecessor_id, "--request-id", "req-new",
        "--task-id", "task-new", "--chat-url", "https://frontier.example/chat/2", "--expect-revision", "3", env=env, ok=False)
    run("frontier", run_id, "successor", "--operation-id", predecessor_id, "--request-id", "req-new",
        "--task-id", "task-new", "--intent-json", '{"purpose":"other"}', "--expect-revision", "3", env=env, ok=False)
    omitted = subprocess.run([str(STATE), "frontier", run_id, "successor", "--operation-id", predecessor_id,
                              "--request-id", "req-new", "--task-id", "task-new"], capture_output=True, text=True, env=env)
    assert omitted.returncode != 0 and "expect-revision" in omitted.stderr
    # Make the blocker explicit, as it is after a crash/restart in production.
    run("transition", run_id, "--to-phase", "collecting-context", "--reason", "context", "--expect-revision", "3", env=env)
    run("transition", run_id, "--to-phase", "requesting-plan", "--reason", "request", "--expect-revision", "4", env=env)
    blocked = run("transition", run_id, "--to-phase", "blocked", "--reason", "observed response",
                  "--blocker", "BLOCKED_FRONTIER_RECOVERY", "--recovery-code", "BLOCKED_FRONTIER_RECOVERY",
                  "--recovery-operation-id", predecessor_id, "--recovery-resume-phase", "requesting-plan",
                  "--expect-revision", "5", env=env)
    successor = run("frontier", run_id, "successor", "--operation-id", predecessor_id, "--request-id", "req-new",
                    "--task-id", "task-new", "--intent-json", '{"correction_metadata":{"attempt":2}}',
                    "--expect-revision", "6", env=env)
    pred = successor["pending_operations"][0]
    succ = successor["pending_operations"][1]
    assert successor["status"] == "active" and successor["phase"] == "requesting-plan"
    assert succ["intent"]["chat_url"] == "https://frontier.example/chat/1"
    assert pred["effect"] == original["effect"] and pred["effective_outcome"] == recovered["pending_operations"][0]["effective_outcome"]
    assert pred.get("pre_effect_absence_proven") is not True
    replay = run("frontier", run_id, "successor", "--operation-id", predecessor_id, "--request-id", "req-new",
                  "--task-id", "task-new", "--intent-json", '{"correction_metadata":{"attempt":2}}',
                  "--expect-revision", str(successor["revision"]), env=env)
    assert replay["revision"] == successor["revision"] and len(replay["pending_operations"]) == 2
    run("frontier", run_id, "successor", "--operation-id", predecessor_id, "--request-id", "req-new",
        "--task-id", "task-other", "--expect-revision", str(successor["revision"]), env=env, ok=False)
    run("frontier", run_id, "successor", "--operation-id", predecessor_id, "--request-id", "req-new",
        "--task-id", "task-new", "--intent-json", '{"correction_metadata":{"attempt":3}}',
        "--expect-revision", str(successor["revision"]), env=env, ok=False)
    run("frontier", run_id, "result", "--operation-id", succ["operation_id"], "--request-id", "req-new",
        "--message-id", "turn-new", "--transport-status", "success", "--frontier-decision", "ACCEPT",
        "--response-type", "plan", "--plan-json", '{"work_unit_id":"WU-1"}',
        "--effect-json", '{"request_id":"req-new","message_id":"turn-new"}',
        "--receipt-json", '{"request_id":"req-new","message_id":"turn-new"}',
        "--expect-revision", str(successor["revision"]), env=env)
    final = run("show", run_id, env=env)
    assert final["pending_operations"][0]["status"] == "superseded"
    assert final["pending_operations"][0]["effect"] == original["effect"]
    assert "successor_pending_operation_id" not in final["recovery"]
    run("frontier", run_id, "successor", "--operation-id", predecessor_id, "--request-id", "req-other",
        "--task-id", "task-other", "--expect-revision", str(final["revision"]), env=env, ok=False)

print("frontier observed-response successor test: PASS")
