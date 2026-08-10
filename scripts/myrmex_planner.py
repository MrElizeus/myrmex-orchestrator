#!/usr/bin/env python3
"""The planning-only boundary between normalized backlog and plan storage.

This module deliberately has no provider, repository, network, or execution
authority.  Its only durable effects are immutable planning artifacts and a
proposed plan revision.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import pathlib
import re
from typing import Any

import myrmex_backlog_normalizer as backlog
import myrmex_campaign_intelligence as intel
import myrmex_plan_store as plan_store

REQUEST_SCHEMA = "myrmex.planning-request/v1"
RESULT_SCHEMA = "myrmex.planning-result/v1"
CONTEXT_SCHEMA = "myrmex.planning-context/v1"
REQUEST_FIELDS = ("schema", "request_id", "run_id", "campaign_id", "objective_id", "base_sha", "parent_revision", "input_digests", "constraints", "required_output_schema", "effect_policy", "created_at")
CONSTRAINT_FIELDS = ("allowed_paths", "forbidden_paths", "required_invariants", "required_sections")
EFFECT_POLICY = {"mode": "planning_only", "repository_write": False, "activate_plan": False, "commit": False, "push": False, "merge": False, "release": False, "deploy": False}
RESULT_FIELDS = ("schema", "request_id", "run_id", "campaign_id", "objective_id", "base_sha", "response_type", "plan_revision", "analysis", "coverage_matrix", "clarification", "completion_evidence", "authority", "result_digest", "created_at")
ANALYSIS_FIELDS = ("facts", "assumptions", "uncertainties")
REPOSITORY_CONTEXT_REQUIRED_FIELDS = (
    "schema", "repository_root", "branch", "base_sha", "git_status", "objective",
    "relevant_files", "relevant_symbols", "architecture", "current_behavior", "tests",
    "data_contracts", "observed_conventions", "implementation_constraints",
    "unresolved_decisions", "protected_dirty_paths", "excluded_sensitive_paths", "evidence",
)
REPOSITORY_CONTEXT_OPTIONAL_FIELDS = ("run_id", "objective_id")
AUTHORITY = {"scope": "planning_only", "plan_activation_authorized": False, "repository_write_authorized": False, "commit_authorized": False, "push_authorized": False, "merge_authorized": False, "release_authorized": False, "deployment_authorized": False}
DEFAULT_CREATED_AT = "1970-01-01T00:00:00+00:00"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SHA1 = re.compile(r"^[0-9a-f]{40}$")
CAMPAIGN_ID = re.compile(r"^camp-[a-z0-9][a-z0-9-]{4,60}$")
RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")


class PlannerOrchestrationError(Exception):
    """Base class for closed planner-boundary failures."""
class PlanningRequestInvalid(PlannerOrchestrationError): pass
class PlanningRequestConflict(PlannerOrchestrationError): pass
class PlanningInputInvalid(PlannerOrchestrationError): pass
class PlanningResultInvalid(PlannerOrchestrationError): pass
class PlanningResultMismatch(PlannerOrchestrationError): pass
class PlanningResultConflict(PlannerOrchestrationError): pass


def _canon(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")

def _sha(value: Any) -> str:
    return hashlib.sha256(_canon(value) if not isinstance(value, bytes) else value).hexdigest()

def _text_sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

def _fields(value: Any, allowed: tuple[str, ...], label: str, exc: type[Exception]) -> None:
    if not isinstance(value, dict):
        raise exc(f"{label} must be an object")
    unknown = sorted(set(value) - set(allowed))
    missing = [x for x in allowed if x not in value]
    if unknown or missing:
        raise exc(f"{label} fields invalid")

def _nonempty(value: Any, label: str, exc: type[Exception]) -> None:
    if not isinstance(value, str) or not value:
        raise exc(f"{label} must be non-empty")

def _bounded_nonempty(value: Any, label: str, exc: type[Exception], maximum: int = 256) -> None:
    _nonempty(value, label, exc)
    if len(value) > maximum:
        raise exc(f"{label} exceeds {maximum} characters")

def _digest(value: Any, label: str, exc: type[Exception]) -> None:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise exc(f"{label} must be a SHA-256 digest")

def _load(campaign_dir: pathlib.Path, campaign_id: str, artifact_id: str, kind: str, exc: type[Exception]) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        envelope = intel.get_artifact(pathlib.Path(campaign_dir), campaign_id, artifact_id)["artifact"]
    except Exception as error:
        raise exc(f"authoritative artifact unavailable: {artifact_id}") from error
    if not isinstance(envelope, dict) or envelope.get("kind") != kind or envelope.get("artifact_id") != artifact_id or not isinstance(envelope.get("payload"), dict):
        raise exc(f"authoritative artifact invalid: {artifact_id}")
    return envelope, envelope["payload"]

def _load_optional(campaign_dir: pathlib.Path, campaign_id: str, artifact_id: str, kind: str, exc: type[Exception]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    try:
        return _load(campaign_dir, campaign_id, artifact_id, kind, exc)
    except exc as error:
        cause = error.__cause__
        if isinstance(cause, intel.IntelligenceArtifactInvalid) and str(cause).startswith("artifact not found for campaign"):
            return None
        raise

def _validate_timestamp(value: Any, label: str, exc: type[Exception]) -> None:
    if not isinstance(value, str) or not RFC3339.fullmatch(value):
        raise exc(f"{label} must be RFC3339 with an explicit timezone")
    try:
        parsed = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise exc(f"{label} must be RFC3339") from error
    if parsed.tzinfo is None:
        raise exc(f"{label} must include a timezone")

def _validate_request(request: Any) -> None:
    _fields(request, REQUEST_FIELDS, "request", PlanningRequestInvalid)
    if request["schema"] != REQUEST_SCHEMA:
        raise PlanningRequestInvalid("request schema mismatch")
    for key in ("request_id", "run_id", "objective_id"):
        _bounded_nonempty(request[key], key, PlanningRequestInvalid)
    if not isinstance(request["campaign_id"], str) or not CAMPAIGN_ID.fullmatch(request["campaign_id"]):
        raise PlanningRequestInvalid("campaign_id invalid")
    if not isinstance(request["base_sha"], str) or not SHA1.fullmatch(request["base_sha"]):
        raise PlanningRequestInvalid("base_sha invalid")
    if request["parent_revision"] is not None:
        raise PlanningRequestInvalid("parent_revision must be null")
    digs = request["input_digests"]
    if not isinstance(digs, list) or len(digs) != 2 or any(not isinstance(d, dict) or set(d) != {"kind", "identity", "sha256"} for d in digs):
        raise PlanningRequestInvalid("input_digests invalid")
    if [d["kind"] for d in digs] != ["normalized-backlog", "repository-context"]:
        raise PlanningRequestInvalid("input_digests must bind normalized-backlog then repository-context")
    if not isinstance(digs[0]["identity"], str) or not digs[0]["identity"].startswith("normalized-backlog/snapshot/blsnaprec_"):
        raise PlanningRequestInvalid("normalized backlog input invalid")
    if not isinstance(digs[1]["identity"], str) or not digs[1]["identity"].startswith("repository-context/snapshot/"):
        raise PlanningRequestInvalid("repository context input invalid")
    for index, digest in enumerate(digs):
        _digest(digest["sha256"], f"input digest {index}", PlanningRequestInvalid)
    _fields(request["constraints"], CONSTRAINT_FIELDS, "constraints", PlanningRequestInvalid)
    if any(not isinstance(x, list) or any(not isinstance(v, str) for v in x) for x in request["constraints"].values()):
        raise PlanningRequestInvalid("constraints arrays invalid")
    if request["required_output_schema"] != RESULT_SCHEMA or request["effect_policy"] != EFFECT_POLICY:
        raise PlanningRequestInvalid("planning authority policy invalid")
    _validate_timestamp(request["created_at"], "created_at", PlanningRequestInvalid)

def _request_id(campaign_id: str, request_id: str) -> str:
    return f"planning-request/request/{_text_sha(request_id)}"
def _response_id(request_id: str) -> str:
    return f"planning-result/response/{_text_sha(request_id)}"

def _input_digest(request: dict[str, Any], kind: str) -> dict[str, Any]:
    return next(entry for entry in request["input_digests"] if entry["kind"] == kind)


def _validate_repository_context(context: Any, *, run_id: str, objective_id: str, base_sha: str) -> None:
    if not isinstance(context, dict):
        raise PlanningInputInvalid("repository context must be an object")
    allowed = set(REPOSITORY_CONTEXT_REQUIRED_FIELDS) | set(REPOSITORY_CONTEXT_OPTIONAL_FIELDS)
    if set(context) - allowed or any(field not in context for field in REPOSITORY_CONTEXT_REQUIRED_FIELDS):
        raise PlanningInputInvalid("repository context fields invalid")
    if context["schema"] != "myrmex.repository-context/v1" or context["base_sha"] != base_sha:
        raise PlanningInputInvalid("repository context identity mismatch")
    if context.get("run_id") not in (None, run_id) or context.get("objective_id") not in (None, objective_id):
        raise PlanningInputInvalid("repository context run/objective mismatch")
    if not isinstance(context["repository_root"], str) or not context["repository_root"]:
        raise PlanningInputInvalid("repository context root invalid")
    for field in (
        "git_status", "relevant_files", "relevant_symbols", "architecture", "current_behavior",
        "tests", "data_contracts", "observed_conventions", "implementation_constraints",
        "unresolved_decisions", "protected_dirty_paths", "excluded_sensitive_paths", "evidence",
    ):
        if not isinstance(context[field], list) or any(not isinstance(item, str) for item in context[field]):
            raise PlanningInputInvalid(f"repository context {field} invalid")
    if not isinstance(context["objective"], str) or not context["objective"]:
        raise PlanningInputInvalid("repository context objective invalid")
    try:
        intel.reject_secret_or_raw(context)
    except intel.IntelligencePayloadRejected as error:
        raise PlanningInputInvalid("repository context rejected by content policy") from error


def create_planning_request(campaign_dir, campaign_id, observed_campaign_revision, request_id, run_id, objective_id, base_sha, normalized_snapshot_record_id, repository_context_artifact_id, constraints, created_at=None):
    if not isinstance(observed_campaign_revision, int) or isinstance(observed_campaign_revision, bool) or observed_campaign_revision < 0:
        raise PlanningRequestInvalid("observed_campaign_revision invalid")
    snapshot_id = f"normalized-backlog/snapshot/{normalized_snapshot_record_id}"
    envelope, snapshot = _load(pathlib.Path(campaign_dir), campaign_id, snapshot_id, "backlog", PlanningInputInvalid)
    try:
        backlog.validate_normalized_snapshot(snapshot)
    except Exception as error:
        raise PlanningInputInvalid("normalized snapshot invalid") from error
    repo_envelope, repository_context = _load(pathlib.Path(campaign_dir), campaign_id, repository_context_artifact_id, "decision", PlanningInputInvalid)
    _validate_repository_context(repository_context, run_id=run_id, objective_id=objective_id, base_sha=base_sha)
    request = {"schema": REQUEST_SCHEMA, "request_id": request_id, "run_id": run_id, "campaign_id": campaign_id, "objective_id": objective_id, "base_sha": base_sha, "parent_revision": None,
               "input_digests": [{"kind": "normalized-backlog", "identity": snapshot_id, "sha256": envelope["payload_digest"]}, {"kind": "repository-context", "identity": repository_context_artifact_id, "sha256": repo_envelope["payload_digest"]}], "constraints": constraints,
               "required_output_schema": RESULT_SCHEMA, "effect_policy": dict(EFFECT_POLICY), "created_at": DEFAULT_CREATED_AT if created_at is None else created_at}
    try:
        _validate_request(request)
    except PlanningRequestInvalid:
        raise
    except Exception as error:
        raise PlanningRequestInvalid("request invalid") from error
    artifact_id = _request_id(campaign_id, request_id)

    def existing_request() -> dict[str, Any] | None:
        loaded = _load_optional(pathlib.Path(campaign_dir), campaign_id, artifact_id, "plan", PlanningRequestConflict)
        if loaded is None:
            return None
        _, stored = loaded
        try:
            _validate_request(stored)
        except Exception as error:
            raise PlanningRequestConflict("stored planning request is invalid") from error
        if _canon(stored) != _canon(request):
            raise PlanningRequestConflict("planning request identity conflicts")
        return stored

    stored = existing_request()
    if stored is not None:
        return stored
    try:
        intel.put_artifact(pathlib.Path(campaign_dir), campaign_id, observed_campaign_revision, "plan", artifact_id, request)
    except intel.IntelligenceArtifactConflict:
        # Another process may have won the immutable first-write race.
        stored = existing_request()
        if stored is None:
            raise PlanningRequestConflict("planning request identity conflicts")
        return stored
    except Exception as error:
        raise PlanningRequestInvalid("planning request persistence failed") from error
    return request

def build_planning_context(campaign_dir, campaign_id, request_id):
    _, request = _load(pathlib.Path(campaign_dir), campaign_id, _request_id(campaign_id, request_id), "plan", PlanningInputInvalid)
    try:
        _validate_request(request)
    except Exception as error:
        raise PlanningInputInvalid("planning request invalid") from error
    snapshot_input = _input_digest(request, "normalized-backlog")
    snapshot_id = snapshot_input["identity"]
    snapshot_env, snapshot = _load(pathlib.Path(campaign_dir), campaign_id, snapshot_id, "backlog", PlanningInputInvalid)
    try:
        backlog.validate_normalized_snapshot(snapshot)
    except Exception as error:
        raise PlanningInputInvalid("normalized snapshot invalid") from error
    if snapshot_env["payload_digest"] != snapshot_input["sha256"]:
        raise PlanningInputInvalid("normalized snapshot digest mismatch")
    items = []
    for desc in snapshot["items"]:
        env, item = _load(pathlib.Path(campaign_dir), campaign_id, desc["artifact_id"], "backlog", PlanningInputInvalid)
        if item.get("backlog_item_id") != desc["backlog_item_id"] or backlog.compute_item_digest(item) != desc["item_digest"]:
            raise PlanningInputInvalid("normalized item descriptor mismatch")
        try:
            backlog.validate_normalized_item(item)
        except Exception as error:
            raise PlanningInputInvalid("normalized item invalid") from error
        items.append(item)
    items.sort(key=lambda x: x["backlog_item_id"])
    repository_input = _input_digest(request, "repository-context")
    repo_env, repository_context = _load(pathlib.Path(campaign_dir), campaign_id, repository_input["identity"], "decision", PlanningInputInvalid)
    if repo_env["payload_digest"] != repository_input["sha256"]:
        raise PlanningInputInvalid("repository context digest mismatch")
    _validate_repository_context(repository_context, run_id=request["run_id"], objective_id=request["objective_id"], base_sha=request["base_sha"])
    return {"schema": CONTEXT_SCHEMA, "request": request, "repository_context": repository_context, "normalized_backlog": {"snapshot": snapshot, "items": items}}

def render_planning_prompt(context):
    _validate_context(context)
    try:
        intel.reject_secret_or_raw(context)
    except intel.IntelligencePayloadRejected as error:
        raise PlanningInputInvalid("context rejected by secret/raw-content policy") from error
    return "You are an external planner. Produce exactly one myrmex.planning-result/v1 object. Planning only: authorize no effects.\n\n" + _canon(context).decode("utf-8")

def _validate_context(context: Any) -> None:
    if not isinstance(context, dict) or set(context) != {"schema", "request", "repository_context", "normalized_backlog"} or context["schema"] != CONTEXT_SCHEMA:
        raise PlanningInputInvalid("context fields invalid")
    try:
        _validate_request(context["request"])
    except Exception as error:
        raise PlanningInputInvalid("context request invalid") from error
    data = context["normalized_backlog"]
    if not isinstance(data, dict) or set(data) != {"snapshot", "items"} or not isinstance(data["items"], list):
        raise PlanningInputInvalid("context backlog fields invalid")
    snapshot = data["snapshot"]
    try:
        backlog.validate_normalized_snapshot(snapshot)
    except Exception as error:
        raise PlanningInputInvalid("context snapshot invalid") from error
    # The request binds the context to the authoritative snapshot payload.  A
    # structurally valid request digest is not sufficient: prompt rendering
    # must reject a request/context pair whose binding was tampered with.
    if _input_digest(context["request"], "normalized-backlog")["sha256"] != intel.compute_payload_digest(snapshot):
        raise PlanningInputInvalid("context snapshot digest mismatch")
    if len(data["items"]) != snapshot["item_count"]:
        raise PlanningInputInvalid("context item count mismatch")
    ids = []
    for item in data["items"]:
        try:
            backlog.validate_normalized_item(item)
        except Exception as error:
            raise PlanningInputInvalid("context item invalid") from error
        ids.append(item["backlog_item_id"])
    if ids != sorted(ids) or ids != [d["backlog_item_id"] for d in snapshot["items"]]:
        raise PlanningInputInvalid("context item ordering mismatch")
    for desc, item in zip(snapshot["items"], data["items"]):
        if desc["item_digest"] != backlog.compute_item_digest(item) or desc["artifact_id"] != f"normalized-backlog/item/{item['backlog_item_id']}/{item['item_digest']}":
            raise PlanningInputInvalid("context item descriptor mismatch")
    if _input_digest(context["request"], "normalized-backlog")["identity"] != f"normalized-backlog/snapshot/{snapshot['snapshot_record_id']}":
        raise PlanningInputInvalid("context snapshot identity mismatch")
    repository_context = context["repository_context"]
    _validate_repository_context(repository_context, run_id=context["request"]["run_id"], objective_id=context["request"]["objective_id"], base_sha=context["request"]["base_sha"])
    if _input_digest(context["request"], "repository-context")["sha256"] != intel.compute_payload_digest(repository_context):
        raise PlanningInputInvalid("context repository digest mismatch")

def validate_planning_result(request, result):
    try:
        _validate_request(request)
    except Exception as error:
        raise PlanningResultMismatch("request invalid") from error
    _fields(result, RESULT_FIELDS, "result", PlanningResultInvalid)
    if result["schema"] != RESULT_SCHEMA or result["request_id"] != request["request_id"] or result["run_id"] != request["run_id"] or result["campaign_id"] != request["campaign_id"] or result["objective_id"] != request["objective_id"] or result["base_sha"] != request["base_sha"]:
        raise PlanningResultMismatch("result identity mismatch")
    if not isinstance(result["result_digest"], str) or result["result_digest"] != _sha({k: v for k, v in result.items() if k != "result_digest"}):
        raise PlanningResultInvalid("result digest mismatch")
    _validate_timestamp(result["created_at"], "created_at", PlanningResultInvalid)
    if result["authority"] != AUTHORITY:
        raise PlanningResultInvalid("result authority is not planning-only")
    if result["response_type"] == "plan":
        analysis = result["analysis"]
        coverage = result["coverage_matrix"]
        if result["clarification"] is not None or result["completion_evidence"] != [] or not isinstance(result["plan_revision"], dict) or not isinstance(analysis, dict) or set(analysis) != set(ANALYSIS_FIELDS) or not isinstance(coverage, list) or not coverage:
            raise PlanningResultInvalid("plan response shape invalid")
        for field in ANALYSIS_FIELDS:
            if not isinstance(analysis[field], list) or any(not isinstance(item, str) or not item for item in analysis[field]):
                raise PlanningResultInvalid(f"analysis.{field} must be an array of non-empty strings")
        revision = result["plan_revision"]
        try:
            plan_store.validate_plan_revision_record(revision)
        except Exception as error:
            raise PlanningResultInvalid("embedded plan is invalid") from error
        if revision["lifecycle_status"] != "proposed" or revision["previous_record_id"] is not None or revision["parent_revision"] is not None:
            raise PlanningResultInvalid("plan lifecycle is not a proposed root")
        if revision["planning_request_id"] != request["request_id"] or revision["campaign_id"] != request["campaign_id"] or revision["objective_id"] != request["objective_id"] or revision["base_sha"] != request["base_sha"] or revision["input_digests"] != request["input_digests"]:
            raise PlanningResultMismatch("embedded plan identity mismatch")
        work_unit_ids = {work_unit["id"] for work_unit in revision["work_units"]}
        covered_backlog_ids = []
        for entry in coverage:
            if not isinstance(entry, dict) or set(entry) != {"backlog_item_id", "work_unit_ids"}:
                raise PlanningResultInvalid("coverage matrix entry invalid")
            backlog_item_id = entry["backlog_item_id"]
            mapped_work_units = entry["work_unit_ids"]
            if (
                not isinstance(backlog_item_id, str) or not re.fullmatch(r"backlog_[0-9a-f]{64}", backlog_item_id)
                or not isinstance(mapped_work_units, list) or not mapped_work_units
                or len(set(mapped_work_units)) != len(mapped_work_units)
                or any(work_unit_id not in work_unit_ids for work_unit_id in mapped_work_units)
            ):
                raise PlanningResultInvalid("coverage matrix identity invalid")
            covered_backlog_ids.append(backlog_item_id)
        if len(set(covered_backlog_ids)) != len(covered_backlog_ids):
            raise PlanningResultInvalid("coverage matrix contains duplicate backlog items")
    elif result["response_type"] == "blocking_clarification":
        c = result["clarification"]
        if result["plan_revision"] is not None or result["analysis"] is not None or result["coverage_matrix"] is not None or result["completion_evidence"] != [] or not isinstance(c, dict) or set(c) != {"question", "options", "recommended_default"} or not isinstance(c["question"], str) or not c["question"] or not isinstance(c["options"], list) or not c["options"] or any(not isinstance(x, str) for x in c["options"]) or (c["recommended_default"] is not None and not isinstance(c["recommended_default"], str)):
            raise PlanningResultInvalid("clarification response shape invalid")
    elif result["response_type"] == "already_complete":
        if result["plan_revision"] is not None or result["analysis"] is not None or result["coverage_matrix"] is not None or result["clarification"] is not None or not isinstance(result["completion_evidence"], list) or not result["completion_evidence"] or any(not isinstance(x, str) or not x for x in result["completion_evidence"]):
            raise PlanningResultInvalid("completion response shape invalid")
    else:
        raise PlanningResultInvalid("unknown response type")
    return result

def record_planning_result(campaign_dir, campaign_id, observed_campaign_revision, request_id, result):
    _, request = _load(pathlib.Path(campaign_dir), campaign_id, _request_id(campaign_id, request_id), "plan", PlanningResultMismatch)
    validate_planning_result(request, result)
    if result["response_type"] == "plan":
        context = build_planning_context(campaign_dir, campaign_id, request_id)
        expected_backlog_ids = {item["backlog_item_id"] for item in context["normalized_backlog"]["items"]}
        covered_backlog_ids = {entry["backlog_item_id"] for entry in result["coverage_matrix"]}
        if covered_backlog_ids != expected_backlog_ids:
            raise PlanningResultInvalid("coverage matrix does not exactly cover the normalized backlog")
    response_id = _response_id(request_id)
    response_path = pathlib.Path(campaign_dir)
    response_reused = False

    def existing_response() -> dict[str, Any] | None:
        loaded = _load_optional(response_path, campaign_id, response_id, "plan", PlanningResultConflict)
        if loaded is None:
            return None
        _, stored = loaded
        try:
            validate_planning_result(request, stored)
        except Exception as error:
            raise PlanningResultConflict("stored planning response is invalid") from error
        if _canon(stored) != _canon(result):
            raise PlanningResultConflict("planning response identity conflicts")
        return stored

    try:
        stored_result = existing_response()
        if stored_result is None:
            try:
                persistence = intel.put_artifact(response_path, campaign_id, observed_campaign_revision, "plan", response_id, result)
            except intel.IntelligenceArtifactConflict:
                # Another process may have won the immutable first-write race;
                # re-read the canonical artifact before declaring a conflict.
                stored_result = existing_response()
                if stored_result is None:
                    raise PlanningResultConflict("planning response identity conflicts")
                response_reused = True
            else:
                stored_result = result
                response_reused = persistence["status"] == "reused"
        else:
            response_reused = True
    except intel.IntelligenceArtifactConflict as error:
        raise PlanningResultConflict("planning response identity conflicts") from error
    except PlanningResultConflict:
        raise
    except Exception as error:
        raise PlanningResultConflict("planning response persistence failed") from error
    if result["response_type"] == "plan":
        try:
            return plan_store.store_plan_record(response_path, campaign_id, observed_campaign_revision, result["plan_revision"])
        except Exception as error:
            raise PlanningResultConflict("proposed plan persistence failed") from error
    return {"ok": True, "status": "reused" if response_reused else "recorded", "artifact_id": response_id, "response_type": result["response_type"]}
