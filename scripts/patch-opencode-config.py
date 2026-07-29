#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

SCHEMA = "https://opencode.ai/config.json"


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"$schema": SCHEMA}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Refusing to edit invalid JSON config {path}: {exc}")
    if not isinstance(data, dict):
        raise SystemExit(f"Refusing to edit non-object config {path}")
    return data



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
    cleaned = "".join(out)
    import re
    return re.sub(r",\s*([}\]])", r"\1", cleaned)


def load_optional_jsonc(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(strip_jsonc(path.read_text(encoding="utf-8")))
    except Exception as exc:
        raise SystemExit(f"Refusing to continue with invalid JSONC config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"Refusing to continue with non-object config {path}")
    return data


def atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = (path.stat().st_mode & 0o777) if path.exists() else 0o600
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def playwright_entry(config_dir: Path) -> dict[str, Any]:
    command = ["npx", "-y", "@playwright/mcp@0.0.78"]
    chrome_candidates = [
        "/usr/bin/google-chrome-stable",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ]
    executable = next((p for p in chrome_candidates if Path(p).exists()), None)
    if executable:
        command.extend(["--browser=chrome", f"--executable-path={executable}"])
    command.append(f"--user-data-dir={config_dir / 'myrmex-chrome-profile'}")
    return {"command": command, "enabled": True, "type": "local"}


def engram_entry() -> dict[str, Any]:
    binary = shutil.which("engram") or "engram"
    return {"command": [binary, "mcp", "--tools=agent"], "enabled": True, "type": "local"}


def validate_config_pair(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    data = load_config(path)
    alternate = load_optional_jsonc(path.with_suffix(".jsonc"))
    for label, value in [(str(path), data.get("mcp", {})), (str(path.with_suffix(".jsonc")), alternate.get("mcp", {}))]:
        if value is not None and not isinstance(value, dict):
            raise SystemExit(f"Refusing to edit config: mcp is not an object in {label}")
    return data, alternate


def check(args: argparse.Namespace) -> None:
    path = Path(args.config).expanduser().resolve()
    data, alternate = validate_config_pair(path)
    alternate_default = alternate.get("default_agent")
    if args.set_default and isinstance(alternate_default, str) and alternate_default != "myrmex-orchestrator":
        raise SystemExit(
            "Refusing ineffective --set-default: opencode.jsonc defines "
            f"default_agent={alternate_default!r} and overrides opencode.json."
        )
    result = {
        "ok": True,
        "config_path": str(path),
        "config_exists": path.is_file(),
        "jsonc_path": str(path.with_suffix(".jsonc")),
        "jsonc_exists": path.with_suffix(".jsonc").is_file(),
        "default_agent_json": data.get("default_agent"),
        "default_agent_jsonc": alternate.get("default_agent"),
        "mcp_json": sorted((data.get("mcp") or {}).keys()),
        "mcp_jsonc": sorted((alternate.get("mcp") or {}).keys()),
    }
    print(json.dumps(result, indent=2))


def apply(args: argparse.Namespace) -> None:
    path = Path(args.config).expanduser().resolve()
    config_dir = path.parent
    data, alternate = validate_config_pair(path)
    alternate_mcp = alternate.get("mcp", {})
    original = json.loads(json.dumps(data))
    record: dict[str, Any] = {
        "config_path": str(path),
        "added_mcp": {},
        "previous_default_agent": data.get("default_agent"),
        "set_default": False,
        "changed": False,
        "mcp_supplied_by_alternate_config": [],
    }

    if not args.no_mcp:
        current_mcp = data.get("mcp", {})
        if not isinstance(current_mcp, dict):
            raise SystemExit("Refusing to edit config: mcp is not an object")
        needs_engram = "engram" not in current_mcp and "engram" not in alternate_mcp
        needs_playwright = "playwright" not in current_mcp and "playwright" not in alternate_mcp
        if needs_engram or needs_playwright:
            mcp = data.setdefault("mcp", {})
            if needs_engram:
                value = engram_entry()
                mcp["engram"] = value
                record["added_mcp"]["engram"] = value
            if needs_playwright:
                value = playwright_entry(config_dir)
                mcp["playwright"] = value
                record["added_mcp"]["playwright"] = value
        if "engram" in alternate_mcp and "engram" not in current_mcp:
            record["mcp_supplied_by_alternate_config"].append("engram")
        if "playwright" in alternate_mcp and "playwright" not in current_mcp:
            record["mcp_supplied_by_alternate_config"].append("playwright")

    alternate_default = alternate.get("default_agent")
    record["alternate_default_agent"] = alternate_default
    if args.set_default and isinstance(alternate_default, str) and alternate_default != "myrmex-orchestrator":
        raise SystemExit(
            "Refusing ineffective --set-default: opencode.jsonc defines "
            f"default_agent={alternate_default!r} and overrides opencode.json. "
            "Back up and update the authoritative JSONC explicitly, or leave Myrmex selectable but non-default."
        )
    if args.set_default and data.get("default_agent") != "myrmex-orchestrator":
        data["default_agent"] = "myrmex-orchestrator"
        record["set_default"] = True

    record["changed"] = data != original
    if record["changed"]:
        atomic_write(path, data)

    record_path = Path(args.record).expanduser().resolve()
    atomic_write(record_path, record)
    print(json.dumps(record, indent=2))


def undo(args: argparse.Namespace) -> None:
    record_path = Path(args.record).expanduser().resolve()
    if not record_path.is_file():
        print(json.dumps({"changed": False, "warning": f"record not found: {record_path}"}, indent=2))
        return
    record = json.loads(record_path.read_text(encoding="utf-8"))
    path = Path(record["config_path"])
    if not path.is_file():
        print(json.dumps({"changed": False, "warning": f"config not found: {path}"}, indent=2))
        return
    data = load_config(path)
    changed = False
    warnings: list[str] = []

    mcp = data.get("mcp")
    if isinstance(mcp, dict):
        for name, installed_value in record.get("added_mcp", {}).items():
            if mcp.get(name) == installed_value:
                del mcp[name]
                changed = True
            elif name in mcp:
                warnings.append(f"preserved modified mcp.{name}")
        if not mcp:
            data.pop("mcp", None)

    if record.get("set_default"):
        if data.get("default_agent") == "myrmex-orchestrator":
            previous = record.get("previous_default_agent")
            if previous is None:
                data.pop("default_agent", None)
            else:
                data["default_agent"] = previous
            changed = True
        else:
            warnings.append("preserved default_agent because it changed after installation")

    if changed:
        atomic_write(path, data)
    print(json.dumps({"changed": changed, "warnings": warnings}, indent=2))


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("check")
    c.add_argument("--config", required=True)
    c.add_argument("--set-default", action="store_true")
    c.set_defaults(func=check)
    a = sub.add_parser("apply")
    a.add_argument("--config", required=True)
    a.add_argument("--record", required=True)
    a.add_argument("--set-default", action="store_true")
    a.add_argument("--no-mcp", action="store_true")
    a.set_defaults(func=apply)
    u = sub.add_parser("undo")
    u.add_argument("--record", required=True)
    u.set_defaults(func=undo)
    return p


if __name__ == "__main__":
    args = parser().parse_args()
    args.func(args)
