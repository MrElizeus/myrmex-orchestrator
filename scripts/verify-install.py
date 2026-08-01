#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def strip_jsonc(text: str) -> str:
    out: list[str] = []
    i = 0
    in_string = False
    escaped = False
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "/":
            i += 2
            while i < len(text) and text[i] not in "\r\n":
                i += 1
            continue
        if ch == "/" and nxt == "*":
            i += 2
            while i + 1 < len(text) and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    import re

    return re.sub(r",\s*([}\]])", r"\1", "".join(out))


def load_config_file(path: Path) -> dict:
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8")
    data = json.loads(strip_jsonc(text) if path.suffix == ".jsonc" else text)
    return data if isinstance(data, dict) else {}


def frontmatter(text: str) -> str:
    if not text.startswith("---\n"):
        raise ValueError("missing opening frontmatter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise ValueError("missing closing frontmatter")
    return text[4:end]


def field(fm: str, name: str) -> str | None:
    prefix = name + ":"
    for line in fm.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip().strip('"\'')
    return None


def require_text(path: Path, needles: list[str], errors: list[str]) -> None:
    text = path.read_text(encoding="utf-8")
    for needle in needles:
        if needle not in text:
            errors.append(f"{path.name}: expected safety rule not found: {needle}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--config-dir", default=os.environ.get("OPENCODE_CONFIG_DIR", "~/.config/opencode"))
    p.add_argument("--bin-dir", default=os.environ.get("MYRMEX_BIN_DIR", "~/.local/bin"))
    p.add_argument("--json", action="store_true")
    args = p.parse_args()
    config_dir = Path(args.config_dir).expanduser().resolve()
    bin_dir = Path(args.bin_dir).expanduser().resolve()
    errors: list[str] = []
    warnings: list[str] = []
    checks: list[str] = []

    expected_agents = {
        "myrmex-orchestrator.md": "primary",
        "myrmex-worker.md": "subagent",
        "myrmex-verifier.md": "subagent",
        "myrmex-scout.md": "subagent",
        "myrmex-frontier.md": "subagent",
    }
    agent_paths: dict[str, Path] = {}
    for name, mode in expected_agents.items():
        path = config_dir / "agents" / name
        agent_paths[name] = path
        if not path.is_file():
            errors.append(f"missing {path}")
            continue
        try:
            fm = frontmatter(path.read_text(encoding="utf-8"))
            if field(fm, "mode") != mode:
                errors.append(f"{name}: expected mode {mode}")
            checks.append(f"agent:{name}")
        except Exception as exc:
            errors.append(f"{name}: {exc}")

    if all(path.is_file() for path in agent_paths.values()):
        require_text(
            agent_paths["myrmex-scout.md"],
            ["edit: deny", "task: deny", '"mem_*": deny', '"playwright_*": deny', '"*": deny'],
            errors,
        )
        require_text(
            agent_paths["myrmex-worker.md"],
            ["task: deny", '"mem_*": deny', '"playwright_*": deny', '"git commit*": deny', '"git push*": deny', '"myrmex-memory*": deny', '"myrmex-git-delivery": deny'],
            errors,
        )
        require_text(
            agent_paths["myrmex-verifier.md"],
            ["edit: deny", "task: deny", '"mem_*": deny', '"git commit*": deny', '"git push*": deny', '"myrmex-memory*": deny', '"myrmex-git-delivery": deny'],
            errors,
        )
        require_text(
            agent_paths["myrmex-frontier.md"],
            ['"*": deny', "read: deny", "edit: deny", "bash: deny", "task: deny", '"mem_*": deny', '"playwright_*": allow'],
            errors,
        )
        require_text(
            agent_paths["myrmex-orchestrator.md"],
            ['"myrmex-frontier": allow', '"playwright_*": deny', '"git push --force*": deny'],
            errors,
        )
        checks.append("agent-safety-invariants")

    for name in ["myrmex-delegation", "myrmex-frontier-delegation", "myrmex-memory", "myrmex-git-delivery"]:
        path = config_dir / "skills" / name / "SKILL.md"
        if not path.is_file():
            errors.append(f"missing {path}")
            continue
        try:
            fm = frontmatter(path.read_text(encoding="utf-8"))
            if field(fm, "name") != name:
                errors.append(f"{path}: skill name mismatch")
            checks.append(f"skill:{name}")
        except Exception as exc:
            errors.append(f"{path}: {exc}")

    frontier_refs = [
        "state-machine.md", "local-and-engram-state.md", "repository-context.md",
        "browser-transport.md", "response-validation.md", "delegation-and-verification.md",
        "security.md", "recovery.md", "git-and-completion.md",
    ]
    for name in frontier_refs:
        path = config_dir / "skills" / "myrmex-frontier-delegation" / "references" / name
        if not path.is_file():
            errors.append(f"missing {path}")
    if all((config_dir / "skills" / "myrmex-frontier-delegation" / "references" / name).is_file() for name in frontier_refs):
        checks.append("frontier-reference-set")

    expected_commands = [
        "myrmex-doctor.md",
        "myrmex-frontier.md",
        "myrmex-frontier-interactive.md",
        "myrmex-direct.md",
        "myrmex-delegate.md",
        "myrmex-resume.md",
        "myrmex-status.md",
    ]
    for name in expected_commands:
        path = config_dir / "commands" / name
        if not path.is_file():
            errors.append(f"missing {path}")
        else:
            checks.append(f"command:{name}")


    state_bin = bin_dir / "myrmex-state"
    if not state_bin.is_file():
        errors.append(f"missing {state_bin}")
    elif not os.access(state_bin, os.X_OK):
        errors.append(f"state CLI is not executable: {state_bin}")
    else:
        try:
            with tempfile.TemporaryDirectory(prefix="myrmex-doctor-") as td:
                env = os.environ.copy()
                env["MYRMEX_STATE_HOME"] = td
                proc = subprocess.run([str(state_bin), "doctor"], capture_output=True, text=True, timeout=20, env=env)
            if proc.returncode != 0:
                errors.append(f"myrmex-state doctor failed: {proc.stderr.strip() or proc.stdout.strip()}")
            else:
                payload = json.loads(proc.stdout)
                if not payload.get("ok"):
                    errors.append("myrmex-state doctor returned ok=false")
                else:
                    checks.append(f"myrmex-state:{state_bin}")
        except Exception as exc:
            errors.append(f"myrmex-state doctor error: {exc}")

    memory_bin = bin_dir / "myrmex-memory"
    if not memory_bin.is_file():
        errors.append(f"missing {memory_bin}")
    elif not os.access(memory_bin, os.X_OK):
        errors.append(f"memory CLI is not executable: {memory_bin}")
    else:
        try:
            with tempfile.TemporaryDirectory(prefix="myrmex-memory-doctor-") as td:
                env = os.environ.copy()
                env["MYRMEX_MEMORY_HOME"] = td
                proc = subprocess.run([str(memory_bin), "doctor"], capture_output=True, text=True, timeout=20, env=env)
            if proc.returncode != 0:
                errors.append(f"myrmex-memory doctor failed: {proc.stderr.strip() or proc.stdout.strip()}")
            else:
                payload = json.loads(proc.stdout)
                if not payload.get("ok"):
                    errors.append("myrmex-memory doctor returned ok=false")
                else:
                    checks.append(f"myrmex-memory:{memory_bin}")
        except Exception as exc:
            errors.append(f"myrmex-memory doctor error: {exc}")

    resolution_script = Path(__file__).with_name("inspect-agent-resolution.py")
    if resolution_script.is_file():
        try:
            resolution = subprocess.run(
                [str(resolution_script), "--workspace", str(Path.cwd()), "--config-dir", str(config_dir), "--enforce"],
                capture_output=True, text=True, timeout=20,
            )
            payload = json.loads(resolution.stdout)
            checks.append("agent-resolution")
            if resolution.returncode != 0:
                errors.append("effective agent resolution failed")
            for agent in payload.get("agents", []):
                status = agent.get("status")
                if status == "WARN_SHADOWED_AGENT":
                    warnings.append("shadowed local agent: " + str(agent.get("agent")))
                elif status in {"FAIL_INVALID_AGENT_STEPS", "AGENT_NOT_INSTALLED",
                                 "AGENT_MODEL_UNRESOLVED", "BLOCKED_NON_ALLOWED_PROVIDER"}:
                    errors.append(status + ": " + str(agent.get("agent")))
        except Exception as exc:
            warnings.append(f"agent resolution unavailable: {exc}")

    path_entries = [Path(item).expanduser().resolve() for item in os.environ.get("PATH", "").split(os.pathsep) if item]
    if bin_dir not in path_entries:
        warnings.append(f"{bin_dir} is not on PATH; add it before invoking myrmex-state or myrmex-memory by name")

    myrmex_config = config_dir / "myrmex.json"
    if myrmex_config.is_file():
        try:
            data = json.loads(myrmex_config.read_text(encoding="utf-8"))
            if data.get("$schema") != "myrmex.config/v1":
                warnings.append("myrmex.json has an unexpected schema")
            else:
                checks.append("myrmex-config")
        except Exception as exc:
            errors.append(f"invalid {myrmex_config}: {exc}")
    else:
        warnings.append("myrmex.json is absent; built-in frontier defaults will be used")

    record_path = config_dir / "myrmex-orchestrator" / "install-record.json"
    record: dict | None = None
    if record_path.is_file():
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
            for item in record.get("files", []):
                path = Path(item["path"])
                if not path.is_file():
                    errors.append(f"recorded file missing: {path}")
                elif sha(path) != item["sha256"]:
                    warnings.append(f"installed file modified after installation: {path}")
            checks.append("install-record")
        except Exception as exc:
            errors.append(f"invalid install record: {exc}")
    else:
        warnings.append("install record missing")

    config_path = config_dir / "opencode.json"
    jsonc_path = config_dir / "opencode.jsonc"
    merged_mcp: dict = {}
    found_config = False
    default_agents: list[str] = []
    for candidate in [config_path, jsonc_path]:
        if not candidate.is_file():
            continue
        found_config = True
        try:
            data = load_config_file(candidate)
            value = data.get("mcp", {})
            if isinstance(value, dict):
                merged_mcp.update(value)
            if isinstance(data.get("default_agent"), str):
                default_agents.append(data["default_agent"])
        except Exception as exc:
            errors.append(f"invalid {candidate}: {exc}")
    if not found_config:
        warnings.append("OpenCode config file missing")
    if "engram" not in merged_mcp:
        warnings.append("Memory MCP is not present; persistent memory/recovery is degraded")
    else:
        checks.append("mcp:engram")
    if "playwright" not in merged_mcp:
        warnings.append("Browser MCP is not present; frontier route is unavailable")
    else:
        checks.append("mcp:playwright")
    if default_agents:
        checks.append("default-agent-effective-candidate:" + default_agents[-1])
        if len(set(default_agents)) > 1:
            warnings.append(
                "opencode.json and opencode.jsonc define different default_agent values; "
                "OpenCode loads JSONC after JSON, so the later value normally wins"
            )


    result = {
        "ok": not errors,
        "config_dir": str(config_dir),
        "bin_dir": str(bin_dir),
        "checks": checks,
        "warnings": warnings,
        "errors": errors,
    }
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Myrmex verification: {'PASS' if result['ok'] else 'FAIL'}")
        print(f"Config: {config_dir}")
        for item in checks:
            print(f"  OK   {item}")
        for item in warnings:
            print(f"  WARN {item}")
        for item in errors:
            print(f"  ERR  {item}")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
