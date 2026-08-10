#!/usr/bin/env python3
"""Versioned, read-only deterministic WorkUnit priority policy for P1-015."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import pathlib
import re
from typing import Any

DECISION_SCHEMA = "myrmex.scheduling-decision/v1"
POLICY_SCHEMA = "myrmex.priority-policy/v1"
POLICY_VERSION = "1.0.0"
WU_ID_RE = re.compile(r"^WU-[A-Z0-9][A-Z0-9-]{0,30}$")
CAMPAIGN_ID_RE = re.compile(r"^camp-[a-z0-9][a-z0-9-]{4,60}$")
SCHEDULABLE = {"pending", "ready"}
ACTIVE = {"active", "verifying", "remediating", "ci", "delivering"}
TERMINAL = {"completed", "cancelled", "superseded"}
KNOWN_STATUSES = SCHEDULABLE | ACTIVE | TERMINAL | {"blocked", "failed"}
HARD_CONSTRAINTS = [
    "campaign_active", "schedulable_status", "dependencies_completed",
    "human_gates_cleared", "work_unit_unblocked", "work_unit_budget_available",
    "campaign_budget_available", "route_budget_available", "cost_budget_available",
    "failure_limit_available", "concurrency_available",
]
WEIGHTS = {
    "critical_path_length": 1000,
    "downstream_unlock_count": 100,
    "age_minutes": 1,
}
TIE_BREAKERS = ["score_desc", "work_unit_id_asc"]
AUTHORITY = {
    "scope": "schedule_preview_only", "dispatch": False, "start_run": False,
    "repository_write": False, "commit": False, "push": False,
}


class PriorityPolicyError(Exception): pass
class PriorityPolicyInputInvalid(PriorityPolicyError): pass
class PriorityPolicyStale(PriorityPolicyError): pass
class PriorityPolicyBackendUnavailable(PriorityPolicyError): pass


def _canon(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canon(value)).hexdigest()


def _time(value: Any, label: str) -> dt.datetime:
    if not isinstance(value, str) or "T" not in value or not (value.endswith("Z") or re.search(r"[+-][0-9]{2}:[0-9]{2}$", value)):
        raise PriorityPolicyInputInvalid(f"{label} must be timezone-aware RFC3339")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise PriorityPolicyInputInvalid(f"{label} must be timezone-aware RFC3339") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PriorityPolicyInputInvalid(f"{label} must include a timezone")
    return parsed


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PriorityPolicyInputInvalid(f"{label} must be a non-negative integer")
    return value


def _nonnegative_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise PriorityPolicyInputInvalid(f"{label} must be a non-negative number")
    return float(value)


def default_policy() -> dict[str, Any]:
    body = {
        "schema": POLICY_SCHEMA, "version": POLICY_VERSION,
        "hard_constraints": list(HARD_CONSTRAINTS), "weights": dict(WEIGHTS),
        "tie_breakers": list(TIE_BREAKERS),
    }
    return {**body, "policy_digest": _sha(body)}


def validate_policy(policy: Any) -> None:
    fields = {"schema", "version", "hard_constraints", "weights", "tie_breakers", "policy_digest"}
    if not isinstance(policy, dict) or set(policy) != fields:
        raise PriorityPolicyInputInvalid("priority policy fields invalid")
    if policy.get("schema") != POLICY_SCHEMA or policy.get("version") != POLICY_VERSION:
        raise PriorityPolicyInputInvalid("priority policy schema/version invalid")
    if policy.get("hard_constraints") != HARD_CONSTRAINTS or policy.get("weights") != WEIGHTS or policy.get("tie_breakers") != TIE_BREAKERS:
        raise PriorityPolicyInputInvalid("priority policy contents differ from the accepted deterministic version")
    if policy.get("policy_digest") != _sha({key: value for key, value in policy.items() if key != "policy_digest"}):
        raise PriorityPolicyInputInvalid("priority policy digest invalid")


def _load_campaign(campaign_dir: pathlib.Path, campaign_id: str, expected_revision: int) -> dict[str, Any]:
    path = pathlib.Path(campaign_dir) / "campaign.json"
    try:
        if path.is_symlink() or not path.is_file():
            raise PriorityPolicyBackendUnavailable("campaign state path unavailable or unsafe")
        campaign = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise PriorityPolicyBackendUnavailable("campaign state is unreadable") from error
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 1:
        raise PriorityPolicyInputInvalid("expected campaign revision must be positive")
    if not isinstance(campaign, dict) or campaign.get("id") != campaign_id or campaign.get("revision") != expected_revision:
        raise PriorityPolicyStale("campaign identity or revision is stale")
    return campaign


def _validate_campaign(campaign: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, list[str]], dict[str, list[str]]]:
    if not isinstance(campaign.get("id"), str) or not CAMPAIGN_ID_RE.fullmatch(campaign["id"]) or campaign.get("status") not in {"active", "paused", "completed", "failed", "cancelled"}:
        raise PriorityPolicyInputInvalid("campaign identity/status invalid")
    campaign_created = _time(campaign.get("created_at"), "campaign.created_at")
    campaign_updated = _time(campaign.get("updated_at"), "campaign.updated_at")
    if campaign_updated < campaign_created:
        raise PriorityPolicyInputInvalid("campaign updated_at predates created_at")
    work_units = campaign.get("work_units")
    if not isinstance(work_units, list):
        raise PriorityPolicyInputInvalid("campaign work_units must be an array")
    by_id: dict[str, dict[str, Any]] = {}
    dependencies: dict[str, list[str]] = {}
    successors: dict[str, list[str]] = {}
    for wu in work_units:
        if not isinstance(wu, dict) or not isinstance(wu.get("id"), str) or not WU_ID_RE.fullmatch(wu["id"]) or wu["id"] in by_id:
            raise PriorityPolicyInputInvalid("campaign WU identity invalid or duplicate")
        if wu.get("status") not in KNOWN_STATUSES:
            raise PriorityPolicyInputInvalid(f"campaign WU status invalid: {wu['id']}")
        deps = wu.get("dependencies")
        if not isinstance(deps, list) or any(not isinstance(item, str) for item in deps) or len(deps) != len(set(deps)) or wu["id"] in deps:
            raise PriorityPolicyInputInvalid(f"campaign WU dependencies invalid: {wu['id']}")
        _nonnegative_int(wu.get("corrections_used"), f"{wu['id']}.corrections_used")
        _nonnegative_int(wu.get("corrections_budget"), f"{wu['id']}.corrections_budget")
        if wu.get("required_route") not in {"auto", "direct-only", "delegated", "frontier", "frontier-gated"}:
            raise PriorityPolicyInputInvalid(f"campaign WU required_route invalid: {wu['id']}")
        order = wu.get("work_order")
        if order is not None:
            gates = order.get("human_gates") if isinstance(order, dict) else None
            if not isinstance(gates, list) or any(not isinstance(gate, dict) or not isinstance(gate.get("gate_id"), str) or not gate["gate_id"] or gate.get("required_before") not in {"plan_activation", "work_unit_ready", "repository_effect", "delivery"} for gate in gates):
                raise PriorityPolicyInputInvalid(f"campaign WU human gates invalid: {wu['id']}")
        by_id[wu["id"]] = wu
        dependencies[wu["id"]] = list(deps)
        successors[wu["id"]] = []
    for wu_id, deps in dependencies.items():
        for dependency in deps:
            if dependency not in by_id:
                raise PriorityPolicyInputInvalid(f"campaign dependency is missing: {dependency}")
            successors[dependency].append(wu_id)
    raw_edges = campaign.get("dag", {}).get("edges") if isinstance(campaign.get("dag"), dict) else None
    if not isinstance(raw_edges, list) or any(not isinstance(edge, list) or len(edge) != 2 or any(not isinstance(item, str) for item in edge) for edge in raw_edges):
        raise PriorityPolicyInputInvalid("campaign DAG edges invalid")
    expected_edges = sorted([dependency, wu_id] for wu_id, deps in dependencies.items() for dependency in deps)
    if sorted(raw_edges) != expected_edges or len(raw_edges) != len({tuple(edge) for edge in raw_edges}):
        raise PriorityPolicyInputInvalid("campaign DAG differs from WU dependencies")
    indegree = {wu_id: len(dependencies[wu_id]) for wu_id in by_id}
    queue = sorted(wu_id for wu_id, degree in indegree.items() if degree == 0)
    visited = []
    while queue:
        current = queue.pop(0); visited.append(current)
        for target in sorted(successors[current]):
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target); queue.sort()
    if len(visited) != len(by_id):
        raise PriorityPolicyInputInvalid("campaign DAG contains a cycle")
    budgets = campaign.get("budgets")
    if not isinstance(budgets, dict):
        raise PriorityPolicyInputInvalid("campaign budgets invalid")
    for key in ("corrections_global", "corrections_global_used", "frontier_calls", "frontier_calls_used", "max_consecutive_failures", "consecutive_failures", "max_concurrent_wu"):
        _nonnegative_int(budgets.get(key), f"budgets.{key}")
    if budgets["max_concurrent_wu"] < 1:
        raise PriorityPolicyInputInvalid("budgets.max_concurrent_wu must be positive")
    if budgets.get("estimated_cost_usd") is not None:
        _nonnegative_number(budgets["estimated_cost_usd"], "budgets.estimated_cost_usd")
    if budgets.get("cost_limit_usd") is not None:
        _nonnegative_number(budgets["cost_limit_usd"], "budgets.cost_limit_usd")
    active_work_unit = campaign.get("active_work_unit")
    if active_work_unit is not None and (not isinstance(active_work_unit, str) or active_work_unit not in by_id):
        raise PriorityPolicyInputInvalid("campaign active_work_unit identity invalid")
    return by_id, dependencies, successors


def _descendants(wu_id: str, successors: dict[str, list[str]]) -> set[str]:
    result: set[str] = set()
    stack = list(successors[wu_id])
    while stack:
        current = stack.pop()
        if current in result:
            continue
        result.add(current); stack.extend(successors[current])
    return result


def _critical_lengths(by_id: dict[str, dict[str, Any]], successors: dict[str, list[str]]) -> dict[str, int]:
    memo: dict[str, int] = {}
    def visit(wu_id: str) -> int:
        if wu_id in memo:
            return memo[wu_id]
        children = [child for child in successors[wu_id] if by_id[child]["status"] not in TERMINAL]
        memo[wu_id] = 1 + max((visit(child) for child in children), default=0)
        return memo[wu_id]
    for wu_id in by_id:
        visit(wu_id)
    return memo


def _exclusions(campaign: dict[str, Any], wu: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> list[str]:
    reasons = []
    budgets = campaign["budgets"]
    if campaign["status"] != "active":
        reasons.append("campaign_not_active")
    if wu["status"] not in SCHEDULABLE:
        reasons.append("status_not_schedulable:" + wu["status"])
    for dependency in sorted(wu["dependencies"]):
        if by_id[dependency]["status"] != "completed":
            reasons.append("dependency_not_completed:" + dependency)
    order = wu.get("work_order")
    gates = order.get("human_gates", []) if isinstance(order, dict) else []
    for gate in sorted((gate for gate in gates if isinstance(gate, dict) and gate.get("required_before") == "work_unit_ready"), key=lambda item: item.get("gate_id", "")):
        reasons.append("human_gate_required:" + str(gate.get("gate_id", "invalid")))
    if wu.get("blocker") is not None:
        blocker_type = wu["blocker"].get("type", "unknown") if isinstance(wu["blocker"], dict) else "invalid"
        reasons.append("work_unit_blocker_present:" + str(blocker_type))
    if wu["corrections_used"] >= wu["corrections_budget"]:
        reasons.append("work_unit_correction_budget_exhausted")
    if budgets["corrections_global_used"] >= budgets["corrections_global"]:
        reasons.append("campaign_correction_budget_exhausted")
    if wu.get("required_route") in {"frontier", "frontier-gated"} and budgets["frontier_calls_used"] >= budgets["frontier_calls"]:
        reasons.append("frontier_call_budget_exhausted")
    if budgets.get("cost_limit_usd") is not None and (budgets.get("estimated_cost_usd") or 0) >= budgets["cost_limit_usd"]:
        reasons.append("cost_budget_exhausted")
    if budgets["consecutive_failures"] >= budgets["max_consecutive_failures"]:
        reasons.append("consecutive_failure_limit_reached")
    active_count = sum(1 for item in by_id.values() if item["status"] in ACTIVE)
    if active_count >= budgets["max_concurrent_wu"] or campaign.get("active_work_unit") is not None:
        reasons.append("concurrency_limit_reached")
    return reasons


def validate_decision(decision: Any) -> None:
    fields = {"schema", "decision_id", "decision_digest", "campaign_id", "campaign_revision", "campaign_state_digest", "observed_campaign_updated_at", "policy", "considered_candidates", "eligible_work_unit_ids", "selected_work_unit_id", "selection_reason", "authority"}
    if not isinstance(decision, dict) or set(decision) != fields or decision.get("schema") != DECISION_SCHEMA:
        raise PriorityPolicyInputInvalid("scheduling decision fields/schema invalid")
    try:
        digest = _sha({key: value for key, value in decision.items() if key not in {"decision_id", "decision_digest"}})
    except (TypeError, ValueError) as error:
        raise PriorityPolicyInputInvalid("scheduling decision is not canonical JSON") from error
    if decision.get("decision_digest") != digest or decision.get("decision_id") != "schedule_" + digest:
        raise PriorityPolicyInputInvalid("scheduling decision digest identity invalid")
    validate_policy(decision.get("policy"))
    if not isinstance(decision.get("campaign_id"), str) or not CAMPAIGN_ID_RE.fullmatch(decision["campaign_id"]) or isinstance(decision.get("campaign_revision"), bool) or not isinstance(decision.get("campaign_revision"), int) or decision["campaign_revision"] < 1 or not isinstance(decision.get("campaign_state_digest"), str) or not re.fullmatch(r"[0-9a-f]{64}", decision["campaign_state_digest"]):
        raise PriorityPolicyInputInvalid("scheduling decision campaign identity invalid")
    _time(decision.get("observed_campaign_updated_at"), "decision.observed_campaign_updated_at")
    rows = decision.get("considered_candidates")
    row_fields = {"work_unit_id", "status", "source_index", "eligible", "exclusion_reasons", "features", "score", "rank"}
    feature_fields = {"age_minutes", "critical_path_length", "downstream_unlock_count", "dependency_count", "completed_dependency_count"}
    if not isinstance(rows, list) or any(not isinstance(row, dict) or set(row) != row_fields for row in rows):
        raise PriorityPolicyInputInvalid("scheduling candidate rows invalid")
    ids = []
    eligible_rows = []
    source_indexes = []
    for row in rows:
        if not isinstance(row["work_unit_id"], str) or not WU_ID_RE.fullmatch(row["work_unit_id"]) or row["status"] not in KNOWN_STATUSES or isinstance(row["source_index"], bool) or not isinstance(row["source_index"], int) or row["source_index"] < 0 or not isinstance(row["eligible"], bool):
            raise PriorityPolicyInputInvalid("scheduling candidate identity invalid")
        if not isinstance(row["exclusion_reasons"], list) or any(not isinstance(reason, str) or not reason for reason in row["exclusion_reasons"]) or len(row["exclusion_reasons"]) != len(set(row["exclusion_reasons"])):
            raise PriorityPolicyInputInvalid("scheduling exclusion reasons invalid")
        features = row["features"]
        if not isinstance(features, dict) or set(features) != feature_fields or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in features.values()):
            raise PriorityPolicyInputInvalid("scheduling candidate features invalid")
        if row["eligible"]:
            expected_score = sum(features[name] * decision["policy"]["weights"][name] for name in WEIGHTS)
            if row["exclusion_reasons"] or isinstance(row["score"], bool) or not isinstance(row["score"], int) or row["score"] != expected_score or isinstance(row["rank"], bool) or not isinstance(row["rank"], int) or row["rank"] < 1:
                raise PriorityPolicyInputInvalid("eligible scheduling candidate fields invalid")
            eligible_rows.append(row)
        elif not row["exclusion_reasons"] or row["score"] is not None or row["rank"] is not None:
            raise PriorityPolicyInputInvalid("excluded scheduling candidate lacks exact reason/null score")
        ids.append(row["work_unit_id"])
        source_indexes.append(row["source_index"])
    if ids != sorted(set(ids)):
        raise PriorityPolicyInputInvalid("scheduling candidates must be unique stable-ID order")
    if sorted(source_indexes) != list(range(len(rows))):
        raise PriorityPolicyInputInvalid("scheduling candidate source indexes invalid")
    expected_ranking = sorted(eligible_rows, key=lambda row: (-row["score"], row["work_unit_id"]))
    if [row["rank"] for row in expected_ranking] != list(range(1, len(expected_ranking) + 1)):
        raise PriorityPolicyInputInvalid("scheduling candidate rank order invalid")
    eligible = decision.get("eligible_work_unit_ids")
    if not isinstance(eligible, list) or eligible != [row["work_unit_id"] for row in expected_ranking]:
        raise PriorityPolicyInputInvalid("scheduling ranking invalid")
    selected = decision.get("selected_work_unit_id")
    if selected != (eligible[0] if eligible else None):
        raise PriorityPolicyInputInvalid("scheduling selection does not match ranking")
    expected_reason = "highest_score_then_stable_id" if eligible else "no_eligible_work_unit"
    if decision.get("selection_reason") != expected_reason or decision.get("authority") != AUTHORITY:
        raise PriorityPolicyInputInvalid("scheduling selection reason/authority invalid")


def preview_schedule(campaign_dir: pathlib.Path, campaign_id: str, expected_revision: int, policy: dict[str, Any] | None = None) -> dict[str, Any]:
    campaign = _load_campaign(pathlib.Path(campaign_dir), campaign_id, expected_revision)
    by_id, dependencies, successors = _validate_campaign(campaign)
    selected_policy = default_policy() if policy is None else json.loads(json.dumps(policy))
    validate_policy(selected_policy)
    observed = _time(campaign["updated_at"], "campaign.updated_at")
    campaign_created = _time(campaign["created_at"], "campaign.created_at")
    critical = _critical_lengths(by_id, successors)
    source_index = {wu["id"]: index for index, wu in enumerate(campaign["work_units"])}
    rows = []
    for wu_id in sorted(by_id):
        wu = by_id[wu_id]
        created = _time(wu.get("created_at", campaign["created_at"]), f"{wu_id}.created_at")
        if created < campaign_created or created > observed:
            raise PriorityPolicyInputInvalid(f"{wu_id}.created_at is outside campaign history")
        descendants = _descendants(wu_id, successors)
        features = {
            "age_minutes": max(0, int((observed - created).total_seconds() // 60)),
            "critical_path_length": critical[wu_id],
            "downstream_unlock_count": sum(1 for item in descendants if by_id[item]["status"] not in TERMINAL),
            "dependency_count": len(dependencies[wu_id]),
            "completed_dependency_count": sum(1 for item in dependencies[wu_id] if by_id[item]["status"] == "completed"),
        }
        reasons = _exclusions(campaign, wu, by_id)
        eligible = not reasons
        score = sum(features[name] * selected_policy["weights"][name] for name in WEIGHTS) if eligible else None
        rows.append({
            "work_unit_id": wu_id, "status": wu["status"], "source_index": source_index[wu_id],
            "eligible": eligible, "exclusion_reasons": reasons, "features": features,
            "score": score, "rank": None,
        })
    eligible_rows = sorted((row for row in rows if row["eligible"]), key=lambda row: (-row["score"], row["work_unit_id"]))
    for rank, row in enumerate(eligible_rows, 1):
        row["rank"] = rank
    eligible_ids = [row["work_unit_id"] for row in eligible_rows]
    body = {
        "schema": DECISION_SCHEMA, "campaign_id": campaign_id,
        "campaign_revision": expected_revision, "campaign_state_digest": _sha(campaign),
        "observed_campaign_updated_at": campaign["updated_at"], "policy": selected_policy,
        "considered_candidates": rows, "eligible_work_unit_ids": eligible_ids,
        "selected_work_unit_id": eligible_ids[0] if eligible_ids else None,
        "selection_reason": "highest_score_then_stable_id" if eligible_ids else "no_eligible_work_unit",
        "authority": dict(AUTHORITY),
    }
    digest = _sha(body)
    decision = {**body, "decision_id": "schedule_" + digest, "decision_digest": digest}
    validate_decision(decision)
    return decision
