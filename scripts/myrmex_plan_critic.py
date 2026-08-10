#!/usr/bin/env python3
"""Independent, state-first plan critic orchestration for P1-009.

The critic agent receives one immutable proposed plan and planner result.  This
module persists critic task intent before transport, validates exact identities
and deterministic minimum checks, records one immutable review, and advances a
PASS plan only to ``reviewed``.  It has no plan-activation or repository-effect
authority.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import re
import sys
from typing import Any, Callable

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import myrmex_campaign_intelligence as intel  # noqa: E402
import myrmex_plan_store as plan_store  # noqa: E402
import myrmex_planner as planner  # noqa: E402

AGENT_NAME = "myrmex-plan-critic"
REVIEW_SCHEMA = "myrmex.plan-review/v1"
CONTEXT_SCHEMA = "myrmex.plan-review-context/v1"
TASK_INTENT_SCHEMA = "myrmex.plan-critic-task-intent/v1"
TASK_RECEIPT_SCHEMA = "myrmex.plan-critic-task-receipt/v1"
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,255}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DEFECT_ID_RE = re.compile(r"^PRD-[0-9]{3,6}$")
CHECK_NAMES = (
    "coverage", "scope", "verification", "dependencies", "risk",
    "unsupported_assumptions", "human_gates",
)
CHECK_VALUES = {"PASS", "FAIL", "BLOCKED"}
VERDICTS = {"PASS", "REVISE", "BLOCKED", "INVALID"}
DEFECT_CATEGORIES = set(CHECK_NAMES) | {"identity", "structure"}
AUTHORITY = {
    "scope": "review_only", "repository_write": False, "activate_plan": False,
    "create_work_units": False, "memory_write": False, "commit": False,
    "push": False, "merge": False, "release": False, "deploy": False,
}
DEFAULT_CREATED_AT = "1970-01-01T00:00:00+00:00"
REVIEW_FIELDS = (
    "schema", "review_request_id", "run_id", "campaign_id", "objective_id",
    "critic_task_id", "planner_task_id", "plan_revision_id", "plan_record_id",
    "plan_digest", "plan_record_digest", "verdict", "checks", "defects",
    "summary", "authority", "review_digest", "created_at",
)


class PlanCriticError(Exception): pass
class PlanCriticInputInvalid(PlanCriticError): pass
class PlanCriticTaskInvalid(PlanCriticError): pass
class PlanCriticTaskConflict(PlanCriticError): pass
class PlanReviewInvalid(PlanCriticError): pass
class PlanReviewConflict(PlanCriticError): pass


def _canon(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canon(value)).hexdigest()


def _artifact_id(review_request_id: str, suffix: str) -> str:
    digest = hashlib.sha256(review_request_id.encode("utf-8")).hexdigest()
    return f"plan-critic/{suffix}/{digest}"


def _planner_artifact_id(planning_request_id: str, suffix: str) -> str:
    digest = hashlib.sha256(planning_request_id.encode("utf-8")).hexdigest()
    if suffix == "result":
        return f"planning-result/response/{digest}"
    return f"planner-task/{suffix}/{digest}"


def _load_payload(campaign_dir, campaign_id, artifact_id, kind="decision") -> dict[str, Any] | None:
    try:
        envelope = intel.get_artifact(pathlib.Path(campaign_dir), campaign_id, artifact_id)["artifact"]
    except intel.IntelligenceArtifactInvalid as error:
        if str(error).startswith("artifact not found"):
            return None
        raise PlanCriticInputInvalid("critic dependency artifact is invalid") from error
    if envelope.get("kind") != kind or not isinstance(envelope.get("payload"), dict):
        raise PlanCriticInputInvalid("critic dependency artifact kind/payload invalid")
    return envelope["payload"]


def _validate_timestamp(value: Any) -> None:
    if not isinstance(value, str):
        raise PlanReviewInvalid("created_at must be RFC3339")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise PlanReviewInvalid("created_at must be RFC3339") from error
    if parsed.tzinfo is None or "T" not in value:
        raise PlanReviewInvalid("created_at must include an explicit timezone")


def _path_within(path: str, allowed: str) -> bool:
    prefix = allowed.rstrip("/")
    return path == prefix or path.startswith(prefix + "/")


def _has_cycle(plan: dict[str, Any]) -> bool:
    graph = {work_unit["id"]: list(work_unit["dependencies"]) for work_unit in plan.get("work_units", []) if isinstance(work_unit, dict) and isinstance(work_unit.get("id"), str)}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        for dependency in graph.get(node, []):
            if dependency not in graph or visit(dependency):
                return True
        visiting.remove(node)
        visited.add(node)
        return False

    return any(visit(node) for node in graph)


def compute_preflight(context: dict[str, Any]) -> dict[str, Any]:
    """Compute deterministic minimum review checks before accepting critic output."""
    plan = context["plan_revision"]
    result = context["planning_result"]
    request = context["planning_request"]
    repository = context["repository_context"]
    backlog_items = context["normalized_backlog"]["items"]
    checks = {name: "PASS" for name in CHECK_NAMES}
    defects: list[dict[str, Any]] = []

    def defect(category: str, message: str, work_units: list[str], evidence: str, *, blocked: bool = False) -> None:
        checks[category] = "BLOCKED" if blocked else "FAIL"
        defects.append({
            "category": category, "message": message,
            "work_unit_ids": sorted(set(work_units)), "evidence_reference": evidence,
        })

    work_units = plan.get("work_units", []) if isinstance(plan, dict) else []
    work_unit_ids = {wu.get("id") for wu in work_units if isinstance(wu, dict)}
    expected_backlog = {item.get("backlog_item_id") for item in backlog_items if isinstance(item, dict)}
    coverage = result.get("coverage_matrix", []) if isinstance(result, dict) else []
    covered_backlog = {entry.get("backlog_item_id") for entry in coverage if isinstance(entry, dict)}
    coverage_wus = {
        work_unit_id for entry in coverage if isinstance(entry, dict)
        for work_unit_id in entry.get("work_unit_ids", []) if isinstance(work_unit_id, str)
    }
    if covered_backlog != expected_backlog or not coverage_wus.issubset(work_unit_ids):
        defect("coverage", "coverage matrix does not exactly map authoritative backlog items to existing WUs", [], "planning_result.coverage_matrix")

    constraints = request.get("constraints", {}) if isinstance(request, dict) else {}
    allowed = constraints.get("allowed_paths", []) if isinstance(constraints, dict) else []
    forbidden = constraints.get("forbidden_paths", []) if isinstance(constraints, dict) else []
    bad_scope: list[str] = []
    for wu in work_units:
        scope = wu.get("scope", {}) if isinstance(wu, dict) else {}
        wu_allowed = scope.get("allowed_paths", []) if isinstance(scope, dict) else []
        wu_forbidden = scope.get("forbidden_paths", []) if isinstance(scope, dict) else []
        invalid = not wu_allowed or any(not isinstance(path, str) or not path for path in [*wu_allowed, *wu_forbidden])
        invalid = invalid or bool(set(wu_allowed) & set(wu_forbidden))
        if allowed:
            invalid = invalid or any(not any(_path_within(path, parent) for parent in allowed) for path in wu_allowed)
        invalid = invalid or any(any(_path_within(path, denied) for denied in forbidden) for path in wu_allowed)
        if invalid:
            bad_scope.append(wu.get("id", ""))
    if bad_scope:
        defect("scope", "WU scope is empty, conflicting, or outside planning constraints", bad_scope, "plan_revision.work_units[].scope")

    bad_verification = []
    for wu in work_units:
        verification = wu.get("verification", {}) if isinstance(wu, dict) else {}
        criteria = wu.get("acceptance_criteria", []) if isinstance(wu, dict) else []
        has_method = bool(verification.get("commands") or verification.get("manual_checks") or verification.get("discover_when_missing") is True) if isinstance(verification, dict) else False
        if not criteria or any(not isinstance(item, str) or not item for item in criteria) or not has_method:
            bad_verification.append(wu.get("id", ""))
    if bad_verification:
        defect("verification", "WU lacks acceptance criteria or an executable/discoverable verification method", bad_verification, "plan_revision.work_units[].verification")

    if _has_cycle(plan):
        defect("dependencies", "dependency graph contains a cycle or missing node", sorted(work_unit_ids), "plan_revision.edges")

    bad_risk = [
        wu.get("id", "") for wu in work_units if isinstance(wu, dict)
        and wu.get("risk_class") == "unbounded"
        and wu.get("required_route") not in {"frontier", "frontier-gated"}
    ]
    if bad_risk:
        defect("risk", "unbounded WU does not require a frontier route", bad_risk, "plan_revision.work_units[].risk_class")

    assumptions = plan.get("assumptions", []) if isinstance(plan, dict) else []
    unresolved_assumptions = [entry for entry in assumptions if isinstance(entry, dict) and entry.get("evidence_status") == "unverified"]
    analysis = result.get("analysis", {}) if isinstance(result, dict) else {}
    if unresolved_assumptions or (isinstance(analysis, dict) and (analysis.get("assumptions") or analysis.get("uncertainties"))):
        defect("unsupported_assumptions", "plan retains unsupported assumptions or uncertainties", [], "planning_result.analysis", blocked=True)

    gate_ids = {
        gate.get("gate_id") for wu in work_units if isinstance(wu, dict)
        for gate in wu.get("human_gates", []) if isinstance(gate, dict)
    }
    missing_resolution_gates = [
        entry for entry in assumptions if isinstance(entry, dict)
        and entry.get("resolution_gate") is not None and entry.get("resolution_gate") not in gate_ids
    ]
    unresolved_decisions = repository.get("unresolved_decisions", []) if isinstance(repository, dict) else []
    if missing_resolution_gates:
        defect("human_gates", "assumption references a missing human gate", [], "plan_revision.assumptions")
    elif unresolved_decisions and not gate_ids:
        defect("human_gates", "repository context contains unresolved product decisions without human gates", [], "repository_context.unresolved_decisions", blocked=True)

    return {
        "checks": checks, "defects": defects,
        "invalid_dag": checks["dependencies"] == "FAIL",
        "hidden_product_decision": checks["unsupported_assumptions"] == "BLOCKED" or checks["human_gates"] == "BLOCKED",
        "digest": _sha({"checks": checks, "defects": defects}),
    }


def _load_context(campaign_dir, campaign_id, planning_request_id, planner_task_id, plan_record_id) -> dict[str, Any]:
    planner_intent = _load_payload(campaign_dir, campaign_id, _planner_artifact_id(planning_request_id, "intent"))
    if planner_intent is None or planner_intent.get("agent") != "myrmex-planner" or planner_intent.get("task_id") != planner_task_id:
        raise PlanCriticInputInvalid("planner task identity is absent or mismatched")
    planning_context = planner.build_planning_context(campaign_dir, campaign_id, planning_request_id)
    planning_result = _load_payload(campaign_dir, campaign_id, _planner_artifact_id(planning_request_id, "result"), kind="plan")
    if planning_result is None:
        raise PlanCriticInputInvalid("planner result is absent")
    try:
        planner.validate_planning_result(planning_context["request"], planning_result)
        proposed = plan_store.get_plan_record(campaign_dir, campaign_id, plan_record_id)
    except Exception as error:
        raise PlanCriticInputInvalid("planner result or proposed plan is invalid") from error
    if (
        planning_result.get("response_type") != "plan"
        or planning_result.get("plan_revision") != proposed
        or proposed.get("lifecycle_status") != "proposed"
    ):
        raise PlanCriticInputInvalid("critic target is not the exact proposed planner result")
    context = {
        "schema": CONTEXT_SCHEMA,
        "planning_request": planning_context["request"],
        "planner_task_intent": planner_intent,
        "planning_result": planning_result,
        "plan_revision": proposed,
        "repository_context": planning_context["repository_context"],
        "normalized_backlog": planning_context["normalized_backlog"],
    }
    context["deterministic_preflight"] = compute_preflight(context)
    return context


def prepare_critic_task(
    campaign_dir, campaign_id, observed_campaign_revision, review_request_id,
    critic_task_id, planning_request_id, planner_task_id, plan_record_id,
    created_at=None,
):
    for label, value in (("review_request_id", review_request_id), ("critic_task_id", critic_task_id), ("planner_task_id", planner_task_id)):
        if not isinstance(value, str) or not TASK_ID_RE.fullmatch(value):
            raise PlanCriticTaskInvalid(f"{label} is invalid")
    if critic_task_id in {planner_task_id, review_request_id}:
        raise PlanCriticTaskInvalid("critic task identity must differ from planner task and review request")
    if not isinstance(observed_campaign_revision, int) or isinstance(observed_campaign_revision, bool) or observed_campaign_revision < 0:
        raise PlanCriticTaskInvalid("observed campaign revision is invalid")
    context = _load_context(campaign_dir, campaign_id, planning_request_id, planner_task_id, plan_record_id)
    proposed = context["plan_revision"]
    prompt = "You are the independent myrmex-plan-critic. Return exactly one myrmex.plan-review/v1 object. Review only; authorize no effects.\n\n" + _canon(context).decode("utf-8")
    timestamp = DEFAULT_CREATED_AT if created_at is None else created_at
    _validate_timestamp(timestamp)
    intent = {
        "schema": TASK_INTENT_SCHEMA, "review_request_id": review_request_id,
        "critic_task_id": critic_task_id, "planner_task_id": planner_task_id,
        "agent": AGENT_NAME, "run_id": context["planning_request"]["run_id"],
        "campaign_id": campaign_id, "objective_id": proposed["objective_id"],
        "planning_request_id": planning_request_id,
        "plan_revision_id": proposed["plan_revision_id"], "plan_record_id": proposed["record_id"],
        "plan_digest": proposed["plan_digest"], "plan_record_digest": proposed["record_digest"],
        "preflight_digest": context["deterministic_preflight"]["digest"],
        "prompt_digest": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "authority": dict(AUTHORITY), "created_at": timestamp,
    }
    artifact_id = _artifact_id(review_request_id, "intent")
    existing = _load_payload(campaign_dir, campaign_id, artifact_id)
    if existing is not None and _canon(existing) != _canon(intent):
        raise PlanCriticTaskConflict("critic task identity conflicts with durable intent")
    if existing is None:
        try:
            intel.put_artifact(pathlib.Path(campaign_dir), campaign_id, observed_campaign_revision, "decision", artifact_id, intent)
        except intel.IntelligenceArtifactConflict as error:
            existing = _load_payload(campaign_dir, campaign_id, artifact_id)
            if existing is None or _canon(existing) != _canon(intent):
                raise PlanCriticTaskConflict("critic task intent conflict") from error
    return {"context": context, "prompt": prompt, "task_intent": intent, "task_intent_artifact_id": artifact_id}


def validate_review(intent: dict[str, Any], context: dict[str, Any], review: Any) -> dict[str, Any]:
    if not isinstance(review, dict) or set(review) != set(REVIEW_FIELDS):
        raise PlanReviewInvalid("review fields invalid")
    expected = {
        "schema": REVIEW_SCHEMA, "review_request_id": intent["review_request_id"],
        "run_id": intent["run_id"], "campaign_id": intent["campaign_id"],
        "objective_id": intent["objective_id"], "critic_task_id": intent["critic_task_id"],
        "planner_task_id": intent["planner_task_id"], "plan_revision_id": intent["plan_revision_id"],
        "plan_record_id": intent["plan_record_id"], "plan_digest": intent["plan_digest"],
        "plan_record_digest": intent["plan_record_digest"], "authority": AUTHORITY,
    }
    if any(review.get(key) != value for key, value in expected.items()):
        raise PlanReviewInvalid("review identity or authority mismatch")
    if review["critic_task_id"] == review["planner_task_id"]:
        raise PlanReviewInvalid("planner task identity cannot be accepted as critic identity")
    if review["verdict"] not in VERDICTS or not isinstance(review["checks"], dict) or set(review["checks"]) != set(CHECK_NAMES):
        raise PlanReviewInvalid("review verdict/checks invalid")
    if any(value not in CHECK_VALUES for value in review["checks"].values()):
        raise PlanReviewInvalid("review check value invalid")
    if not isinstance(review["defects"], list) or not isinstance(review["summary"], str) or not review["summary"]:
        raise PlanReviewInvalid("review defects/summary invalid")
    work_unit_ids = {work_unit["id"] for work_unit in context["plan_revision"]["work_units"]}
    defect_ids: set[str] = set()
    defect_categories: set[str] = set()
    for defect in review["defects"]:
        if not isinstance(defect, dict) or set(defect) != {"defect_id", "category", "severity", "message", "work_unit_ids", "evidence_references"}:
            raise PlanReviewInvalid("review defect fields invalid")
        if not isinstance(defect["defect_id"], str) or not DEFECT_ID_RE.fullmatch(defect["defect_id"]) or defect["defect_id"] in defect_ids:
            raise PlanReviewInvalid("review defect identity invalid")
        if defect["category"] not in DEFECT_CATEGORIES or defect["severity"] not in {"BLOCKING", "MAJOR", "MINOR"}:
            raise PlanReviewInvalid("review defect category/severity invalid")
        if not isinstance(defect["message"], str) or not defect["message"] or not isinstance(defect["work_unit_ids"], list) or len(set(defect["work_unit_ids"])) != len(defect["work_unit_ids"]) or any(item not in work_unit_ids for item in defect["work_unit_ids"]):
            raise PlanReviewInvalid("review defect message/WU references invalid")
        if not isinstance(defect["evidence_references"], list) or not defect["evidence_references"] or any(not isinstance(item, str) or not item for item in defect["evidence_references"]):
            raise PlanReviewInvalid("review defect evidence references invalid")
        defect_ids.add(defect["defect_id"])
        defect_categories.add(defect["category"])
    if review["review_digest"] != _sha({key: value for key, value in review.items() if key != "review_digest"}):
        raise PlanReviewInvalid("review digest mismatch")
    _validate_timestamp(review["created_at"])
    preflight = context["deterministic_preflight"]
    for category in CHECK_NAMES:
        if preflight["checks"][category] != "PASS" and review["checks"][category] == "PASS":
            raise PlanReviewInvalid(f"review contradicts deterministic {category} preflight")
    if preflight["invalid_dag"] and review["verdict"] != "INVALID":
        raise PlanReviewInvalid("invalid dependency graph requires INVALID verdict")
    if not preflight["invalid_dag"] and preflight["hidden_product_decision"] and review["verdict"] != "BLOCKED":
        raise PlanReviewInvalid("hidden product decision requires BLOCKED verdict")
    if review["verdict"] == "PASS":
        if review["defects"] or any(value != "PASS" for value in review["checks"].values()):
            raise PlanReviewInvalid("PASS review requires all checks PASS and no defects")
    elif not review["defects"]:
        raise PlanReviewInvalid("non-PASS review requires defect references")
    for category, status in review["checks"].items():
        if status != "PASS" and category not in defect_categories:
            raise PlanReviewInvalid(f"failed review check lacks a {category} defect")
    if review["verdict"] == "INVALID" and not defect_categories.intersection({"identity", "structure", "dependencies"}):
        raise PlanReviewInvalid("INVALID review requires identity, structure, or dependency defect")
    return review


def _review_result(review_request_id: str, critic_task_id: str, payload: dict[str, Any], status: str) -> dict[str, Any]:
    return {
        "ok": True, "status": status, "critic_task_id": critic_task_id,
        "task_intent_artifact_id": _artifact_id(review_request_id, "intent"),
        "review_artifact_id": _artifact_id(review_request_id, "review"),
        "task_receipt_artifact_id": _artifact_id(review_request_id, "receipt"),
        "review_digest": payload["review_digest"], "verdict": payload["verdict"],
        "reviewed_plan_record_id": payload.get("reviewed_plan_record_id"),
        "defect_references": payload.get("defect_references", []),
    }


def record_review(campaign_dir, campaign_id, observed_campaign_revision, review_request_id, critic_task_id, review):
    intent = _load_payload(campaign_dir, campaign_id, _artifact_id(review_request_id, "intent"))
    if intent is None or intent.get("critic_task_id") != critic_task_id or intent.get("agent") != AGENT_NAME:
        raise PlanCriticTaskInvalid("review does not match durable critic task intent")
    context = _load_context(campaign_dir, campaign_id, intent["planning_request_id"], intent["planner_task_id"], intent["plan_record_id"])
    if context["deterministic_preflight"]["digest"] != intent["preflight_digest"]:
        raise PlanReviewConflict("critic preflight changed after task intent")
    validate_review(intent, context, review)
    review_artifact_id = _artifact_id(review_request_id, "review")
    existing_review = _load_payload(campaign_dir, campaign_id, review_artifact_id)
    if existing_review is not None and _canon(existing_review) != _canon(review):
        raise PlanReviewConflict("plan review conflicts with immutable result")
    if existing_review is None:
        try:
            persisted = intel.put_artifact(pathlib.Path(campaign_dir), campaign_id, observed_campaign_revision, "decision", review_artifact_id, review)
            review_status = persisted["status"]
        except intel.IntelligenceArtifactConflict as error:
            existing_review = _load_payload(campaign_dir, campaign_id, review_artifact_id)
            if existing_review is None or _canon(existing_review) != _canon(review):
                raise PlanReviewConflict("plan review persistence conflict") from error
            review_status = "reused"
    else:
        review_status = "reused"

    reviewed_plan_record_id = None
    if review["verdict"] == "PASS":
        head = plan_store.get_plan_head(campaign_dir, campaign_id, review["plan_revision_id"])
        if head["record_id"] == review["plan_record_id"] and head["lifecycle_status"] == "proposed":
            reviewed = plan_store.build_lifecycle_record(head, "reviewed", review["created_at"])
            lifecycle = plan_store.store_plan_record(campaign_dir, campaign_id, observed_campaign_revision, reviewed)
        elif head["lifecycle_status"] == "reviewed" and head["previous_record_id"] == review["plan_record_id"]:
            lifecycle = plan_store.store_plan_record(campaign_dir, campaign_id, observed_campaign_revision, head)
        else:
            raise PlanReviewConflict("PASS review target is no longer the current proposed/reviewed plan head")
        reviewed_plan_record_id = lifecycle["record_id"]

    receipt = {
        "schema": TASK_RECEIPT_SCHEMA, "review_request_id": review_request_id,
        "critic_task_id": critic_task_id, "planner_task_id": intent["planner_task_id"],
        "agent": AGENT_NAME, "review_digest": review["review_digest"],
        "verdict": review["verdict"], "plan_revision_id": review["plan_revision_id"],
        "plan_record_id": review["plan_record_id"], "reviewed_plan_record_id": reviewed_plan_record_id,
        "defect_references": [defect["defect_id"] for defect in review["defects"]],
        "identity_separation": critic_task_id != intent["planner_task_id"],
        "authority": dict(AUTHORITY), "created_at": review["created_at"],
    }
    receipt_id = _artifact_id(review_request_id, "receipt")
    existing_receipt = _load_payload(campaign_dir, campaign_id, receipt_id)
    if existing_receipt is not None and _canon(existing_receipt) != _canon(receipt):
        raise PlanReviewConflict("critic task receipt conflicts")
    if existing_receipt is None:
        try:
            persisted = intel.put_artifact(pathlib.Path(campaign_dir), campaign_id, observed_campaign_revision, "decision", receipt_id, receipt)
            receipt_status = persisted["status"]
        except intel.IntelligenceArtifactConflict as error:
            existing_receipt = _load_payload(campaign_dir, campaign_id, receipt_id)
            if existing_receipt is None or _canon(existing_receipt) != _canon(receipt):
                raise PlanReviewConflict("critic task receipt persistence conflict") from error
            receipt_status = "reused"
    else:
        receipt_status = "reused"
    payload = {**review, "reviewed_plan_record_id": reviewed_plan_record_id, "defect_references": receipt["defect_references"]}
    return _review_result(review_request_id, critic_task_id, payload, "reused" if review_status == "reused" and receipt_status == "reused" else "recorded")


def execute_critic_task(
    campaign_dir, campaign_id, observed_campaign_revision, review_request_id,
    critic_task_id, planning_request_id, planner_task_id, plan_record_id,
    executor: Callable[..., Any], created_at=None,
):
    prepared = prepare_critic_task(
        campaign_dir, campaign_id, observed_campaign_revision, review_request_id,
        critic_task_id, planning_request_id, planner_task_id, plan_record_id, created_at,
    )
    existing_receipt = _load_payload(campaign_dir, campaign_id, _artifact_id(review_request_id, "receipt"))
    if existing_receipt is not None:
        if existing_receipt.get("critic_task_id") != critic_task_id or existing_receipt.get("planner_task_id") != planner_task_id or existing_receipt.get("authority") != AUTHORITY:
            raise PlanReviewConflict("durable critic receipt identity invalid")
        return _review_result(review_request_id, critic_task_id, existing_receipt, "reused")
    existing_review = _load_payload(campaign_dir, campaign_id, _artifact_id(review_request_id, "review"))
    if existing_review is not None:
        return record_review(campaign_dir, campaign_id, observed_campaign_revision, review_request_id, critic_task_id, existing_review)
    response = executor(agent=AGENT_NAME, task_id=critic_task_id, prompt=prepared["prompt"])
    if not isinstance(response, dict) or set(response) != {"task_id", "review"} or response["task_id"] != critic_task_id:
        raise PlanCriticTaskInvalid("critic executor returned a mismatched task envelope")
    return record_review(campaign_dir, campaign_id, observed_campaign_revision, review_request_id, critic_task_id, response["review"])
