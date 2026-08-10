#!/usr/bin/env python3
"""Deterministic, read-only route and model policy for P1-016."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import pathlib
import re
from typing import Any

DECISION_SCHEMA = "myrmex.route-model-decision/v1"
REQUEST_SCHEMA = "myrmex.route-model-request/v1"
POLICY_SCHEMA = "myrmex.route-model-policy/v1"
AVAILABILITY_SCHEMA = "myrmex.route-model-availability/v1"
POLICY_VERSION = "1.0.0"
ROUTES = {"direct", "delegated", "frontier"}
REQUIRED_ROUTES = {"auto", "direct-only", "delegated", "frontier", "frontier-gated"}
SELECTION_ORDER = ["priority_asc", "option_id_asc"]
WU_ID_RE = re.compile(r"^WU-[A-Z0-9][A-Z0-9-]{0,30}$")
PLAN_ID_RE = re.compile(r"^plan_[0-9a-f]{64}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
AGENT_RE = re.compile(r"^myrmex-[a-z0-9][a-z0-9-]{0,62}$")
MODEL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$")
CREDENTIAL_HANDLING = {
    "inspection": "not_performed",
    "request": "not_performed",
    "reason": "provider credentials are owned by the transport and are not route/model policy inputs",
}
AUTHORITY = {
    "scope": "route_model_decision_only",
    "dispatch": False,
    "invoke_agent": False,
    "invoke_provider": False,
    "repository_write": False,
    "commit": False,
    "push": False,
}
REASON_TO_BLOCKER = [
    ("agent_model_unresolved", "AGENT_MODEL_UNRESOLVED"),
    ("agent_not_installed:", "AGENT_NOT_INSTALLED"),
    ("provider_not_allowed:", "BLOCKED_NON_ALLOWED_PROVIDER"),
    ("model_not_allowed:", "BLOCKED_NON_ALLOWED_MODEL"),
    ("option_role_mismatch:", "BLOCKED_ROLE_INCOMPATIBLE"),
    ("role_not_configured:", "BLOCKED_ROLE_INCOMPATIBLE"),
    ("role_agent_incompatible:", "BLOCKED_ROLE_INCOMPATIBLE"),
    ("role_route_incompatible:", "BLOCKED_ROLE_INCOMPATIBLE"),
    ("route_not_allowed:", "BLOCKED_ROUTE_INCOMPATIBLE"),
    ("required_route_mismatch:", "BLOCKED_ROUTE_INCOMPATIBLE"),
    ("provider_unavailable:", "PROVIDER_UNAVAILABLE"),
    ("provider_availability_unknown:", "PROVIDER_UNAVAILABLE"),
    ("model_unavailable:", "MODEL_UNAVAILABLE"),
    ("model_availability_unknown:", "MODEL_UNAVAILABLE"),
    ("insufficient_budget:", "BLOCKED_INSUFFICIENT_BUDGET"),
]


class RouteModelPolicyError(Exception):
    pass


class RouteModelPolicyInputInvalid(RouteModelPolicyError):
    pass


def _canon(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canon(value)).hexdigest()


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RouteModelPolicyInputInvalid(f"{label} must be an object")
    return value


def _fields(value: dict[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise RouteModelPolicyInputInvalid(f"{label} fields invalid")


def _identifier(value: Any, label: str, pattern: re.Pattern[str] = IDENTIFIER_RE) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise RouteModelPolicyInputInvalid(f"{label} invalid")
    return value


def _money(value: Any, label: str) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise RouteModelPolicyInputInvalid(f"{label} must be a non-negative finite number")
    return value


def compile_request(source: Any) -> dict[str, Any]:
    request = _mapping(source, "route/model request")
    allowed = {"schema", "work_unit_id", "plan_revision_id", "plan_revision_digest", "required_route", "required_role", "budget_headroom_usd", "request_digest"}
    if set(request) not in (allowed, allowed - {"request_digest"}):
        raise RouteModelPolicyInputInvalid("route/model request fields invalid")
    if request.get("schema") != REQUEST_SCHEMA:
        raise RouteModelPolicyInputInvalid("route/model request schema invalid")
    _identifier(request.get("work_unit_id"), "work_unit_id", WU_ID_RE)
    _identifier(request.get("plan_revision_id"), "plan_revision_id", PLAN_ID_RE)
    _identifier(request.get("plan_revision_digest"), "plan_revision_digest", DIGEST_RE)
    if request.get("required_route") not in REQUIRED_ROUTES:
        raise RouteModelPolicyInputInvalid("required_route invalid")
    _identifier(request.get("required_role"), "required_role")
    _money(request.get("budget_headroom_usd"), "budget_headroom_usd")
    body = {key: request[key] for key in allowed if key != "request_digest"}
    body = {key: body[key] for key in sorted(body)}
    digest = _sha(body)
    if "request_digest" in request and request["request_digest"] != digest:
        raise RouteModelPolicyInputInvalid("route/model request digest invalid")
    return {**body, "request_digest": digest}


def _compile_option(source: Any, label: str) -> dict[str, Any]:
    option = _mapping(source, label)
    expected = {"option_id", "priority", "route", "agent", "role", "provider", "model", "max_estimated_cost_usd"}
    _fields(option, expected, label)
    _identifier(option.get("option_id"), f"{label}.option_id")
    priority = option.get("priority")
    if isinstance(priority, bool) or not isinstance(priority, int) or priority < 0:
        raise RouteModelPolicyInputInvalid(f"{label}.priority invalid")
    if option.get("route") not in ROUTES:
        raise RouteModelPolicyInputInvalid(f"{label}.route invalid")
    _identifier(option.get("agent"), f"{label}.agent", AGENT_RE)
    _identifier(option.get("role"), f"{label}.role")
    provider = option.get("provider")
    model = option.get("model")
    if provider is None or model is None:
        if provider is not None or model is not None:
            raise RouteModelPolicyInputInvalid(f"{label} provider/model must both be resolved or null")
    else:
        _identifier(provider, f"{label}.provider")
        _identifier(model, f"{label}.model", MODEL_RE)
        if not model.startswith(provider + "/"):
            raise RouteModelPolicyInputInvalid(f"{label} provider does not own model")
    _money(option.get("max_estimated_cost_usd"), f"{label}.max_estimated_cost_usd")
    return {key: option[key] for key in ("option_id", "priority", "route", "agent", "role", "provider", "model", "max_estimated_cost_usd")}


def compile_policy(source: Any) -> dict[str, Any]:
    policy = _mapping(source, "route/model policy")
    allowed = {"schema", "version", "adaptive_scoring", "allowed_routes", "allowed_provider_prefixes", "allowed_models", "role_compatibility", "selection_order", "options", "policy_digest"}
    if set(policy) not in (allowed, allowed - {"policy_digest"}):
        raise RouteModelPolicyInputInvalid("route/model policy fields invalid")
    if policy.get("schema") != POLICY_SCHEMA or policy.get("version") != POLICY_VERSION:
        raise RouteModelPolicyInputInvalid("route/model policy schema/version invalid")
    if policy.get("adaptive_scoring") is not False:
        raise RouteModelPolicyInputInvalid("adaptive scoring is forbidden in P1 route/model policy")
    if policy.get("selection_order") != SELECTION_ORDER:
        raise RouteModelPolicyInputInvalid("route/model selection order invalid")

    routes = policy.get("allowed_routes")
    if not isinstance(routes, list) or not routes or any(route not in ROUTES for route in routes) or len(routes) != len(set(routes)):
        raise RouteModelPolicyInputInvalid("allowed_routes invalid")
    prefixes = policy.get("allowed_provider_prefixes")
    if not isinstance(prefixes, list) or not prefixes or any(not isinstance(item, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*/", item) for item in prefixes) or len(prefixes) != len(set(prefixes)):
        raise RouteModelPolicyInputInvalid("allowed_provider_prefixes invalid")
    models = policy.get("allowed_models")
    if not isinstance(models, list) or not models or any(not isinstance(item, str) or not MODEL_RE.fullmatch(item) for item in models) or len(models) != len(set(models)):
        raise RouteModelPolicyInputInvalid("allowed_models invalid")

    compatibility = policy.get("role_compatibility")
    if not isinstance(compatibility, list) or not compatibility:
        raise RouteModelPolicyInputInvalid("role_compatibility invalid")
    compiled_roles = []
    for index, source_role in enumerate(compatibility):
        role = _mapping(source_role, f"role_compatibility[{index}]")
        _fields(role, {"role", "agents", "routes"}, f"role_compatibility[{index}]")
        _identifier(role.get("role"), f"role_compatibility[{index}].role")
        agents = role.get("agents")
        role_routes = role.get("routes")
        if not isinstance(agents, list) or not agents or any(not isinstance(agent, str) or not AGENT_RE.fullmatch(agent) for agent in agents) or len(agents) != len(set(agents)):
            raise RouteModelPolicyInputInvalid(f"role_compatibility[{index}].agents invalid")
        if not isinstance(role_routes, list) or not role_routes or any(route not in ROUTES for route in role_routes) or len(role_routes) != len(set(role_routes)):
            raise RouteModelPolicyInputInvalid(f"role_compatibility[{index}].routes invalid")
        compiled_roles.append({"role": role["role"], "agents": sorted(agents), "routes": sorted(role_routes)})
    compiled_roles.sort(key=lambda row: row["role"])
    if len({row["role"] for row in compiled_roles}) != len(compiled_roles):
        raise RouteModelPolicyInputInvalid("role_compatibility contains duplicate roles")

    options = policy.get("options")
    if not isinstance(options, list) or not options:
        raise RouteModelPolicyInputInvalid("route/model policy options invalid")
    compiled_options = sorted((_compile_option(option, f"options[{index}]") for index, option in enumerate(options)), key=lambda option: (option["priority"], option["option_id"]))
    if len({option["option_id"] for option in compiled_options}) != len(compiled_options):
        raise RouteModelPolicyInputInvalid("route/model option IDs must be unique")

    body = {
        "schema": POLICY_SCHEMA,
        "version": POLICY_VERSION,
        "adaptive_scoring": False,
        "allowed_routes": sorted(routes),
        "allowed_provider_prefixes": sorted(prefixes),
        "allowed_models": sorted(models),
        "role_compatibility": compiled_roles,
        "selection_order": list(SELECTION_ORDER),
        "options": compiled_options,
    }
    digest = _sha(body)
    if "policy_digest" in policy and policy["policy_digest"] != digest:
        raise RouteModelPolicyInputInvalid("route/model policy digest invalid")
    return {**body, "policy_digest": digest}


def compile_availability(source: Any) -> dict[str, Any]:
    availability = _mapping(source, "route/model availability")
    allowed = {"schema", "agents", "providers", "models", "availability_digest"}
    if set(availability) not in (allowed, allowed - {"availability_digest"}):
        raise RouteModelPolicyInputInvalid("route/model availability fields invalid")
    if availability.get("schema") != AVAILABILITY_SCHEMA:
        raise RouteModelPolicyInputInvalid("route/model availability schema invalid")
    compiled: dict[str, Any] = {"schema": AVAILABILITY_SCHEMA}
    patterns = {"agents": AGENT_RE, "providers": IDENTIFIER_RE, "models": MODEL_RE}
    for field, pattern in patterns.items():
        values = availability.get(field)
        if not isinstance(values, dict) or any(not isinstance(key, str) or not pattern.fullmatch(key) or not isinstance(value, bool) for key, value in values.items()):
            raise RouteModelPolicyInputInvalid(f"availability.{field} invalid")
        compiled[field] = {key: values[key] for key in sorted(values)}
    digest = _sha(compiled)
    if "availability_digest" in availability and availability["availability_digest"] != digest:
        raise RouteModelPolicyInputInvalid("route/model availability digest invalid")
    return {**compiled, "availability_digest": digest}


def _required_effective_route(required_route: str) -> str | None:
    return {
        "auto": None,
        "direct-only": "direct",
        "delegated": "delegated",
        "frontier": "frontier",
        "frontier-gated": "frontier",
    }[required_route]


def _reasons(request: dict[str, Any], policy: dict[str, Any], availability: dict[str, Any], option: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    required_effective_route = _required_effective_route(request["required_route"])
    if option["route"] not in policy["allowed_routes"]:
        reasons.append("route_not_allowed:" + option["route"])
    if required_effective_route is not None and option["route"] != required_effective_route:
        reasons.append("required_route_mismatch:" + required_effective_route)
    if option["role"] != request["required_role"]:
        reasons.append("option_role_mismatch:" + request["required_role"])
    compatibility = next((row for row in policy["role_compatibility"] if row["role"] == request["required_role"]), None)
    if compatibility is None:
        reasons.append("role_not_configured:" + request["required_role"])
    else:
        if option["agent"] not in compatibility["agents"]:
            reasons.append("role_agent_incompatible:" + option["agent"])
        if option["route"] not in compatibility["routes"]:
            reasons.append("role_route_incompatible:" + option["route"])

    if option["provider"] is None or option["model"] is None:
        reasons.append("agent_model_unresolved")
    else:
        if not any(option["model"].startswith(prefix) for prefix in policy["allowed_provider_prefixes"]):
            reasons.append("provider_not_allowed:" + option["provider"])
        if option["model"] not in policy["allowed_models"]:
            reasons.append("model_not_allowed:" + option["model"])
    agent_state = availability["agents"].get(option["agent"])
    if agent_state is not True:
        reasons.append(("agent_not_installed:" if agent_state is False else "agent_availability_unknown:") + option["agent"])
    if option["provider"] is not None:
        provider_state = availability["providers"].get(option["provider"])
        if provider_state is not True:
            reasons.append(("provider_unavailable:" if provider_state is False else "provider_availability_unknown:") + option["provider"])
    if option["model"] is not None:
        model_state = availability["models"].get(option["model"])
        if model_state is not True:
            reasons.append(("model_unavailable:" if model_state is False else "model_availability_unknown:") + option["model"])
    if option["max_estimated_cost_usd"] > request["budget_headroom_usd"]:
        reasons.append(f"insufficient_budget:{option['max_estimated_cost_usd']}>{request['budget_headroom_usd']}")
    return reasons


def _blocker(rows: list[dict[str, Any]]) -> dict[str, Any]:
    reasons = sorted({reason for row in rows for reason in row["rejection_reasons"]})
    for prefix, code in REASON_TO_BLOCKER:
        if any(reason == prefix or reason.startswith(prefix) for reason in reasons):
            return {"code": code, "rejection_reasons": reasons}
    return {"code": "NO_ELIGIBLE_ROUTE_MODEL_OPTION", "rejection_reasons": reasons or ["no_policy_option"]}


def _evaluate(request: dict[str, Any], policy: dict[str, Any], availability: dict[str, Any]) -> dict[str, Any]:
    rows = []
    for option in policy["options"]:
        reasons = _reasons(request, policy, availability, option)
        rows.append({**option, "eligible": not reasons, "rejection_reasons": reasons})
    selected_row = next((row for row in rows if row["eligible"]), None)
    selected = None if selected_row is None else {key: selected_row[key] for key in ("option_id", "priority", "route", "agent", "role", "provider", "model", "max_estimated_cost_usd")}
    return {
        "schema": DECISION_SCHEMA,
        "request": request,
        "effective_policy": policy,
        "availability": availability,
        "considered_options": rows,
        "selected": selected,
        "status": "SELECTED" if selected is not None else "BLOCKED",
        "blocker": None if selected is not None else _blocker(rows),
        "credential_handling": dict(CREDENTIAL_HANDLING),
        "authority": dict(AUTHORITY),
    }


def decide(source_request: Any, source_policy: Any, source_availability: Any) -> dict[str, Any]:
    body = _evaluate(compile_request(source_request), compile_policy(source_policy), compile_availability(source_availability))
    digest = _sha(body)
    decision = {**body, "decision_id": "routemodel_" + digest, "decision_digest": digest}
    validate_decision(decision)
    return decision


def validate_decision(source: Any) -> None:
    decision = _mapping(source, "route/model decision")
    expected_fields = {"schema", "decision_id", "decision_digest", "request", "effective_policy", "availability", "considered_options", "selected", "status", "blocker", "credential_handling", "authority"}
    _fields(decision, expected_fields, "route/model decision")
    if decision.get("schema") != DECISION_SCHEMA:
        raise RouteModelPolicyInputInvalid("route/model decision schema invalid")
    try:
        digest = _sha({key: value for key, value in decision.items() if key not in {"decision_id", "decision_digest"}})
    except (TypeError, ValueError) as error:
        raise RouteModelPolicyInputInvalid("route/model decision is not canonical JSON") from error
    if decision.get("decision_digest") != digest or decision.get("decision_id") != "routemodel_" + digest:
        raise RouteModelPolicyInputInvalid("route/model decision digest identity invalid")
    request = compile_request(decision.get("request"))
    policy = compile_policy(decision.get("effective_policy"))
    availability = compile_availability(decision.get("availability"))
    expected = _evaluate(request, policy, availability)
    actual = {key: value for key, value in decision.items() if key not in {"decision_id", "decision_digest"}}
    if actual != expected:
        raise RouteModelPolicyInputInvalid("route/model decision does not match deterministic policy evaluation")


def _load(path: str, label: str) -> Any:
    candidate = pathlib.Path(path)
    try:
        if candidate.is_symlink() or not candidate.is_file():
            raise RouteModelPolicyInputInvalid(f"{label} path unavailable or unsafe")
        return json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        if isinstance(error, RouteModelPolicyInputInvalid):
            raise
        raise RouteModelPolicyInputInvalid(f"{label} is unreadable") from error


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a deterministic read-only route/model decision")
    parser.add_argument("--request", required=True, help="route/model request JSON")
    parser.add_argument("--policy", required=True, help="explicit route/model policy JSON")
    parser.add_argument("--availability", required=True, help="explicit agent/provider/model availability JSON")
    parser.add_argument("--require-selected", action="store_true", help="return non-zero when the valid decision is BLOCKED")
    args = parser.parse_args()
    try:
        decision = decide(_load(args.request, "request"), _load(args.policy, "policy"), _load(args.availability, "availability"))
    except RouteModelPolicyError as error:
        print(json.dumps({"error": type(error).__name__, "message": str(error)}, sort_keys=True))
        return 2
    print(json.dumps(decision, ensure_ascii=False, indent=2, sort_keys=True))
    return 3 if args.require_selected and decision["status"] != "SELECTED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
