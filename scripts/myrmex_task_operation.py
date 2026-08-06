#!/usr/bin/env python3
"""
Task Operation Ledger for Myrmex Task Contracts (myrmex.task-operation/v1).
Manages durable intent, dispatch, observation, and receipt lifecycle for OpenCode tasks.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from scripts.opencode_transport import sanitize_text


def _task_ops_dir() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    d = Path(xdg) / "myrmex/task-operations"
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class TaskOperationV1:
    operation_id: str
    campaign_id: str
    work_unit_id: str
    run_id: str
    role: str  # writer, verifier, remediator
    agent: str
    provider: str
    model: str
    workspace: str
    base_sha: str
    candidate_sha: str | None
    diff_digest: str | None
    status: str  # intent, dispatching, task-observed, running, completed, failed, cancelled, ambiguous, superseded
    created_at: str
    started_at: str | None
    completed_at: str | None
    task_id: str | None
    request_digest: str
    result_digest: str | None
    error_type: str | None
    receipt: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TaskOperationV1:
        return cls(**d)


def create_task_intent(
    campaign_id: str,
    work_unit_id: str,
    run_id: str,
    role: str,
    agent: str,
    workspace: str,
    base_sha: str,
    prompt: str,
    provider: str = "opencode",
    model: str = "opencode/deepseek-v4-flash-free",
    attempt: int = 1,
) -> TaskOperationV1:
    """
    Creates and persists a TASK_INTENT record BEFORE contacting OpenCode.
    """
    req_bytes = prompt.encode("utf-8")
    req_digest = hashlib.sha256(req_bytes).hexdigest()
    op_id = f"op-{run_id}-{work_unit_id}-{role}-att{attempt}"
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    op = TaskOperationV1(
        operation_id=op_id,
        campaign_id=campaign_id,
        work_unit_id=work_unit_id,
        run_id=run_id,
        role=role,
        agent=agent,
        provider=provider,
        model=model,
        workspace=str(Path(workspace).resolve()),
        base_sha=base_sha,
        candidate_sha=None,
        diff_digest=None,
        status="intent",
        created_at=now,
        started_at=None,
        completed_at=None,
        task_id=None,
        request_digest=req_digest,
        result_digest=None,
        error_type=None,
        receipt={"schema": "myrmex.task-operation/v1", "phase": "TASK_INTENT"},
    )
    save_task_operation(op)
    return op


def save_task_operation(op: TaskOperationV1) -> Path:
    d = _task_ops_dir()
    path = d / f"{op.operation_id}.json"
    path.write_text(json.dumps(op.to_dict(), indent=2), encoding="utf-8")
    return path


def find_task_operation(operation_id: str) -> TaskOperationV1 | None:
    d = _task_ops_dir()
    path = d / f"{operation_id}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return TaskOperationV1(**data)
    except Exception:
        return None


def find_existing_op_for_phase(
    run_id: str,
    work_unit_id: str,
    role: str,
    attempt: int = 1,
) -> TaskOperationV1 | None:
    op_id = f"op-{run_id}-{work_unit_id}-{role}-att{attempt}"
    return find_task_operation(op_id)


def record_task_observed(op: TaskOperationV1, task_id: str) -> TaskOperationV1:
    op.task_id = task_id
    op.status = "task-observed"
    op.started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    op.receipt["phase"] = "TASK_ID_OBSERVED"
    op.receipt["observed_task_id"] = task_id
    save_task_operation(op)
    return op


def record_task_terminal(
    op: TaskOperationV1,
    status: str,
    result_text: str,
    candidate_sha: str | None = None,
    diff_digest: str | None = None,
    error_type: str | None = None,
    result_payload: dict[str, Any] | None = None,
) -> TaskOperationV1:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    op.status = status
    op.completed_at = now
    op.candidate_sha = candidate_sha
    op.diff_digest = diff_digest
    op.error_type = error_type

    res_bytes = result_text.encode("utf-8")
    op.result_digest = hashlib.sha256(res_bytes).hexdigest()
    op.receipt["phase"] = "TASK_TERMINAL"
    op.receipt["result_digest"] = op.result_digest
    op.receipt["result_payload"] = result_payload
    op.receipt["sanitized_summary"] = sanitize_text(result_text[:500])
    save_task_operation(op)
    return op


def record_receipt_confirmed(op: TaskOperationV1) -> TaskOperationV1:
    op.receipt["phase"] = "RESULT_RECEIPT_CONFIRMED"
    op.receipt["confirmed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    save_task_operation(op)
    return op
