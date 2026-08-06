#!/usr/bin/env python3
"""
OpenCode Transport Module for Myrmex Head (Production Execution Driver).
Encapsulates real interaction with the OpenCode CLI / process runtime.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# Path to OpenCode executable
OPENCODE_BIN = os.environ.get("OPENCODE_BIN") or str(
    Path.home() / ".opencode/bin/opencode"
)
if not Path(OPENCODE_BIN).exists():
    OPENCODE_BIN = "opencode"


@dataclass
class TaskIdentity:
    task_id: str  # OpenCode session ID (e.g. ses_...)
    agent: str
    model: str | None
    workspace: str
    created_at: str


@dataclass
class TaskSnapshot:
    task_id: str
    status: str  # intent, dispatching, task-observed, running, completed, failed, cancelled, ambiguous
    agent: str
    model: str | None
    workspace: str
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    exit_code: int | None = None
    error_type: str | None = None
    raw_export: dict[str, Any] | None = None


@dataclass
class TaskResult:
    task_id: str
    status: str
    agent: str
    text_content: str
    json_payload: dict[str, Any] | None
    tokens: dict[str, Any]
    cost: float
    model: str | None
    finish_reason: str | None
    error_type: str | None = None


def sanitize_text(text: str) -> str:
    """Redacts potential tokens or sensitive secrets from logs and output text."""
    if not text:
        return text
    # Redact common token patterns: ghp_..., sk-..., bearer tokens, auth header values
    redacted = re.sub(
        r"(sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{20,}|Bearer\s+[A-Za-z0-9_.-]{20,})",
        "[REDACTED_SECRET]",
        text,
    )
    # Redact env var exports containing passwords or tokens
    redacted = re.sub(
        r"(OPENAI_API_KEY|GITHUB_TOKEN|SECRET|PASSWORD)=['\"][^'\"]+['\"]",
        r"\1=[REDACTED]",
        redacted,
    )
    return redacted


def _state_dir() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    d = Path(xdg) / "myrmex/task-operations"
    d.mkdir(parents=True, exist_ok=True)
    return d


def create_task(
    spec: dict[str, Any],
    transport_state_dir: Path | None = None,
) -> tuple[TaskIdentity, subprocess.Popen[str]]:
    """
    Creates and dispatches an OpenCode Task via background execution stream.
    Requires spec to contain:
    - prompt: str
    - agent: str
    - workspace: str (Path to worktree)
    - model: str | None (optional)
    """
    prompt = spec["prompt"]
    agent = spec["agent"]
    workspace = str(Path(spec["workspace"]).resolve())
    model = spec.get("model") or "opencode/deepseek-v4-flash-free"
    env = dict(os.environ)

    cmd = [
        OPENCODE_BIN,
        "run",
        prompt,
        "--agent",
        agent,
        "--dir",
        workspace,
        "--format",
        "json",
        "--auto",
    ]
    if model:
        cmd.extend(["--model", model])

    # Launch background process with stdout/stderr piped
    proc = subprocess.Popen(
        cmd,
        cwd=workspace,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        bufsize=1,
    )

    # Read initial JSON events from stdout to capture sessionID
    session_id: str | None = None
    start_time = time.time()
    initial_events: list[str] = []

    assert proc.stdout is not None
    # Read output non-blockingly or up to first sessionID event (within 15s timeout)
    while time.time() - start_time < 15.0:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                break
            time.sleep(0.1)
            continue
        initial_events.append(line)
        try:
            evt = json.loads(line)
            if isinstance(evt, dict) and "sessionID" in evt:
                session_id = evt["sessionID"]
                break
        except Exception:
            pass

    if not session_id:
        # Check if process exited with error
        if proc.poll() is not None and proc.returncode != 0:
            stderr_out = proc.stderr.read() if proc.stderr else ""
            err_msg = sanitize_text(stderr_out or "".join(initial_events))
            if "Authentication" in err_msg or "401" in err_msg or "unauthorized" in err_msg.lower():
                raise RuntimeError(f"OPENCODE_PROVIDER_AUTHENTICATION_FAILED: {err_msg}")
            if "Provider" in err_msg or "unavailable" in err_msg.lower() or "503" in err_msg:
                raise RuntimeError(f"OPENCODE_PROVIDER_UNAVAILABLE: {err_msg}")
            raise RuntimeError(f"OPENCODE_TASK_IDENTITY_MISSING: Failed to capture sessionID. Stderr: {err_msg}")
        # Generate deterministic fallback session ID if process is still running but session ID not yet parsed
        session_id = f"ses_opencode_{proc.pid}_{int(time.time()*1000)}"

    created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    identity = TaskIdentity(
        task_id=session_id,
        agent=agent,
        model=model,
        workspace=workspace,
        created_at=created_at,
    )

    # Save process PID mapping for cancellation & status checking
    meta_dir = transport_state_dir or _state_dir()
    meta_path = meta_dir / f"{session_id}.proc.json"
    meta_data = {
        "task_id": session_id,
        "pid": proc.pid,
        "agent": agent,
        "model": model,
        "workspace": workspace,
        "created_at": created_at,
        "cmd": cmd,
    }
    meta_path.write_text(json.dumps(meta_data, indent=2), encoding="utf-8")

    return identity, proc


def get_task(task_id: str, transport_state_dir: Path | None = None) -> TaskSnapshot:
    """Fetches task status and snapshot via OpenCode CLI export & process check."""
    meta_dir = transport_state_dir or _state_dir()
    meta_path = meta_dir / f"{task_id}.proc.json"
    proc_meta: dict[str, Any] = {}
    if meta_path.is_file():
        try:
            proc_meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    pid = proc_meta.get("pid")
    is_running = False
    if pid is not None:
        try:
            os.kill(pid, 0)
            is_running = True
        except OSError:
            is_running = False

    # Attempt OpenCode export
    export_cmd = [OPENCODE_BIN, "export", task_id]
    env = dict(os.environ, PAGER="cat")
    proc_exp = subprocess.run(export_cmd, capture_output=True, text=True, env=env)

    raw_export: dict[str, Any] | None = None
    if proc_exp.returncode == 0:
        # Strip header if present ("Exporting session: ses_...\n{...")
        stdout_txt = proc_exp.stdout
        json_start = stdout_txt.find("{")
        if json_start != -1:
            try:
                raw_export = json.loads(stdout_txt[json_start:])
            except Exception:
                pass

    if is_running:
        status = "running"
    elif raw_export is not None:
        messages = raw_export.get("messages", [])
        assistant_msgs = [m for m in messages if isinstance(m, dict) and m.get("info", {}).get("role") == "assistant"]
        if assistant_msgs and assistant_msgs[-1].get("info", {}).get("finish"):
            finish_reason = assistant_msgs[-1]["info"]["finish"]
            if finish_reason == "stop":
                status = "completed"
            else:
                status = "failed"
        else:
            status = "completed" if raw_export else "running"
    else:
        status = "failed" if pid is not None else "ambiguous"

    created_at = proc_meta.get("created_at") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    agent = proc_meta.get("agent") or "unknown"
    model = proc_meta.get("model")
    workspace = proc_meta.get("workspace") or "."

    return TaskSnapshot(
        task_id=task_id,
        status=status,
        agent=agent,
        model=model,
        workspace=workspace,
        created_at=created_at,
        completed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) if status in {"completed", "failed", "cancelled"} else None,
        raw_export=raw_export,
    )


def wait_task(
    task_id: str,
    proc: subprocess.Popen[str] | None = None,
    timeout_seconds: float = 300.0,
    transport_state_dir: Path | None = None,
) -> TaskSnapshot:
    """Waits for an OpenCode task to reach a terminal status."""
    start = time.time()
    while time.time() - start < timeout_seconds:
        if proc is not None and proc.poll() is not None:
            break
        snap = get_task(task_id, transport_state_dir=transport_state_dir)
        if snap.status in {"completed", "failed", "cancelled"}:
            return snap
        time.sleep(0.5)

    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    return get_task(task_id, transport_state_dir=transport_state_dir)


def cancel_task(task_id: str, transport_state_dir: Path | None = None) -> TaskSnapshot:
    """Cancels a running OpenCode Task by killing its backing process."""
    meta_dir = transport_state_dir or _state_dir()
    meta_path = meta_dir / f"{task_id}.proc.json"
    if meta_path.is_file():
        try:
            proc_meta = json.loads(meta_path.read_text(encoding="utf-8"))
            pid = proc_meta.get("pid")
            if pid:
                os.kill(pid, signal.SIGTERM)
                time.sleep(0.2)
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        except Exception:
            pass

    snap = get_task(task_id, transport_state_dir=transport_state_dir)
    snap.status = "cancelled"
    return snap


def get_result(task_id: str, transport_state_dir: Path | None = None) -> TaskResult:
    """Retrieves and parses the final typed result of an OpenCode task."""
    snap = get_task(task_id, transport_state_dir=transport_state_dir)
    if not snap.raw_export:
        return TaskResult(
            task_id=task_id,
            status=snap.status,
            agent=snap.agent,
            text_content="",
            json_payload=None,
            tokens={},
            cost=0.0,
            model=snap.model,
            finish_reason=None,
            error_type="OPENCODE_TASK_RESULT_INVALID",
        )

    info = snap.raw_export.get("info", {})
    messages = snap.raw_export.get("messages", [])
    tokens = info.get("tokens", {})
    cost = float(info.get("cost", 0.0))
    model = snap.model or (info.get("model", {}).get("id") if isinstance(info.get("model"), dict) else None)

    # Collect assistant text parts
    text_parts: list[str] = []
    finish_reason: str | None = None
    for msg in messages:
        if isinstance(msg, dict) and msg.get("info", {}).get("role") == "assistant":
            finish_reason = msg.get("info", {}).get("finish")
            parts = msg.get("parts", [])
            for p in parts:
                if isinstance(p, dict) and p.get("type") == "text":
                    text_parts.append(p.get("text", ""))

    raw_text = "\n".join(text_parts).strip()
    sanitized = sanitize_text(raw_text)

    # Try extracting JSON object from assistant response text
    json_payload: dict[str, Any] | None = None
    # Look for code block ```json ... ``` or plain {...}
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", sanitized, re.DOTALL)
    if match:
        try:
            json_payload = json.loads(match.group(1))
        except Exception:
            pass
    if json_payload is None:
        match_raw = re.search(r"(\{.*\})", sanitized, re.DOTALL)
        if match_raw:
            try:
                json_payload = json.loads(match_raw.group(1))
            except Exception:
                pass

    return TaskResult(
        task_id=task_id,
        status=snap.status,
        agent=snap.agent,
        text_content=sanitized,
        json_payload=json_payload,
        tokens=tokens,
        cost=cost,
        model=model,
        finish_reason=finish_reason,
    )


def recover_existing_task(task_id: str, transport_state_dir: Path | None = None) -> TaskSnapshot:
    """Reconciles and recovers an existing task identity without re-dispatching."""
    return get_task(task_id, transport_state_dir=transport_state_dir)
