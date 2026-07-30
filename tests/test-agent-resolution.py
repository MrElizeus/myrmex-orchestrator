#!/usr/bin/env python3
import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).parents[1]
AGENT = """---
description: test worker
mode: subagent
hidden: true
steps: 110
---
test
"""
with tempfile.TemporaryDirectory(prefix="myrmex-resolution-") as td:
    root = Path(td)
    config = root / "config"
    workspace = root / "workspace"
    local = workspace / ".opencode" / "agents"
    global_dir = config / "agents"
    local.mkdir(parents=True)
    global_dir.mkdir(parents=True)

    (config / "opencode.jsonc").write_text(
        '{\n'
        '  "$schema": "https://opencode.ai/config.json",\n'
        '  // URL above must survive JSONC parsing\n'
        '  "model": "openai/global-model",\n'
        '  "agent": {"myrmex-worker": {"model": "openai/global-worker"}}\n'
        '}\n'
    )
    (config / "myrmex.json").write_text(json.dumps({
        "$schema": "myrmex.config/v1",
        "agent_policy": {"allowed_provider_prefixes": ["openai/", "custom/"]},
    }))
    (global_dir / "myrmex-worker.md").write_text(AGENT)
    (workspace / "opencode.json").write_text(json.dumps({
        "model": "openai/workspace-model",
        "agent": {"myrmex-worker": {"model": "custom/workspace-worker"}},
    }))
    (local / "myrmex-worker.md").write_text(AGENT.replace("steps: 110", "steps: 110"))

    proc = subprocess.run(
        ["python3", str(ROOT / "scripts/inspect-agent-resolution.py"),
         "--workspace", str(workspace), "--config-dir", str(config)],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(proc.stdout)
    row = next(item for item in data["agents"] if item["agent"] == "myrmex-worker")
    assert row["model"] == "custom/workspace-worker", row
    assert row["model_source"] == "config.agent", row
    assert row["shadowed"] is True, row

    (local / "myrmex-worker.md").write_text(AGENT.replace(
        "steps: 110", "steps: 110\nmodel: openai/frontmatter-worker"
    ))
    proc = subprocess.run(
        ["python3", str(ROOT / "scripts/inspect-agent-resolution.py"),
         "--workspace", str(workspace), "--config-dir", str(config)],
        capture_output=True, text=True, check=True,
    )
    row = next(item for item in json.loads(proc.stdout)["agents"] if item["agent"] == "myrmex-worker")
    assert row["model"] == "openai/frontmatter-worker", row
    assert row["model_source"] == "frontmatter", row

    (local / "myrmex-worker.md").write_text(AGENT.replace("steps: 110", "steps: 999"))
    blocked = subprocess.run(
        ["python3", str(ROOT / "scripts/inspect-agent-resolution.py"),
         "--workspace", str(workspace), "--config-dir", str(config), "--enforce"],
        capture_output=True, text=True,
    )
    assert blocked.returncode != 0
    assert "FAIL_INVALID_AGENT_STEPS" in blocked.stdout

print("agent resolution, JSONC, precedence, and policy test: PASS")
