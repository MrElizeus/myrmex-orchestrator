#!/usr/bin/env python3
"""State-first task gateway for the bounded P1-008 planner agent.

The gateway persists an exact repository-context snapshot, planning request,
and task intent before invoking a caller-supplied task transport. Replays reuse
the same task identity and immutable artifacts. It has no repository-write,
activation, commit, push, memory, merge, release, or deployment authority.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import sys
from typing import Any, Callable

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import myrmex_campaign_intelligence as intel  # noqa: E402
import myrmex_planner as planner  # noqa: E402

AGENT_NAME = "myrmex-planner"
TASK_INTENT_SCHEMA = "myrmex.planner-task-intent/v1"
TASK_RECEIPT_SCHEMA = "myrmex.planner-task-receipt/v1"
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,255}$")
AUTHORITY = {
    "scope": "planning_only", "repository_write": False, "activate_plan": False,
    "create_work_units": False, "memory_write": False, "commit": False,
    "push": False, "merge": False, "release": False, "deploy": False,
}


class PlannerGatewayError(Exception): pass
class PlannerTaskConflict(PlannerGatewayError): pass
class PlannerTaskInvalid(PlannerGatewayError): pass


def _canon(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canon(value)).hexdigest()


def _task_artifact_id(request_id: str, suffix: str) -> str:
    return f"planner-task/{suffix}/{hashlib.sha256(request_id.encode('utf-8')).hexdigest()}"


def _planning_response_artifact_id(request_id: str) -> str:
    return f"planning-result/response/{hashlib.sha256(request_id.encode('utf-8')).hexdigest()}"


def _load_payload(campaign_dir, campaign_id, artifact_id, kind="decision") -> dict[str, Any] | None:
    try:
        envelope = intel.get_artifact(pathlib.Path(campaign_dir), campaign_id, artifact_id)["artifact"]
    except intel.IntelligenceArtifactInvalid as error:
        if str(error).startswith("artifact not found"):
            return None
        raise PlannerTaskInvalid("planner gateway artifact is invalid") from error
    if envelope.get("kind") != kind or not isinstance(envelope.get("payload"), dict):
        raise PlannerTaskInvalid("planner gateway artifact kind/payload invalid")
    return envelope["payload"]


def persist_repository_context(campaign_dir, campaign_id, observed_campaign_revision, run_id, objective_id, base_sha, repository_context):
    planner._validate_repository_context(repository_context, run_id=run_id, objective_id=objective_id, base_sha=base_sha)
    digest = intel.compute_payload_digest(repository_context)
    artifact_id = f"repository-context/snapshot/{digest}"
    result = intel.put_artifact(pathlib.Path(campaign_dir), campaign_id, observed_campaign_revision, "decision", artifact_id, repository_context)
    return {"artifact_id": artifact_id, "payload_digest": digest, "status": result["status"]}


def prepare_planner_task(campaign_dir, campaign_id, observed_campaign_revision, request_id, task_id, run_id, objective_id, base_sha, normalized_snapshot_record_id, repository_context, constraints, created_at=None):
    if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id) or task_id == request_id:
        raise PlannerTaskInvalid("task_id must be a distinct bounded task identity")
    repo = persist_repository_context(campaign_dir, campaign_id, observed_campaign_revision, run_id, objective_id, base_sha, repository_context)
    request = planner.create_planning_request(
        campaign_dir, campaign_id, observed_campaign_revision, request_id, run_id,
        objective_id, base_sha, normalized_snapshot_record_id, repo["artifact_id"],
        constraints, created_at,
    )
    context = planner.build_planning_context(campaign_dir, campaign_id, request_id)
    prompt = planner.render_planning_prompt(context)
    intent = {
        "schema": TASK_INTENT_SCHEMA, "request_id": request_id, "task_id": task_id,
        "agent": AGENT_NAME, "run_id": run_id, "campaign_id": campaign_id,
        "objective_id": objective_id, "base_sha": base_sha,
        "input_digests": request["input_digests"], "prompt_digest": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "authority": dict(AUTHORITY), "created_at": request["created_at"],
    }
    artifact_id = _task_artifact_id(request_id, "intent")
    existing = _load_payload(campaign_dir, campaign_id, artifact_id)
    if existing is not None and _canon(existing) != _canon(intent):
        raise PlannerTaskConflict("planner task identity conflicts with durable intent")
    if existing is None:
        try:
            intel.put_artifact(pathlib.Path(campaign_dir), campaign_id, observed_campaign_revision, "decision", artifact_id, intent)
        except intel.IntelligenceArtifactConflict as error:
            existing = _load_payload(campaign_dir, campaign_id, artifact_id)
            if existing is None or _canon(existing) != _canon(intent):
                raise PlannerTaskConflict("planner task intent conflict") from error
    return {"request": request, "context": context, "prompt": prompt, "task_intent": intent, "task_intent_artifact_id": artifact_id}


def record_planner_task_result(campaign_dir, campaign_id, observed_campaign_revision, request_id, task_id, result):
    intent_id = _task_artifact_id(request_id, "intent")
    intent = _load_payload(campaign_dir, campaign_id, intent_id)
    if intent is None or intent.get("task_id") != task_id or intent.get("agent") != AGENT_NAME:
        raise PlannerTaskInvalid("planner task result does not match durable task intent")
    planner_receipt = planner.record_planning_result(campaign_dir, campaign_id, observed_campaign_revision, request_id, result)
    stable_planner_binding = {
        key: planner_receipt[key]
        for key in (
            "artifact_id", "response_type", "plan_revision_id", "record_id",
            "plan_digest", "record_digest", "lifecycle_status", "head_record_id",
        )
        if key in planner_receipt
    }
    receipt = {
        "schema": TASK_RECEIPT_SCHEMA, "request_id": request_id, "task_id": task_id,
        "agent": AGENT_NAME, "result_digest": result["result_digest"],
        "response_type": result["response_type"], "planner_receipt": stable_planner_binding,
        "authority": dict(AUTHORITY), "created_at": result["created_at"],
    }
    receipt_id = _task_artifact_id(request_id, "receipt")
    existing = _load_payload(campaign_dir, campaign_id, receipt_id)
    if existing is not None and _canon(existing) != _canon(receipt):
        raise PlannerTaskConflict("planner task receipt conflicts")
    if existing is None:
        try:
            persisted = intel.put_artifact(pathlib.Path(campaign_dir), campaign_id, observed_campaign_revision, "decision", receipt_id, receipt)
            status = persisted["status"]
        except intel.IntelligenceArtifactConflict as error:
            existing = _load_payload(campaign_dir, campaign_id, receipt_id)
            if existing is None or _canon(existing) != _canon(receipt):
                raise PlannerTaskConflict("planner task receipt conflict") from error
            status = "reused"
    else:
        status = "reused"
    return {"ok": True, "status": status, "task_id": task_id, "task_intent_artifact_id": intent_id, "task_receipt_artifact_id": receipt_id, "result_digest": result["result_digest"], "planner_receipt": planner_receipt}


def _reused_task_receipt(campaign_dir, campaign_id, request_id, task_id):
    receipt_id = _task_artifact_id(request_id, "receipt")
    receipt = _load_payload(campaign_dir, campaign_id, receipt_id)
    if receipt is None:
        return None
    if (
        receipt.get("schema") != TASK_RECEIPT_SCHEMA
        or receipt.get("request_id") != request_id
        or receipt.get("task_id") != task_id
        or receipt.get("agent") != AGENT_NAME
        or receipt.get("authority") != AUTHORITY
        or not isinstance(receipt.get("result_digest"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", receipt["result_digest"])
        or not isinstance(receipt.get("planner_receipt"), dict)
    ):
        raise PlannerTaskInvalid("durable planner task receipt is invalid")
    return {
        "ok": True, "status": "reused", "task_id": task_id,
        "task_intent_artifact_id": _task_artifact_id(request_id, "intent"),
        "task_receipt_artifact_id": receipt_id,
        "result_digest": receipt["result_digest"],
        "planner_receipt": receipt["planner_receipt"],
    }


def execute_planner_task(campaign_dir, campaign_id, observed_campaign_revision, request_id, task_id, run_id, objective_id, base_sha, normalized_snapshot_record_id, repository_context, constraints, executor: Callable[..., Any], created_at=None):
    prepared = prepare_planner_task(campaign_dir, campaign_id, observed_campaign_revision, request_id, task_id, run_id, objective_id, base_sha, normalized_snapshot_record_id, repository_context, constraints, created_at)
    existing_receipt = _reused_task_receipt(campaign_dir, campaign_id, request_id, task_id)
    if existing_receipt is not None:
        return {**prepared, "receipt": existing_receipt}
    # The planning response is persisted before the task receipt.  If a crash
    # lands in that window, finish the receipt from the accepted immutable
    # response rather than invoking the task transport a second time.
    existing_result = _load_payload(
        campaign_dir, campaign_id, _planning_response_artifact_id(request_id), kind="plan",
    )
    if existing_result is not None:
        receipt = record_planner_task_result(
            campaign_dir, campaign_id, observed_campaign_revision,
            request_id, task_id, existing_result,
        )
        return {**prepared, "receipt": receipt}
    response = executor(agent=AGENT_NAME, task_id=task_id, prompt=prepared["prompt"])
    if not isinstance(response, dict) or set(response) != {"task_id", "result"} or response["task_id"] != task_id:
        raise PlannerTaskInvalid("planner executor returned a mismatched task envelope")
    receipt = record_planner_task_result(campaign_dir, campaign_id, observed_campaign_revision, request_id, task_id, response["result"])
    return {**prepared, "receipt": receipt}
