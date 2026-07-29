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

    run("lock", run_id, "--owner", "test-owner", env=env)
    run("lock", run_id, "--owner", "other-owner", env=env, ok=False)
    run("lock", run_id, "--owner", "test-owner", env=env)

    recorded = json.loads(run("delegation", run_id, "--agent", "myrmex-worker", "--role", "writer", "--reason", "bounded implementation", "--task-id", "task-1", "--work-unit-id", "unit-1", "--status", "success", env=env).stdout)
    assert recorded["attempts"]["writers"] == 1
    assert recorded["delegation_ledger"][0]["work_unit_id"] == "unit-1"
    defects = json.loads(run("defects", run_id, "--defects-json", '["missing assertion"]', env=env).stdout)
    assert defects["defect_history"][0]["remaining"] == ["missing assertion"]
    assert defects["verification_revision"] == 1

    patched = json.loads(run("patch", run_id, "--expect-revision", "2", "--set", "phase=collecting-context", env=env).stdout)
    assert patched["revision"] == 3
    run("patch", run_id, "--expect-revision", "2", "--set", "phase=requesting-plan", env=env, ok=False)

    artifact = Path(run("artifact-path", run_id, "context.json", env=env).stdout.strip())
    assert artifact.parent.name == "artifacts" and artifact.name == "context.json"

    run("unlock", run_id, "--owner", "other-owner", env=env, ok=False)
    run("unlock", run_id, "--owner", "test-owner", env=env)
    doctor = json.loads(run("doctor", env=env).stdout)
    assert doctor["ok"] is True and doctor["runs"] == 1

    schema = json.loads((ROOT / "contracts" / "frontier-state-v1.schema.json").read_text())
    missing = sorted(set(schema["required"]) - set(patched))
    assert not missing, missing
    try:
        import jsonschema  # type: ignore
    except ImportError:
        pass
    else:
        jsonschema.Draft202012Validator(schema).validate(patched)

print("state CLI test: PASS")
