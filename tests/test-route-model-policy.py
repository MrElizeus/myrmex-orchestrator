#!/usr/bin/env python3
"""P1-016 deterministic route/model decisions and explicit rejection evidence."""
from __future__ import annotations

import copy
import json
import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/myrmex_route_model_policy.py"
sys.path.insert(0, str(ROOT / "scripts"))
import myrmex_route_model_policy as route_model  # noqa: E402

DIGEST_A = "a" * 64


def request(*, route="delegated", role="worker", budget=2.0):
    return {
        "schema": "myrmex.route-model-request/v1",
        "work_unit_id": "WU-ROUTE-MODEL",
        "plan_revision_id": "plan_" + DIGEST_A,
        "plan_revision_digest": DIGEST_A,
        "required_route": route,
        "required_role": role,
        "budget_headroom_usd": budget,
    }


def option(option_id="worker-primary", *, priority=10, route="delegated", agent="myrmex-worker", role="worker", provider="openai", model="openai/model-primary", cost=1.0):
    return {
        "option_id": option_id,
        "priority": priority,
        "route": route,
        "agent": agent,
        "role": role,
        "provider": provider,
        "model": model,
        "max_estimated_cost_usd": cost,
    }


def policy(*options):
    return {
        "schema": "myrmex.route-model-policy/v1",
        "version": "1.0.0",
        "adaptive_scoring": False,
        "allowed_routes": ["delegated"],
        "allowed_provider_prefixes": ["openai/"],
        "allowed_models": ["openai/model-primary", "openai/model-secondary"],
        "role_compatibility": [{"role": "worker", "agents": ["myrmex-worker"], "routes": ["delegated"]}],
        "selection_order": ["priority_asc", "option_id_asc"],
        "options": list(options or [option()]),
    }


def availability(*, agent=True, provider=True, primary=True, secondary=True):
    return {
        "schema": "myrmex.route-model-availability/v1",
        "agents": {"myrmex-worker": agent},
        "providers": {"openai": provider},
        "models": {"openai/model-primary": primary, "openai/model-secondary": secondary},
    }


def blocked(req, pol, available, code):
    decision = route_model.decide(req, pol, available)
    route_model.validate_decision(decision)
    assert decision["status"] == "BLOCKED" and decision["selected"] is None, decision
    assert decision["blocker"]["code"] == code, decision["blocker"]
    return decision


# Golden selection is bound to the exact WU and plan revision, is replay stable,
# records effective policy/availability, and grants no invocation authority.
golden = route_model.decide(request(), policy(), availability())
assert golden == route_model.decide(request(), policy(), availability())
route_model.validate_decision(golden)
assert golden["request"]["work_unit_id"] == "WU-ROUTE-MODEL"
assert golden["request"]["plan_revision_id"] == "plan_" + DIGEST_A
assert golden["request"]["plan_revision_digest"] == DIGEST_A
assert golden["selected"]["model"] == "openai/model-primary"
assert golden["effective_policy"]["adaptive_scoring"] is False
assert golden["effective_policy"]["selection_order"] == ["priority_asc", "option_id_asc"]
assert golden["credential_handling"] == route_model.CREDENTIAL_HANDLING
assert not golden["authority"]["invoke_agent"] and not golden["authority"]["invoke_provider"]

import jsonschema
schema = json.loads((ROOT / "contracts/route-model-decision-v1.schema.json").read_text(encoding="utf-8"))
jsonschema.validate(golden, schema)

# An explicitly ordered second option may be selected only after the first is
# visibly rejected. This is recorded policy traversal, not a silent fallback.
alternatives = policy(
    option("worker-primary", priority=10),
    option("worker-secondary", priority=20, model="openai/model-secondary", cost=0.5),
)
alternative_decision = route_model.decide(request(), alternatives, availability(primary=False))
assert alternative_decision["selected"]["option_id"] == "worker-secondary"
assert alternative_decision["considered_options"][0]["rejection_reasons"] == ["model_unavailable:openai/model-primary"]

# Every required negative case yields a valid BLOCKED artifact with an explicit
# resolver-backed or policy blocker. No provider invocation failure is invented.
forbidden_policy = policy(option(provider="custom", model="custom/model-primary"))
forbidden = blocked(request(), forbidden_policy, {
    "schema": "myrmex.route-model-availability/v1",
    "agents": {"myrmex-worker": True}, "providers": {"custom": True}, "models": {"custom/model-primary": True},
}, "BLOCKED_NON_ALLOWED_PROVIDER")
assert "PROVIDER_INVOCATION_FAILED" not in json.dumps(forbidden)

unresolved = blocked(request(), policy(option(provider=None, model=None)), availability(), "AGENT_MODEL_UNRESOLVED")
assert "agent_model_unresolved" in unresolved["blocker"]["rejection_reasons"]

incompatible = blocked(request(), policy(option(agent="myrmex-verifier")), availability(), "BLOCKED_ROLE_INCOMPATIBLE")
assert "role_agent_incompatible:myrmex-verifier" in incompatible["blocker"]["rejection_reasons"]

provider_down = blocked(request(), policy(), availability(provider=False), "PROVIDER_UNAVAILABLE")
assert "provider_unavailable:openai" in provider_down["blocker"]["rejection_reasons"]

model_down = blocked(request(), policy(), availability(primary=False), "MODEL_UNAVAILABLE")
assert "model_unavailable:openai/model-primary" in model_down["blocker"]["rejection_reasons"]

budget = blocked(request(budget=0.25), policy(), availability(), "BLOCKED_INSUFFICIENT_BUDGET")
assert any(reason.startswith("insufficient_budget:") for reason in budget["blocker"]["rejection_reasons"])

missing = blocked(request(), policy(), availability(agent=False), "AGENT_NOT_INSTALLED")
assert "agent_not_installed:myrmex-worker" in missing["blocker"]["rejection_reasons"]

# Route constraints are exact: direct-only cannot silently become delegated.
blocked(request(route="direct-only"), policy(), availability(), "BLOCKED_ROUTE_INCOMPATIBLE")

# Candidate source order cannot influence policy priority/ID ordering.
same_priority = policy(
    option("worker-z", priority=10, model="openai/model-secondary"),
    option("worker-a", priority=10),
)
ordered = route_model.decide(request(), same_priority, availability())
assert [row["option_id"] for row in ordered["considered_options"]] == ["worker-a", "worker-z"]
assert ordered["selected"]["option_id"] == "worker-a"

# Adaptive/learned policy and rehashed semantic tampering both fail closed.
adaptive = policy(); adaptive["adaptive_scoring"] = True
try:
    route_model.decide(request(), adaptive, availability())
except route_model.RouteModelPolicyInputInvalid as error:
    assert "adaptive scoring" in str(error)
else:
    raise AssertionError("adaptive route/model scoring was accepted")

tampered = copy.deepcopy(golden)
tampered["selected"]["model"] = "openai/model-secondary"
body = {key: value for key, value in tampered.items() if key not in {"decision_id", "decision_digest"}}
tampered["decision_digest"] = route_model._sha(body)
tampered["decision_id"] = "routemodel_" + tampered["decision_digest"]
try:
    route_model.validate_decision(tampered)
except route_model.RouteModelPolicyInputInvalid:
    pass
else:
    raise AssertionError("rehashed route/model selection tampering was accepted")

# CLI reads only explicit JSON inputs. Credential-shaped environment variables
# are intentionally irrelevant and never requested or inspected.
with tempfile.TemporaryDirectory(prefix="myrmex-p1016-") as td:
    root = pathlib.Path(td)
    for name, value in (("request", request()), ("policy", policy()), ("availability", availability())):
        (root / f"{name}.json").write_text(json.dumps(value), encoding="utf-8")
    env = dict(os.environ)
    env.pop("OPENAI_API_KEY", None)
    env.pop("OPENCODE_GO_API_KEY", None)
    proc = subprocess.run([
        sys.executable, str(SCRIPT), "--request", str(root / "request.json"),
        "--policy", str(root / "policy.json"), "--availability", str(root / "availability.json"),
        "--require-selected",
    ], capture_output=True, text=True, env=env, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout) == golden

print("route/model policy: binding, allowlists, roles, explicit availability, budget, replay, no credentials, and no adaptive scoring PASS")
