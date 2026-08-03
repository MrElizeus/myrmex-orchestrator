#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATCHER = ROOT / "scripts" / "patch-opencode-config.py"
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")


def run(*args: str, ok: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["python3", str(PATCHER), *args], capture_output=True, text=True, env=ENV, timeout=20)
    if ok and result.returncode != 0:
        raise AssertionError(f"patcher failed: {args}\nstdout={result.stdout}\nstderr={result.stderr}")
    if not ok and result.returncode == 0:
        raise AssertionError(f"patcher unexpectedly succeeded: {args}")
    return result


with tempfile.TemporaryDirectory(prefix="myrmex-patcher-") as td:
    base = Path(td)

    # Existing MCP entries and unrelated fields are preserved; undo removes only Myrmex additions.
    config = base / "opencode.json"
    record = base / "change.json"
    config.write_text(
        json.dumps({
            "$schema": "https://opencode.ai/config.json",
            "default_agent": "existing-default-agent",
            "mcp": {"playwright": {"type": "remote", "url": "https://example.invalid"}},
            "custom": {"x": 1},
        }) + "\n"
    )
    run("check", "--config", str(config))
    run("apply", "--config", str(config), "--record", str(record), "--set-default")
    data = json.loads(config.read_text())
    assert data["default_agent"] == "myrmex-orchestrator"
    assert data["mcp"]["playwright"]["type"] == "remote"
    assert "engram" in data["mcp"]
    assert data["custom"] == {"x": 1}
    run("undo", "--record", str(record))
    data = json.loads(config.read_text())
    assert data["default_agent"] == "existing-default-agent"
    assert data["mcp"]["playwright"]["type"] == "remote"
    assert "engram" not in data["mcp"]
    assert data["custom"] == {"x": 1}

    # JSONC-supplied MCP is recognized without rewriting or duplicating it.
    config2 = base / "pair" / "opencode.json"
    config2.parent.mkdir()
    config2.write_text('{"$schema":"https://opencode.ai/config.json"}\n')
    jsonc2 = config2.with_suffix(".jsonc")
    jsonc_original = '''{
      // authoritative later config
      "default_agent": "existing-default-agent",
      "mcp": {
        "engram": {"type": "local", "command": ["/custom/engram", "mcp"]},
        "playwright": {"type": "local", "command": ["npx", "@playwright/mcp@pinned"]},
      },
    }
'''
    jsonc2.write_text(jsonc_original)
    record2 = base / "pair-change.json"
    run("apply", "--config", str(config2), "--record", str(record2))
    assert jsonc2.read_text() == jsonc_original
    assert "mcp" not in json.loads(config2.read_text())
    run("check", "--config", str(config2), "--set-default", ok=False)
    run("apply", "--config", str(config2), "--record", str(record2), "--set-default", ok=False)

    # Invalid JSONC is a hard stop rather than being silently ignored.
    bad = base / "bad" / "opencode.json"
    bad.parent.mkdir()
    bad.write_text("{}\n")
    bad.with_suffix(".jsonc").write_text('{ "mcp": ')  # malformed
    run("check", "--config", str(bad), ok=False)

    # A newly generated Playwright entry is pinned and profile-scoped.
    clean = base / "clean" / "opencode.json"
    clean.parent.mkdir()
    clean.write_text("{}\n")
    clean_record = base / "clean-change.json"
    run("apply", "--config", str(clean), "--record", str(clean_record))
    generated = json.loads(clean.read_text())["mcp"]["playwright"]["command"]
    assert "@playwright/mcp@0.0.78" in generated
    assert all("@latest" not in part for part in generated)
    assert any(part.startswith("--user-data-dir=") for part in generated)

    # Existing child symlinks cannot redirect generated profile or transport
    # output into the repository or outside the explicit artifact root.
    repository = base / "repository"
    repository.mkdir()
    for child in ("browser-profile", "transport-output"):
        isolated_root = base / ("symlink-" + child)
        isolated_root.mkdir()
        (isolated_root / child).symlink_to(repository, target_is_directory=True)
        isolated_config = isolated_root / "opencode.json"
        isolated_config.write_text("{}\n")
        before = isolated_config.read_bytes()
        run(
            "apply", "--config", str(isolated_config), "--record", str(isolated_root / "change.json"),
            "--artifact-root", str(isolated_root), ok=False,
        )
        assert isolated_config.read_bytes() == before

print("config patcher test: PASS")
