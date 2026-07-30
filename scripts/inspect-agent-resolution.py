#!/usr/bin/env python3
"""Inspect effective OpenCode agent precedence, models, providers, and steps."""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

AGENTS = {
    "myrmex-orchestrator": None,
    "myrmex-scout": 80,
    "myrmex-worker": 110,
    "myrmex-verifier": 90,
    "myrmex-frontier": None,
}
DEFAULT_POLICY = {
    "allowed_provider_prefixes": ["openai/"],
    "require_resolved_model_for_delegation": True,
    "block_shadowed_agents": True,
}


def strip_jsonc(text: str) -> str:
    """Remove JSONC comments without touching // inside quoted strings."""
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
    return re.sub(r",\s*([}\]])", r"\1", "".join(out))


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8")
    data = json.loads(strip_jsonc(text) if path.suffix == ".jsonc" else text)
    return data if isinstance(data, dict) else {}


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def frontmatter(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}
    values: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            values[key.strip()] = value.strip().strip("\"'")
    return values


def config_for(global_dir: Path, workspace: Path) -> tuple[dict[str, Any], list[str]]:
    merged: dict[str, Any] = {}
    sources: list[str] = []
    candidates = [
        global_dir / "opencode.json",
        global_dir / "opencode.jsonc",
        workspace / "opencode.json",
        workspace / "opencode.jsonc",
    ]
    for path in candidates:
        if path.is_file():
            merged = deep_merge(merged, load_json(path))
            sources.append(str(path))
    return merged, sources


def model_from(config: dict[str, Any], agent_name: str, fm: dict[str, str]) -> tuple[str | None, str]:
    if isinstance(fm.get("model"), str) and fm["model"]:
        return fm["model"], "frontmatter"
    agent_config = config.get("agent")
    if isinstance(agent_config, dict):
        entry = agent_config.get(agent_name)
        if isinstance(entry, dict) and isinstance(entry.get("model"), str) and entry["model"]:
            return entry["model"], "config.agent"
    if isinstance(config.get("model"), str) and config["model"]:
        return config["model"], "config.model"
    return None, "unresolved"


def policy_for(config_dir: Path, explicit: str | None, packaged: Path) -> dict[str, Any]:
    policy = dict(DEFAULT_POLICY)
    candidates = [Path(explicit).expanduser()] if explicit else [config_dir / "myrmex.json", packaged]
    for path in candidates:
        if path.is_file():
            data = load_json(path)
            if isinstance(data.get("agent_policy"), dict):
                policy = deep_merge(policy, data["agent_policy"])
            break
    return policy


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--config-dir", default=os.environ.get("OPENCODE_CONFIG_DIR", "~/.config/opencode"))
    parser.add_argument("--policy", default=None)
    parser.add_argument("--enforce", action="store_true")
    args = parser.parse_args()

    workspace = Path(args.workspace).expanduser().resolve()
    config_dir = Path(args.config_dir).expanduser().resolve()
    config, config_sources = config_for(config_dir, workspace)
    packaged_profile = Path(__file__).resolve().parents[1] / "profiles" / "myrmex-defaults.json"
    policy = policy_for(config_dir, args.policy, packaged_profile)

    rows: list[dict[str, Any]] = []
    failures: list[str] = []
    warnings: list[str] = []
    for name, expected_steps in AGENTS.items():
        local_path = workspace / ".opencode" / "agents" / (name + ".md")
        global_path = config_dir / "agents" / (name + ".md")
        effective = local_path if local_path.is_file() else global_path if global_path.is_file() else None
        shadowed = local_path.is_file() and global_path.is_file()
        fm = frontmatter(effective)
        model, model_source = model_from(config, name, fm)
        provider = model.split("/", 1)[0] if model and "/" in model else None
        status = "PASS_AGENT_RESOLUTION"

        if shadowed:
            warnings.append("WARN_SHADOWED_AGENT:" + name)
            status = "WARN_SHADOWED_AGENT"
            if policy.get("block_shadowed_agents") and args.enforce:
                failures.append("WARN_SHADOWED_AGENT:" + name)
        if effective is None:
            status = "AGENT_NOT_INSTALLED"
            if args.enforce:
                failures.append(status + ":" + name)

        actual_steps = fm.get("steps") if effective else None
        if effective is not None and expected_steps is not None and actual_steps != str(expected_steps):
            status = "FAIL_INVALID_AGENT_STEPS"
            if args.enforce:
                failures.append(status + ":" + name)

        requires_model = name not in {"myrmex-orchestrator", "myrmex-frontier"}
        if requires_model and policy.get("require_resolved_model_for_delegation") and not model:
            status = "AGENT_MODEL_UNRESOLVED"
            if args.enforce:
                failures.append(status + ":" + name)
        if model and not any(model.startswith(prefix) for prefix in policy.get("allowed_provider_prefixes", [])):
            status = "BLOCKED_NON_ALLOWED_PROVIDER"
            if args.enforce:
                failures.append(status + ":" + name)

        rows.append({
            "agent": name,
            "effective_source": str(effective) if effective else None,
            "global_source": str(global_path) if global_path.is_file() else None,
            "shadowed": shadowed,
            "model": model,
            "model_source": model_source,
            "provider": provider,
            "steps": int(actual_steps) if actual_steps and actual_steps.isdigit() else actual_steps,
            "status": status,
        })

    result = {
        "ok": not failures,
        "policy": policy,
        "config_sources": config_sources,
        "agents": rows,
        "warnings": warnings,
        "errors": failures,
        "credential_visibility": "CREDENTIAL_NOT_VISIBLE_TO_ORCHESTRATOR is informational; environment credentials are not inspected for delegation readiness",
    }
    print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
