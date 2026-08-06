#!/usr/bin/env python3
"""Standalone tests for the P1-001 plan-revision and planning exchange contracts.

Loads contracts/plan-revision-v1.schema.json, contracts/planning-request-v1.schema.json,
and contracts/planning-result-v1.schema.json, meta-validates each against JSON Schema
2020-12, resolves the relative planning-result reference to plan-revision-v1.schema.json,
and runs positive/negative fixtures plus a semantic validator for invariants that JSON
Schema alone cannot express (digest identity, derived record/plan identities,
WU/edge/dependency consistency, no self-edge, immutable lifecycle records, conflicting
input identities, and result-envelope/response-type conditionals). A negative reference
check asserts that an unresolvable relative schema reference is rejected.

Canonical digest helpers follow the contract descriptions: UTF-8, LF line endings,
deterministic key ordering for structured fields, no insignificant trailing whitespace.
plan_digest covers the plan payload excluding record envelope fields and the derived
plan_revision_id; record_digest covers the full record excluding record_digest and the
derived record_id. plan_revision_id == 'plan_' + plan_digest and
record_id == 'planrec_' + record_digest are enforced by the semantic validator.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import referencing

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts"

PLAN_REVISION_SCHEMA_PATH = CONTRACTS / "plan-revision-v1.schema.json"
PLANNING_REQUEST_SCHEMA_PATH = CONTRACTS / "planning-request-v1.schema.json"
PLANNING_RESULT_SCHEMA_PATH = CONTRACTS / "planning-result-v1.schema.json"

PLAN_REVISION_REF = "plan-revision-v1.schema.json"
PLAN_REVISION_ID = "urn:myrmex:schema:plan-revision:v1"
PLANNING_REQUEST_ID = "urn:myrmex:schema:planning-request:v1"
PLANNING_RESULT_ID = "urn:myrmex:schema:planning-result:v1"

# Fields excluded from the plan payload digest (record envelope fields plus the
# derived plan identity). plan_revision_id is derived FROM plan_digest, so it cannot
# be part of the payload the digest is computed over.
PLAN_PAYLOAD_EXCLUDED = {
    "record_id",
    "record_digest",
    "previous_record_id",
    "lifecycle_status",
    "created_at",
    "plan_digest",
    "plan_revision_id",
}

# Fields excluded from the record digest computation. record_id is derived FROM
# record_digest, so it cannot be part of the record the digest is computed over.
RECORD_DIGEST_EXCLUDED = {
    "record_id",
    "record_digest",
}

# Fields recomputed by the fixture builder after content overrides are applied;
# passing any of these as an override deliberately corrupts the derivation.
DERIVED_FIELDS = {
    "record_id",
    "plan_revision_id",
    "plan_digest",
    "record_digest",
}

# Deterministic fixture constants.
HEX64_A = "ab" * 32
HEX64_B = "cd" * 32
HEX64_C = "ef" * 32
HEX64_D = "12" * 32
HEX64_E = "34" * 32
BASE_SHA = "f42631da2396d93fe11ce04a833d4e3c2de71df7"
CREATED_AT = "2026-08-06T18:22:49+00:00"

ALLOWED_TRANSITIONS = {
    "proposed": {"reviewed", "rejected", "withdrawn", "superseded"},
    "reviewed": {"validated", "rejected", "withdrawn", "superseded"},
    "validated": {"active", "rejected", "withdrawn", "superseded"},
    "active": {"superseded", "withdrawn", "rejected"},
    "rejected": set(),
    "superseded": set(),
    "withdrawn": set(),
}


def canonical_json_bytes(obj: object) -> bytes:
    """Deterministic canonical serialization: sorted keys, compact separators, UTF-8, no trailing whitespace/newline."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_digest(obj: object) -> str:
    return sha256_hex(canonical_json_bytes(obj))


def load_schema(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def build_registry(plan_revision_schema: dict):
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012

    resource = Resource.from_contents(plan_revision_schema, default_specification=DRAFT202012)
    # The planning-result schema uses a relative $ref against an opaque urn base, so
    # referencing resolves it to the literal relative URI; register both the relative
    # URI and the canonical $id so the reference resolves either way.
    return (
        Registry()
        .with_resource(PLAN_REVISION_REF, resource)
        .with_resource(plan_revision_schema["$id"], resource)
    )


# ---------------------------------------------------------------------------
# Semantic validators (invariants JSON Schema cannot express)
# ---------------------------------------------------------------------------

def validate_plan_revision_semantics(record: dict) -> list[str]:
    errors: list[str] = []
    payload = {k: v for k, v in record.items() if k not in PLAN_PAYLOAD_EXCLUDED}
    if record.get("plan_digest") != canonical_digest(payload):
        errors.append("plan_digest does not match canonical digest of the plan payload")
    # Digest-derived identities: plan_revision_id and record_id must be derived from
    # their digests. A record that reuses an id for different content (or fabricates
    # an id) breaks immutability and is rejected here.
    if record.get("plan_revision_id") != "plan_" + str(record.get("plan_digest", "")):
        errors.append("plan_revision_id is not derived from plan_digest ('plan_' + plan_digest)")
    record_minus_digest = {k: v for k, v in record.items() if k not in RECORD_DIGEST_EXCLUDED}
    if record.get("record_digest") != canonical_digest(record_minus_digest):
        errors.append("record_digest does not match canonical digest of the record (immutability broken)")
    if record.get("record_id") != "planrec_" + str(record.get("record_digest", "")):
        errors.append("record_id is not derived from record_digest ('planrec_' + record_digest)")

    # Conflicting input identities: the same identity must not bind to two digests.
    input_by_identity: dict[str, set[str]] = {}
    for inp in record.get("input_digests", []):
        if isinstance(inp, dict):
            input_by_identity.setdefault(str(inp.get("identity")), set()).add(str(inp.get("sha256")))
    for identity, digests in input_by_identity.items():
        if len(digests) > 1:
            errors.append(f"input identity {identity!r} has conflicting digests: {sorted(digests)}")

    wu_list = record.get("work_units", [])
    wu_ids = [wu.get("id") for wu in wu_list]
    if len(wu_ids) != len(set(wu_ids)):
        dupes = sorted({x for x in wu_ids if wu_ids.count(x) > 1})
        errors.append(f"duplicate work unit id(s): {', '.join(dupes)}")
    wu_by_id = {wu.get("id"): wu for wu in wu_list}
    for wu in wu_list:
        wu_id = wu.get("id")
        for dep in wu.get("dependencies", []):
            if dep == wu_id:
                errors.append(f"work unit {wu_id} depends on itself")
            elif dep not in wu_ids:
                errors.append(f"work unit {wu_id} depends on undefined work unit {dep}")
    edge_pairs = []
    for edge in record.get("edges", []):
        if not isinstance(edge, list) or len(edge) != 2:
            errors.append(f"edge is not a [from,to] pair: {edge!r}")
            continue
        frm, to = edge
        edge_pairs.append((frm, to))
        if frm == to:
            errors.append(f"self-edge {frm} -> {to}")
        if frm not in wu_ids:
            errors.append(f"edge from undefined work unit {frm}")
        if to not in wu_ids:
            errors.append(f"edge to undefined work unit {to}")
        elif frm not in wu_by_id[to].get("dependencies", []):
            errors.append(f"edge {frm} -> {to} not reflected in {to}.dependencies")
    # Every declared dependency must be represented by a matching edge so the two
    # relations cannot drift apart.
    edge_set = set(edge_pairs)
    for wu in wu_list:
        for dep in wu.get("dependencies", []):
            if (dep, wu.get("id")) not in edge_set:
                errors.append(f"dependency {dep} of {wu.get('id')} not represented by an edge [{dep}, {wu.get('id')}]")
    return errors


def validate_lifecycle_transition(previous_status: str | None, current_status: str) -> bool:
    """Revisions are immutable; status only moves forward along the legal transition table."""
    if previous_status is None:
        return current_status == "proposed"
    return current_status in ALLOWED_TRANSITIONS.get(previous_status, set())


def validate_result_envelope(result: dict) -> list[str]:
    errors: list[str] = []
    result_minus_digest = {k: v for k, v in result.items() if k != "result_digest"}
    if result.get("result_digest") != canonical_digest(result_minus_digest):
        errors.append("result_digest does not match canonical digest of the result envelope")
    rtype = result.get("response_type")
    plan_rev = result.get("plan_revision")
    if rtype == "plan":
        if not isinstance(plan_rev, dict):
            errors.append("plan responses must embed a plan_revision object")
        else:
            for result_field, plan_field in (
                ("request_id", "planning_request_id"),
                ("campaign_id", "campaign_id"),
                ("objective_id", "objective_id"),
                ("base_sha", "base_sha"),
            ):
                if result.get(result_field) != plan_rev.get(plan_field):
                    errors.append(f"result {result_field} != embedded plan_revision {plan_field}")
            if plan_rev.get("lifecycle_status") != "proposed":
                errors.append("embedded plan_revision.lifecycle_status must be proposed for plan responses")
        if result.get("clarification") is not None:
            errors.append("plan responses must have a null clarification")
        if result.get("completion_evidence"):
            errors.append("plan responses must have empty completion_evidence")
    elif rtype == "blocking_clarification":
        if not isinstance(result.get("clarification"), dict):
            errors.append("blocking_clarification responses must include a clarification object")
        if plan_rev is not None:
            errors.append("blocking_clarification responses must not embed a plan_revision")
        if result.get("completion_evidence"):
            errors.append("blocking_clarification responses must have empty completion_evidence")
    elif rtype == "already_complete":
        if plan_rev is not None:
            errors.append("already_complete responses must not embed a plan_revision")
        if result.get("clarification") is not None:
            errors.append("already_complete responses must have a null clarification")
        if not result.get("completion_evidence"):
            errors.append("already_complete responses must have non-empty completion_evidence")
    else:
        errors.append(f"unknown response_type {rtype!r}")
    return errors


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def wu(id_: str, deps: list[str] | None = None) -> dict:
    return {
        "id": id_,
        "objective": f"Objective for {id_}",
        "non_goals": ["No opportunistic refactors"],
        "dependencies": list(deps or []),
        "scope": {
            "allowed_paths": ["contracts/", "tests/test-plan-contracts.py"],
            "forbidden_paths": [".env", "agents/", "skills/"],
        },
        "acceptance_criteria": ["Contract behavior is observable"],
        "verification": {
            "commands": ["python3 tests/test-plan-contracts.py"],
            "manual_checks": [],
            "discover_when_missing": True,
        },
        "risk_class": "bounded",
        "required_route": "auto",
        "human_gates": [],
        "required_evidence": ["schema digests"],
        "terminal_gate": "G0-PLAN-CONTRACT",
    }


def make_plan_revision(**overrides: object) -> dict:
    record: dict = {
        "schema": "myrmex.plan-revision/v1",
        "record_id": "planrec_" + HEX64_A,
        "plan_revision_id": "plan_" + HEX64_B,
        "campaign_id": "camp-p1-campaign-intelligence",
        "objective_id": "P1-001",
        "planning_request_id": "p1-001-plan-req-0001",
        "base_sha": BASE_SHA,
        "parent_revision": None,
        "input_digests": [
            {"kind": "repository-context", "identity": BASE_SHA, "sha256": HEX64_C}
        ],
        "assumptions": [
            {
                "assumption_id": "ASM-001",
                "statement": "Plan revisions are immutable.",
                "evidence_status": "supported",
                "impact": "low",
                "resolution_gate": None,
            }
        ],
        "work_units": [wu("WU-P1-001"), wu("WU-P1-002", deps=["WU-P1-001"])],
        "edges": [["WU-P1-001", "WU-P1-002"]],
        "lifecycle_status": "proposed",
        "previous_record_id": None,
        "created_at": CREATED_AT,
    }
    # Content overrides apply first so the derived fields describe the final content;
    # derived-field overrides (ids and digests) are applied last so fixtures can
    # deliberately corrupt a derivation.
    derived_overrides = {k: v for k, v in overrides.items() if k in DERIVED_FIELDS}
    record.update({k: v for k, v in overrides.items() if k not in DERIVED_FIELDS})
    record["plan_digest"] = canonical_digest(
        {k: v for k, v in record.items() if k not in PLAN_PAYLOAD_EXCLUDED}
    )
    record["plan_revision_id"] = "plan_" + record["plan_digest"]
    record["record_digest"] = canonical_digest(
        {k: v for k, v in record.items() if k not in RECORD_DIGEST_EXCLUDED}
    )
    record["record_id"] = "planrec_" + record["record_digest"]
    record.update(derived_overrides)
    return record


def make_superseded_revision() -> dict:
    previous = make_plan_revision()
    return make_plan_revision(
        lifecycle_status="superseded",
        previous_record_id=previous["record_id"],
        parent_revision={
            "artifact_id": previous["plan_revision_id"],
            "artifact_digest": previous["plan_digest"],
        },
    )


def make_reviewed_revision(proposed: dict) -> dict:
    """Second immutable lifecycle record for the same plan payload.

    Keeps identical plan content (so plan_digest and plan_revision_id are unchanged)
    while advancing lifecycle_status and linking previous_record_id to the proposed
    record. Only the record envelope changes, so record_digest and the derived
    record_id are new.
    """
    return make_plan_revision(
        lifecycle_status="reviewed",
        previous_record_id=proposed["record_id"],
    )


def make_planning_request(**overrides: object) -> dict:
    request: dict = {
        "schema": "myrmex.planning-request/v1",
        "request_id": "p1-001-plan-req-0001",
        "run_id": "myrmex-20260806T182249Z-p1-001-persist-the-p1-p2-plan-revisi-353d96",
        "campaign_id": "camp-p1-campaign-intelligence",
        "objective_id": "P1-001",
        "base_sha": BASE_SHA,
        "parent_revision": None,
        "input_digests": [
            {"kind": "repository-context", "identity": BASE_SHA, "sha256": HEX64_C}
        ],
        "constraints": {
            "allowed_paths": ["contracts/", "scripts/check-package.py", "tests/test-plan-contracts.py", "docs/ARCHITECTURE.md"],
            "forbidden_paths": [".env", "agents/", "skills/"],
            "required_invariants": ["state-first operation", "no fabricated authority"],
            "required_sections": ["Summary", "Target Architecture", "Dependency Graph", "Implementation Tasks"],
        },
        "required_output_schema": "myrmex.planning-result/v1",
        "effect_policy": {
            "mode": "planning_only",
            "repository_write": False,
            "activate_plan": False,
            "commit": False,
            "push": False,
            "merge": False,
            "release": False,
            "deploy": False,
        },
        "created_at": CREATED_AT,
    }
    request.update(overrides)
    return request


def make_planning_result(
    response_type: str = "plan",
    plan_revision: dict | None = None,
    clarification: dict | None = None,
    completion_evidence: list[str] | None = None,
    **overrides: object,
) -> dict:
    result: dict = {
        "schema": "myrmex.planning-result/v1",
        "request_id": "p1-001-plan-req-0001",
        "run_id": "myrmex-20260806T182249Z-p1-001-persist-the-p1-p2-plan-revisi-353d96",
        "campaign_id": "camp-p1-campaign-intelligence",
        "objective_id": "P1-001",
        "base_sha": BASE_SHA,
        "response_type": response_type,
        "plan_revision": plan_revision,
        "clarification": clarification,
        "completion_evidence": completion_evidence if completion_evidence is not None else [],
        "authority": {
            "scope": "planning_only",
            "plan_activation_authorized": False,
            "repository_write_authorized": False,
            "commit_authorized": False,
            "push_authorized": False,
            "merge_authorized": False,
            "release_authorized": False,
            "deployment_authorized": False,
        },
        "created_at": CREATED_AT,
    }
    result["result_digest"] = canonical_digest(
        {k: v for k, v in result.items() if k != "result_digest"}
    )
    result.update(overrides)
    return result


# ---------------------------------------------------------------------------
# Fixture lists
# ---------------------------------------------------------------------------

def build_fixtures():
    revision = make_plan_revision()
    request = make_planning_request()

    positive = [
        ("plan-revision", PLAN_REVISION_SCHEMA_PATH, revision, []),
        ("plan-revision-superseded", PLAN_REVISION_SCHEMA_PATH, make_superseded_revision(), []),
        ("planning-request", PLANNING_REQUEST_SCHEMA_PATH, request, []),
        (
            "planning-result-plan",
            PLANNING_RESULT_SCHEMA_PATH,
            make_planning_result(plan_revision=revision),
            [],
        ),
        (
            "planning-result-clarification",
            PLANNING_RESULT_SCHEMA_PATH,
            make_planning_result(
                response_type="blocking_clarification",
                plan_revision=None,
                clarification={"question": "Confirm scope?", "options": ["A", "B"], "recommended_default": "A"},
            ),
            [],
        ),
        (
            "planning-result-already-complete",
            PLANNING_RESULT_SCHEMA_PATH,
            make_planning_result(
                response_type="already_complete",
                plan_revision=None,
                clarification=None,
                completion_evidence=["objective already satisfied: evidence-digest present"],
            ),
            [],
        ),
        (
            "plan-revision-reviewed",
            PLAN_REVISION_SCHEMA_PATH,
            make_reviewed_revision(revision),
            [],
        ),
        (
            "plan-revision-unverified-assumption-gate",
            PLAN_REVISION_SCHEMA_PATH,
            make_plan_revision(
                assumptions=[
                    {
                        "assumption_id": "ASM-002",
                        "statement": "Frontier model availability is stable through run 353d96.",
                        "evidence_status": "unverified",
                        "impact": "high",
                        "resolution_gate": "G0-PLAN-CONTRACT",
                    }
                ]
            ),
            [],
        ),
        (
            "plan-revision-parent-exact",
            PLAN_REVISION_SCHEMA_PATH,
            make_plan_revision(
                parent_revision={
                    "artifact_id": "myrmex.plan-revision/p1p2-arch-001/f42631da/plan-req-0002/v1",
                    "artifact_digest": "a81a4ff2550f7922ad94ac19fbf07d93d2cb5381415f7531d2e4a22479bf9b2d",
                }
            ),
            [],
        ),
    ]

    # Schema-level negatives: instance must FAIL validation against its schema.
    def pr(**overrides):
        return make_plan_revision(**overrides)

    negative_schema = [
        ("pr-schema-const", PLAN_REVISION_SCHEMA_PATH, pr(schema="myrmex.plan-revision/v2")),
        ("pr-plan_revision_id-pattern", PLAN_REVISION_SCHEMA_PATH, pr(plan_revision_id="plan-" + HEX64_B)),
        ("pr-record_id-pattern", PLAN_REVISION_SCHEMA_PATH, pr(record_id="planrec-" + HEX64_A)),
        ("pr-campaign_id-pattern", PLAN_REVISION_SCHEMA_PATH, pr(campaign_id="camp-UPPER-case")),
        ("pr-base_sha-pattern", PLAN_REVISION_SCHEMA_PATH, pr(base_sha="zz" + "0" * 38)),
        ("pr-plan_digest-pattern", PLAN_REVISION_SCHEMA_PATH, pr(plan_digest=HEX64_B[:-1])),
        ("pr-record_digest-pattern", PLAN_REVISION_SCHEMA_PATH, pr(record_digest=HEX64_B[:-2])),
        ("pr-input-digest-sha256-pattern", PLAN_REVISION_SCHEMA_PATH, pr(input_digests=[{"kind": "k", "identity": "i", "sha256": "not-hex"}])),
        ("pr-previous_record_id-pattern", PLAN_REVISION_SCHEMA_PATH, pr(previous_record_id="planrec_" + HEX64_A[:-1])),
        ("pr-lifecycle-invalid", PLAN_REVISION_SCHEMA_PATH, pr(lifecycle_status="draft")),
        ("pr-additional-property", PLAN_REVISION_SCHEMA_PATH, pr(extra_field=True)),
        ("pr-missing-record_digest", PLAN_REVISION_SCHEMA_PATH, pr(record_digest=None)),
        (
            "pr-missing-created_at",
            PLAN_REVISION_SCHEMA_PATH,
            {k: v for k, v in pr().items() if k != "created_at"},
        ),
        (
            "pr-missing-input_digests",
            PLAN_REVISION_SCHEMA_PATH,
            {k: v for k, v in pr().items() if k != "input_digests"},
        ),
        ("pr-empty-work_units", PLAN_REVISION_SCHEMA_PATH, pr(work_units=[])),
        ("pr-wu-id-pattern", PLAN_REVISION_SCHEMA_PATH, pr(work_units=[wu("wu-001")])),
        ("pr-wu-risk-class-invalid", PLAN_REVISION_SCHEMA_PATH, pr(work_units=[dict(wu("WU-P1-001"), risk_class="medium")])),
        ("pr-wu-required-route-invalid", PLAN_REVISION_SCHEMA_PATH, pr(work_units=[dict(wu("WU-P1-001"), required_route="telepath")])),
        ("pr-wu-human-gate-required_before-invalid", PLAN_REVISION_SCHEMA_PATH, pr(work_units=[dict(wu("WU-P1-001"), human_gates=[{"gate_id": "G1", "decision_type": "approve", "reason": "r", "required_before": "delivery_now"}])])),
        ("pr-wu-verification-missing-field", PLAN_REVISION_SCHEMA_PATH, pr(work_units=[dict(wu("WU-P1-001"), verification={"commands": ["x"], "manual_checks": []})])),
        ("pr-wu-scope-missing-forbidden", PLAN_REVISION_SCHEMA_PATH, pr(work_units=[dict(wu("WU-P1-001"), scope={"allowed_paths": ["contracts/"]})])),
        ("pr-edge-not-pair", PLAN_REVISION_SCHEMA_PATH, pr(edges=[["WU-P1-001"]])),
        ("pr-edge-element-pattern", PLAN_REVISION_SCHEMA_PATH, pr(edges=[["WU-P1-001!", "WU-P1-002"]])),
        ("pr-input-digests-empty", PLAN_REVISION_SCHEMA_PATH, pr(input_digests=[])),
        ("pr-input-digests-duplicate", PLAN_REVISION_SCHEMA_PATH, pr(input_digests=[{"kind": "k", "identity": "i", "sha256": HEX64_C}, {"kind": "k", "identity": "i", "sha256": HEX64_C}])),
        ("pr-assumption-evidence-status-invalid", PLAN_REVISION_SCHEMA_PATH, pr(assumptions=[{"assumption_id": "A", "statement": "s", "evidence_status": "maybe", "impact": "i", "resolution_gate": None}])),
        ("pr-assumption-missing-resolution_gate", PLAN_REVISION_SCHEMA_PATH, pr(assumptions=[{"assumption_id": "A", "statement": "s", "evidence_status": "supported", "impact": "i"}])),
        ("pr-parent-revision-missing-digest", PLAN_REVISION_SCHEMA_PATH, pr(parent_revision={"artifact_id": "plan_" + HEX64_B})),
        ("pr-parent-revision-not-null-or-object", PLAN_REVISION_SCHEMA_PATH, pr(parent_revision="plan_" + HEX64_B)),
        (
            "req-missing-effect_policy",
            PLANNING_REQUEST_SCHEMA_PATH,
            {k: v for k, v in request.items() if k != "effect_policy"},
        ),
        ("req-effect-policy-mode-invalid", PLANNING_REQUEST_SCHEMA_PATH, make_planning_request(effect_policy={**request["effect_policy"], "mode": "planning_and_effects"})),
        ("req-effect-policy-commit-true", PLANNING_REQUEST_SCHEMA_PATH, make_planning_request(effect_policy={**request["effect_policy"], "commit": True})),
        ("req-required-output-schema-wrong", PLANNING_REQUEST_SCHEMA_PATH, make_planning_request(required_output_schema="myrmex.planning-request/v1")),
        ("req-additional-property", PLANNING_REQUEST_SCHEMA_PATH, make_planning_request(bonus_field="x")),
        ("req-input-digests-empty", PLANNING_REQUEST_SCHEMA_PATH, make_planning_request(input_digests=[])),
        ("req-constraints-missing-required_sections", PLANNING_REQUEST_SCHEMA_PATH, make_planning_request(constraints={k: v for k, v in request["constraints"].items() if k != "required_sections"})),
        ("res-response-type-invalid", PLANNING_RESULT_SCHEMA_PATH, make_planning_result(response_type="maybe")),
        ("res-authority-activation-true", PLANNING_RESULT_SCHEMA_PATH, make_planning_result(authority={**make_planning_result()["authority"], "plan_activation_authorized": True})),
        ("res-result-digest-pattern", PLANNING_RESULT_SCHEMA_PATH, make_planning_result(result_digest="not-a-digest")),
        ("res-clarification-wrong-shape", PLANNING_RESULT_SCHEMA_PATH, make_planning_result(response_type="blocking_clarification", plan_revision=None, clarification={"question": "q"})),
        ("res-additional-property", PLANNING_RESULT_SCHEMA_PATH, make_planning_result(extra=True)),
        ("res-completion-evidence-item-type", PLANNING_RESULT_SCHEMA_PATH, make_planning_result(response_type="already_complete", plan_revision=None, completion_evidence=[{"kind": "x"}])),
        (
            "res-embedded-plan-revision-invalid",
            PLANNING_RESULT_SCHEMA_PATH,
            make_planning_result(plan_revision=make_plan_revision(record_digest=None)),
        ),
        ("pr-edges-duplicate-pair", PLAN_REVISION_SCHEMA_PATH, pr(edges=[["WU-P1-001", "WU-P1-002"], ["WU-P1-001", "WU-P1-002"]])),
        ("pr-planning-request-id-maxlength", PLAN_REVISION_SCHEMA_PATH, pr(planning_request_id="x" * 257)),
        ("pr-objective-id-maxlength", PLAN_REVISION_SCHEMA_PATH, pr(objective_id="y" * 257)),
        ("req-request-id-maxlength", PLANNING_REQUEST_SCHEMA_PATH, make_planning_request(request_id="z" * 257)),
        ("req-run-id-maxlength", PLANNING_REQUEST_SCHEMA_PATH, make_planning_request(run_id="w" * 257)),
        ("req-objective-id-maxlength", PLANNING_REQUEST_SCHEMA_PATH, make_planning_request(objective_id="v" * 257)),
        ("pr-wu-unknown-nested-field", PLAN_REVISION_SCHEMA_PATH, pr(work_units=[dict(wu("WU-P1-001"), rogue_field="x")])),
    ]

    # Semantic negatives: instance VALIDATES against the schema but must be rejected by
    # the semantic validator (invariants JSON Schema cannot express).
    semantic = [
        ("sem-plan-digest-mismatch", make_plan_revision(plan_digest=HEX64_D)),
        ("sem-record-digest-mismatch", make_plan_revision(record_digest=HEX64_D)),
        ("sem-edge-undefined-wu", make_plan_revision(edges=[["WU-P1-001", "WU-NOPE"]])),
        ("sem-edge-self", make_plan_revision(edges=[["WU-P1-001", "WU-P1-001"]])),
        ("sem-dependency-undefined-wu", make_plan_revision(work_units=[wu("WU-P1-001"), wu("WU-P1-002", deps=["WU-NOPE"])])),
        ("sem-dependency-self", make_plan_revision(work_units=[wu("WU-P1-001", deps=["WU-P1-001"]), wu("WU-P1-002")])),
        (
            "sem-plan-response-lifecycle-not-proposed",
            make_planning_result(plan_revision=make_plan_revision(lifecycle_status="active")),
        ),
        (
            "sem-plan-response-identity-mismatch",
            make_planning_result(plan_revision=make_plan_revision(campaign_id="camp-other-campaign")),
        ),
        (
            "sem-plan-response-completion-evidence",
            make_planning_result(plan_revision=make_plan_revision(), completion_evidence=["unexpected"]),
        ),
        (
            "sem-clarification-missing",
            make_planning_result(response_type="blocking_clarification", plan_revision=None, clarification=None),
        ),
        (
            "sem-already-complete-no-evidence",
            make_planning_result(response_type="already_complete", plan_revision=None, completion_evidence=[]),
        ),
        ("sem-result-digest-mismatch", make_planning_result(result_digest=HEX64_D)),
        (
            "sem-plan-revision-id-not-derived-from-plan-digest",
            make_plan_revision(plan_revision_id="plan_" + HEX64_C),
        ),
        (
            "sem-record-id-not-derived-from-record-digest",
            make_plan_revision(record_id="planrec_" + HEX64_C),
        ),
        (
            "sem-changed-immutable-content-same-plan-revision-id",
            make_plan_revision(
                work_units=[
                    wu("WU-P1-001"),
                    wu("WU-P1-002", deps=["WU-P1-001"]),
                    wu("WU-P1-003", deps=["WU-P1-002"]),
                ],
                edges=[["WU-P1-001", "WU-P1-002"], ["WU-P1-002", "WU-P1-003"]],
                plan_revision_id=make_plan_revision()["plan_revision_id"],
            ),
        ),
        (
            "sem-plan-response-with-clarification",
            make_planning_result(
                plan_revision=make_plan_revision(),
                clarification={"question": "Is this approved?", "options": ["yes", "no"], "recommended_default": "yes"},
            ),
        ),
        (
            "sem-blocking-clarification-with-embedded-plan",
            make_planning_result(
                response_type="blocking_clarification",
                plan_revision=make_plan_revision(),
                clarification={"question": "Which scope?", "options": ["A", "B"], "recommended_default": "A"},
            ),
        ),
        (
            "sem-already-complete-with-embedded-plan",
            make_planning_result(
                response_type="already_complete",
                plan_revision=make_plan_revision(),
                clarification=None,
                completion_evidence=["objective already satisfied"],
            ),
        ),
        (
            "sem-duplicate-wu-id",
            make_plan_revision(work_units=[wu("WU-P1-001"), wu("WU-P1-001")], edges=[]),
        ),
        (
            "sem-edge-dependency-mismatch",
            make_plan_revision(
                work_units=[wu("WU-P1-001"), wu("WU-P1-002")],
                edges=[["WU-P1-001", "WU-P1-002"]],
            ),
        ),
        (
            "sem-conflicting-duplicate-input-identity",
            make_plan_revision(
                input_digests=[
                    {"kind": "repository-context", "identity": "same-input", "sha256": HEX64_C},
                    {"kind": "campaign-state", "identity": "same-input", "sha256": HEX64_D},
                ]
            ),
        ),
        (
            "sem-plan-response-request-id-mismatch",
            make_planning_result(plan_revision=make_plan_revision(planning_request_id="p1-001-plan-req-0002")),
        ),
    ]

    # Lifecycle transition table checks (immutability semantics).
    lifecycle_cases = [
        (None, "proposed", True),
        ("proposed", "reviewed", True),
        ("proposed", "active", False),
        ("reviewed", "validated", True),
        ("validated", "active", True),
        ("active", "superseded", True),
        ("active", "proposed", False),
        ("rejected", "active", False),
        ("superseded", "reviewed", False),
        ("withdrawn", "proposed", False),
    ]
    return positive, negative_schema, semantic, lifecycle_cases


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

def main() -> int:
    import jsonschema

    plan_revision_schema = load_schema(PLAN_REVISION_SCHEMA_PATH)
    planning_request_schema = load_schema(PLANNING_REQUEST_SCHEMA_PATH)
    planning_result_schema = load_schema(PLANNING_RESULT_SCHEMA_PATH)

    # Contract identity and const invariants.
    assert plan_revision_schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert plan_revision_schema["$id"] == PLAN_REVISION_ID
    assert plan_revision_schema["properties"]["schema"]["const"] == "myrmex.plan-revision/v1"
    assert planning_request_schema["$id"] == PLANNING_REQUEST_ID
    assert planning_request_schema["properties"]["schema"]["const"] == "myrmex.planning-request/v1"
    assert planning_result_schema["$id"] == PLANNING_RESULT_ID
    assert planning_result_schema["properties"]["schema"]["const"] == "myrmex.planning-result/v1"
    # The planning-result schema must reference the plan-revision contract by relative URI.
    assert planning_result_schema["properties"]["plan_revision"]["anyOf"][1]["$ref"] == PLAN_REVISION_REF

    # Meta-validation against JSON Schema 2020-12.
    jsonschema.Draft202012Validator.check_schema(plan_revision_schema)
    jsonschema.Draft202012Validator.check_schema(planning_request_schema)
    jsonschema.Draft202012Validator.check_schema(planning_result_schema)

    registry = build_registry(plan_revision_schema)
    schemas = {
        PLAN_REVISION_SCHEMA_PATH: plan_revision_schema,
        PLANNING_REQUEST_SCHEMA_PATH: planning_request_schema,
        PLANNING_RESULT_SCHEMA_PATH: planning_result_schema,
    }

    positive, negative_schema, semantic, lifecycle_cases = build_fixtures()

    def schema_errors(path: Path, instance: object) -> list:
        return list(jsonschema.Draft202012Validator(schemas[path], registry=registry).iter_errors(instance))

    positive_pass = 0
    for name, path, instance, _ in positive:
        errs = schema_errors(path, instance)
        assert not errs, f"positive fixture {name} failed: {[e.message for e in errs][:3]}"
        positive_pass += 1

    negative_pass = 0
    for name, path, instance in negative_schema:
        errs = schema_errors(path, instance)
        assert errs, f"negative fixture {name} unexpectedly validated"
        negative_pass += 1

    semantic_pass = 0
    for name, instance in semantic:
        path = (
            PLAN_REVISION_SCHEMA_PATH
            if instance.get("schema") == "myrmex.plan-revision/v1"
            else PLANNING_RESULT_SCHEMA_PATH
        )
        errs = schema_errors(path, instance)
        assert not errs, f"semantic fixture {name} must be schema-valid: {[e.message for e in errs][:3]}"
        if instance.get("schema") == "myrmex.plan-revision/v1":
            sem_errs = validate_plan_revision_semantics(instance)
        else:
            sem_errs = validate_result_envelope(instance)
        assert sem_errs, f"semantic fixture {name} not rejected by semantic validator"
        semantic_pass += 1

    lifecycle_pass = 0
    for previous, current, expected in lifecycle_cases:
        assert validate_lifecycle_transition(previous, current) is expected, (
            f"lifecycle transition {previous!r} -> {current!r} expected {expected}"
        )
        lifecycle_pass += 1

    # Negative reference check: an unresolvable relative $ref must fail resolution.
    reference_pass = 0
    broken_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:myrmex:test:broken-ref",
        "type": "object",
        "properties": {"plan_revision": {"$ref": "definitely-missing-schema.json"}},
    }
    try:
        jsonschema.Draft202012Validator.check_schema(broken_schema)
        broken_validator = jsonschema.Draft202012Validator(broken_schema, registry=registry)
        broken_validator.validate({"plan_revision": {}})
        raise AssertionError("unresolvable relative $ref unexpectedly validated")
    except referencing.exceptions.Unresolvable:
        reference_pass += 1
    except AssertionError:
        raise
    except Exception as exc:
        raise AssertionError(
            f"unresolvable relative $ref raised unexpected {type(exc).__name__}: {exc!r}"
        ) from exc

    # Positive semantic cross-check: canonical fixtures must pass the semantic validators too.
    for name, path, instance, _ in positive:
        if path == PLAN_REVISION_SCHEMA_PATH:
            assert not validate_plan_revision_semantics(instance), f"positive semantic failed for {name}"
        elif path == PLANNING_RESULT_SCHEMA_PATH:
            assert not validate_result_envelope(instance), f"positive semantic failed for {name}"

    print(f"positive_fixture_results: {positive_pass} passed, 0 failed")
    print(f"negative_fixture_results: {negative_pass} passed, 0 failed")
    print(f"semantic_reference_test_results: {semantic_pass + lifecycle_pass + reference_pass} passed, 0 failed "
          f"({semantic_pass} semantic fixtures, {lifecycle_pass} lifecycle transitions, "
          f"{reference_pass} negative reference checks)")
    print("plan-contract tests: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
