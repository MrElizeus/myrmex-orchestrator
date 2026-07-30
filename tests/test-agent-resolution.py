#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "inspect-agent-resolution.py"
STEPS = {
    "myrmex-orchestrator": None,
    "myrmex-scout": 80,
    "myrmex-worker": 110,
    "myrmex-verifier": 90,
    "myrmex-frontier": None,
}


def agent(name: str, steps: int | None, model: str | None = None) -> str:
    fields = ["---", f"description: test {name}", "mode: subagent", "hidden: true"]
    if steps is not None:
        fields.append(f"steps: {steps}")
    if model:
        fields.append(f"model: {model}")
    return "\n".join([*fields, "---", "test", ""])


def invoke(workspace: Path, config: Path, *, enforce: bool = False, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    args = ["python3", str(SCRIPT), "--workspace", str(workspace), "--config-dir", str(config)]
    if enforce:
        args.append("--enforce")
    return subprocess.run(args, capture_output=True, text=True, env=env, check=False)


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
        "agent_policy": {"allowed_provider_prefixes": ["openai/", "custom/"], "block_shadowed_agents": False},
    }))
    for name, step_count in STEPS.items():
        (global_dir / f"{name}.md").write_text(agent(name, step_count))
    (workspace / "opencode.json").write_text(json.dumps({
        "model": "openai/workspace-model",
        "agent": {"myrmex-worker": {"model": "custom/workspace-worker"}},
    }))
    (local / "myrmex-worker.md").write_text(agent("myrmex-worker", 110))

    proc = invoke(workspace, config)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    row = next(item for item in data["agents"] if item["agent"] == "myrmex-worker")
    assert row["model"] == "custom/workspace-worker", row
    assert row["model_source"] == "config.agent", row
    assert row["shadowed"] is True, row

    # OpenCode can route a configured agent even when the parent process cannot
    # see a provider variable. The resolver must not block on that absence.
    no_credential_env = dict(os.environ)
    no_credential_env.pop("OPENCODE_GO_API_KEY", None)
    no_credential_env.pop("OPENAI_API_KEY", None)
    no_credential = invoke(workspace, config, enforce=True, env=no_credential_env)
    assert no_credential.returncode == 0, no_credential.stdout
    assert "CREDENTIAL_NOT_VISIBLE_TO_ORCHESTRATOR is informational" in json.loads(no_credential.stdout)["credential_visibility"]

    (local / "myrmex-worker.md").write_text(agent("myrmex-worker", 110, "openai/frontmatter-worker"))
    proc = invoke(workspace, config)
    row = next(item for item in json.loads(proc.stdout)["agents"] if item["agent"] == "myrmex-worker")
    assert row["model"] == "openai/frontmatter-worker", row
    assert row["model_source"] == "frontmatter", row

    (local / "myrmex-worker.md").write_text(agent("myrmex-worker", 999))
    invalid_steps = invoke(workspace, config, enforce=True)
    assert invalid_steps.returncode != 0
    assert "FAIL_INVALID_AGENT_STEPS" in invalid_steps.stdout

    missing_config = root / "missing-config"
    missing_workspace = root / "missing-workspace"
    (missing_config / "agents").mkdir(parents=True)
    (missing_config / "opencode.json").write_text(json.dumps({"model": "openai/test"}))
    missing = invoke(missing_workspace, missing_config, enforce=True)
    assert missing.returncode != 0 and "AGENT_NOT_INSTALLED" in missing.stdout

    unresolved_config = root / "unresolved-config"
    unresolved_workspace = root / "unresolved-workspace"
    unresolved_agents = unresolved_config / "agents"
    unresolved_agents.mkdir(parents=True)
    for name, step_count in STEPS.items():
        (unresolved_agents / f"{name}.md").write_text(agent(name, step_count))
    unresolved = invoke(unresolved_workspace, unresolved_config, enforce=True)
    assert unresolved.returncode != 0 and "AGENT_MODEL_UNRESOLVED" in unresolved.stdout

print("agent resolution, credential visibility, JSONC, precedence, and policy test: PASS")
