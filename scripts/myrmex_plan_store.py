#!/usr/bin/env python3
"""Immutable plan revision store and linear lifecycle for Myrmex P1-007.

Persists complete ``myrmex.plan-revision/v1`` records into the P1-002
Campaign Intelligence sidecar under ``kind=plan`` at
``plan-revision/record/<record_id>`` and enforces a linear, no-fork lifecycle
chain reconstructed deterministically from immutable ``previous_record_id``
links.

Design:
  * plan_digest / plan_revision_id and record_digest / record_id follow the
    P1-001 contract exclusion rules exactly; plan-content arrays retain their
    supplied order (ordering is part of plan identity);
  * lifecycle recognizes all seven P1-001 states with the exact transition
    table; every successor links the current head; forks and disconnected
    records are rejected;
  * a previously absent ``active`` record is refused with
    PlanActivationAuthorityRequired (P1-012 owns governed activation); exact
    replay/read of an already-durable active record is allowed passively;
  * structural validation runs BEFORE any digest recomputation so malformed
    input raises typed failures, never incidental KeyError/TypeError;
  * a dedicated per-campaign ``intelligence/plan-store.lock`` serializes the
    read/check/write transaction without nesting P1-002's internal lock;
  * no WU, DAG, plan activation, commit, push, merge, deployment, campaign
    mutation, or repository effect capability is introduced.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import pathlib
import re
import sys
from contextlib import contextmanager
from typing import Any, Iterator


class PlanStoreError(Exception):
    """Base error for the plan revision store."""


class PlanRecordInvalid(PlanStoreError):
    """A plan record violates the P1-001 contract or integrity rules."""


class PlanRecordNotFound(PlanStoreError):
    """A referenced plan record is not durable."""


class PlanLifecycleInvalid(PlanStoreError):
    """Lifecycle status or transition is not legal."""


class PlanLifecycleConflict(PlanStoreError):
    """A lifecycle fork or stale-head append was attempted."""


class PlanStoreConflict(PlanStoreError):
    """An immutable artifact conflict occurred."""


class PlanStoreBackendUnavailable(PlanStoreError):
    """P1-002 backend or locking is unavailable."""


class PlanActivationAuthorityRequired(PlanStoreError):
    """Creation of a previously absent active record requires governed authority."""


try:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import myrmex_campaign_intelligence as intel  # type: ignore
except Exception as exc:  # pragma: no cover - import-time only
    raise PlanStoreError(
        "P1-002 backend is unavailable; plan store fails closed"
    ) from exc


PLAN_SCHEMA = "myrmex.plan-revision/v1"

LIFECYCLE_STATES = {
    "proposed", "reviewed", "validated", "active",
    "rejected", "superseded", "withdrawn",
}

# Exact transition table from tests/test-plan-contracts.py.
TRANSITIONS: dict[str, set[str]] = {
    "proposed": {"reviewed", "rejected", "withdrawn", "superseded"},
    "reviewed": {"validated", "rejected", "withdrawn", "superseded"},
    "validated": {"active", "rejected", "withdrawn", "superseded"},
    "active": {"superseded", "withdrawn", "rejected"},
    "rejected": set(),
    "superseded": set(),
    "withdrawn": set(),
}

PLAN_RECORD_FIELDS = (
    "schema", "record_id", "plan_revision_id", "campaign_id", "objective_id",
    "planning_request_id", "base_sha", "parent_revision", "input_digests",
    "assumptions", "work_units", "edges", "lifecycle_status",
    "previous_record_id", "plan_digest", "record_digest", "created_at",
)
PLAN_PAYLOAD_EXCLUDED = {
    "record_id", "record_digest", "previous_record_id", "lifecycle_status",
    "created_at", "plan_digest", "plan_revision_id",
}
RECORD_DIGEST_EXCLUDED = {"record_id", "record_digest"}

WU_FIELDS = (
    "id", "objective", "non_goals", "dependencies", "scope", "acceptance_criteria",
    "verification", "risk_class", "required_route", "human_gates",
    "required_evidence", "terminal_gate",
)
WU_SCOPE_FIELDS = ("allowed_paths", "forbidden_paths")
WU_VERIFICATION_FIELDS = ("commands", "manual_checks", "discover_when_missing")
HUMAN_GATE_FIELDS = ("gate_id", "decision_type", "reason", "required_before")
ASSUMPTION_FIELDS = ("assumption_id", "statement", "evidence_status", "impact", "resolution_gate")
INPUT_DIGEST_FIELDS = ("kind", "identity", "sha256")
PARENT_REVISION_FIELDS = ("artifact_id", "artifact_digest")

CAMPAIGN_ID_RE = re.compile(r"^camp-[a-z0-9][a-z0-9-]{4,60}$")
WU_ID_RE = re.compile(r"^WU-[A-Z0-9][A-Z0-9-]{0,30}$")
RECORD_ID_RE = re.compile(r"^planrec_[0-9a-f]{64}$")
PLAN_REVISION_ID_RE = re.compile(r"^plan_[0-9a-f]{64}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

ARTIFACT_NAMESPACE = "plan-revision/record/"


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Pure digest/identity helpers


def plan_payload(record: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in record.items() if k not in PLAN_PAYLOAD_EXCLUDED}


def compute_plan_digest(record: dict[str, Any]) -> str:
    return sha256_hex(canonical_json_bytes(plan_payload(record)))


def compute_record_digest(record: dict[str, Any]) -> str:
    core = {k: v for k, v in record.items() if k not in RECORD_DIGEST_EXCLUDED}
    return sha256_hex(canonical_json_bytes(core))


def derive_plan_revision_id(plan_digest: str) -> str:
    return "plan_" + plan_digest


def derive_record_id(record_digest: str) -> str:
    return "planrec_" + record_digest


def plan_record_artifact_id(record_id: str) -> str:
    return ARTIFACT_NAMESPACE + record_id


def same_plan_content(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return canonical_json_bytes(plan_payload(a)) == canonical_json_bytes(plan_payload(b))


# ---------------------------------------------------------------------------
# Structural validation (all checks before any digest recomputation)


def _obj_fields(value: Any, allowed: tuple[str, ...], label: str) -> None:
    if not isinstance(value, dict):
        raise PlanRecordInvalid(f"{label} must be an object")
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise PlanRecordInvalid(f"{label} has unknown fields: {', '.join(unknown)}")
    missing = [f for f in allowed if f not in value]
    if missing:
        raise PlanRecordInvalid(f"{label} is missing: {', '.join(missing)}")


def _require_str(value: Any, label: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        raise PlanRecordInvalid(f"{label} must be a non-empty string")
    return value


def _require_str_list(value: Any, label: str, *, min_items: int = 0, nonempty_entries: bool = False) -> list[str]:
    if not isinstance(value, list) or len(value) < min_items:
        raise PlanRecordInvalid(f"{label} must be a list with at least {min_items} items")
    for entry in value:
        if not isinstance(entry, str):
            raise PlanRecordInvalid(f"{label} entries must be strings")
        if nonempty_entries and not entry:
            raise PlanRecordInvalid(f"{label} entries must be non-empty strings")
    return value


def _validate_parent_revision(value: Any) -> None:
    if value is None:
        return
    _obj_fields(value, PARENT_REVISION_FIELDS, "parent_revision")
    _require_str(value["artifact_id"], "parent_revision.artifact_id")
    if not isinstance(value["artifact_digest"], str) or not SHA256_RE.fullmatch(value["artifact_digest"]):
        raise PlanRecordInvalid("parent_revision.artifact_digest must be a 64-hex digest")


def _validate_input_digests(value: Any) -> None:
    if not isinstance(value, list) or not value:
        raise PlanRecordInvalid("input_digests must be a non-empty list")
    seen: dict[str, str] = {}
    exact: list[tuple] = []
    for entry in value:
        _obj_fields(entry, INPUT_DIGEST_FIELDS, "input_digest")
        _require_str(entry["kind"], "input_digest.kind")
        _require_str(entry["identity"], "input_digest.identity")
        if not isinstance(entry["sha256"], str) or not SHA256_RE.fullmatch(entry["sha256"]):
            raise PlanRecordInvalid("input_digest.sha256 must be a 64-hex digest")
        pair = (entry["kind"], entry["identity"], entry["sha256"])
        if pair in exact:
            raise PlanRecordInvalid("duplicate input_digest entry")
        exact.append(pair)
        # P1-001 semantics: identity is the conflict key regardless of kind.
        if entry["identity"] in seen and seen[entry["identity"]] != entry["sha256"]:
            raise PlanRecordInvalid("same input identity bound to conflicting digests")
        seen[entry["identity"]] = entry["sha256"]


def _validate_assumptions(value: Any) -> None:
    if not isinstance(value, list):
        raise PlanRecordInvalid("assumptions must be a list")
    for entry in value:
        _obj_fields(entry, ASSUMPTION_FIELDS, "assumption")
        _require_str(entry["assumption_id"], "assumption.assumption_id")
        _require_str(entry["statement"], "assumption.statement")
        if entry["evidence_status"] not in ("supported", "safe_default", "unverified"):
            raise PlanRecordInvalid("assumption.evidence_status must be supported|safe_default|unverified")
        _require_str(entry["impact"], "assumption.impact")
        if entry["resolution_gate"] is not None and not isinstance(entry["resolution_gate"], str):
            raise PlanRecordInvalid("assumption.resolution_gate must be string or null")


def _validate_work_units(value: Any) -> None:
    if not isinstance(value, list) or not value:
        raise PlanRecordInvalid("work_units must be a non-empty list")
    ids: list[str] = []
    for wu in value:
        _obj_fields(wu, WU_FIELDS, "work_unit")
        wu_id = _require_str(wu["id"], "work_unit.id")
        if not WU_ID_RE.fullmatch(wu_id):
            raise PlanRecordInvalid("work_unit.id must match ^WU-[A-Z0-9][A-Z0-9-]{0,30}$")
        ids.append(wu_id)
        _require_str(wu["objective"], "work_unit.objective")
        _require_str_list(wu["non_goals"], "work_unit.non_goals")
        _require_str_list(wu["dependencies"], "work_unit.dependencies")
        _obj_fields(wu["scope"], WU_SCOPE_FIELDS, "work_unit.scope")
        _require_str_list(wu["scope"]["allowed_paths"], "work_unit.scope.allowed_paths")
        _require_str_list(wu["scope"]["forbidden_paths"], "work_unit.scope.forbidden_paths")
        _require_str_list(wu["acceptance_criteria"], "work_unit.acceptance_criteria", min_items=1, nonempty_entries=True)
        _obj_fields(wu["verification"], WU_VERIFICATION_FIELDS, "work_unit.verification")
        _require_str_list(wu["verification"]["commands"], "work_unit.verification.commands")
        _require_str_list(wu["verification"]["manual_checks"], "work_unit.verification.manual_checks")
        if not isinstance(wu["verification"]["discover_when_missing"], bool):
            raise PlanRecordInvalid("work_unit.verification.discover_when_missing must be a boolean")
        if wu["risk_class"] not in ("bounded", "unbounded"):
            raise PlanRecordInvalid("work_unit.risk_class must be bounded|unbounded")
        if wu["required_route"] not in ("auto", "direct-only", "delegated", "frontier", "frontier-gated"):
            raise PlanRecordInvalid("work_unit.required_route must be auto|direct-only|delegated|frontier|frontier-gated")
        human_gates = wu["human_gates"]
        if not isinstance(human_gates, list):
            raise PlanRecordInvalid("work_unit.human_gates must be a list")
        for gate in human_gates:
            _obj_fields(gate, HUMAN_GATE_FIELDS, "human_gate")
            _require_str(gate["gate_id"], "human_gate.gate_id")
            _require_str(gate["decision_type"], "human_gate.decision_type")
            _require_str(gate["reason"], "human_gate.reason")
            if gate["required_before"] not in ("plan_activation", "work_unit_ready", "repository_effect", "delivery"):
                raise PlanRecordInvalid("human_gate.required_before must be plan_activation|work_unit_ready|repository_effect|delivery")
        _require_str_list(wu["required_evidence"], "work_unit.required_evidence")
        _require_str(wu["terminal_gate"], "work_unit.terminal_gate")
    if len(ids) != len(set(ids)):
        raise PlanRecordInvalid("work_unit IDs must be unique")
    id_set = set(ids)
    for wu in value:
        for dep in wu["dependencies"]:
            if dep not in id_set:
                raise PlanRecordInvalid(f"work_unit dependency {dep!r} references an undefined WU")
            if dep == wu["id"]:
                raise PlanRecordInvalid("work_unit must not depend on itself")


def _validate_edges(value: Any, work_units: list[dict[str, Any]]) -> None:
    if not isinstance(value, list):
        raise PlanRecordInvalid("edges must be a list")
    id_set = {wu["id"] for wu in work_units}
    seen_pairs: set[tuple] = set()
    for edge in value:
        if not isinstance(edge, list) or len(edge) != 2:
            raise PlanRecordInvalid("each edge must be exactly [from_wu_id, to_wu_id]")
        frm, to = edge
        if not isinstance(frm, str) or not WU_ID_RE.fullmatch(frm) or not isinstance(to, str) or not WU_ID_RE.fullmatch(to):
            raise PlanRecordInvalid("edge endpoints must be valid WU IDs")
        if frm not in id_set or to not in id_set:
            raise PlanRecordInvalid("edge endpoint references an undefined WU")
        if frm == to:
            raise PlanRecordInvalid("edge must not be a self-edge")
        if (frm, to) in seen_pairs:
            raise PlanRecordInvalid("duplicate edge pair")
        seen_pairs.add((frm, to))
    # Edge/dependency correspondence.
    dep_map = {wu["id"]: set(wu["dependencies"]) for wu in work_units}
    for frm, to in seen_pairs:
        if frm not in dep_map[to]:
            raise PlanRecordInvalid("edge [from,to] missing from to.dependencies")
    for wu in work_units:
        for dep in wu["dependencies"]:
            if (dep, wu["id"]) not in seen_pairs:
                raise PlanRecordInvalid("declared dependency missing its edge")


RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


def _validate_datetime(value: Any) -> None:
    if not isinstance(value, str) or not RFC3339_RE.fullmatch(value):
        raise PlanRecordInvalid(
            "created_at must be RFC3339 date-time with uppercase T separator and explicit timezone"
        )
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PlanRecordInvalid("created_at must be an RFC3339 date-time") from exc
    if parsed.tzinfo is None:
        raise PlanRecordInvalid("created_at must include a timezone")


def validate_lifecycle_transition(previous_status: str | None, current_status: str) -> bool:
    if current_status not in LIFECYCLE_STATES:
        return False
    if previous_status is None:
        return current_status == "proposed"
    if previous_status not in LIFECYCLE_STATES:
        return False
    return current_status in TRANSITIONS[previous_status]


def validate_plan_revision_record(record: Any) -> None:
    """Strict production validator; structural checks before digest recomputation."""
    if not isinstance(record, dict):
        raise PlanRecordInvalid("plan record must be an object")
    _obj_fields(record, PLAN_RECORD_FIELDS, "plan record")
    if record["schema"] != PLAN_SCHEMA:
        raise PlanRecordInvalid("plan record schema mismatch")
    if not isinstance(record["record_id"], str) or not RECORD_ID_RE.fullmatch(record["record_id"]):
        raise PlanRecordInvalid("record_id must match ^planrec_[0-9a-f]{64}$")
    if not isinstance(record["plan_revision_id"], str) or not PLAN_REVISION_ID_RE.fullmatch(record["plan_revision_id"]):
        raise PlanRecordInvalid("plan_revision_id must match ^plan_[0-9a-f]{64}$")
    if not isinstance(record["campaign_id"], str) or not CAMPAIGN_ID_RE.fullmatch(record["campaign_id"]):
        raise PlanRecordInvalid("campaign_id must match ^camp-[a-z0-9][a-z0-9-]{4,60}$")
    if not isinstance(record["base_sha"], str) or not GIT_SHA_RE.fullmatch(record["base_sha"]):
        raise PlanRecordInvalid("base_sha must match ^[0-9a-f]{40}$")
    if not isinstance(record["plan_digest"], str) or not SHA256_RE.fullmatch(record["plan_digest"]):
        raise PlanRecordInvalid("plan_digest must be a 64-hex digest")
    if not isinstance(record["record_digest"], str) or not SHA256_RE.fullmatch(record["record_digest"]):
        raise PlanRecordInvalid("record_digest must be a 64-hex digest")
    prev = record["previous_record_id"]
    if prev is not None and (not isinstance(prev, str) or not RECORD_ID_RE.fullmatch(prev)):
        raise PlanRecordInvalid("previous_record_id must be null or ^planrec_[0-9a-f]{64}$")
    for key in ("objective_id", "planning_request_id"):
        value = record[key]
        if not isinstance(value, str) or not value or len(value) > 256:
            raise PlanRecordInvalid(f"{key} must be a non-empty string of at most 256 characters")
    if record["lifecycle_status"] not in LIFECYCLE_STATES:
        raise PlanRecordInvalid("lifecycle_status must be one of the seven plan states")
    _validate_parent_revision(record["parent_revision"])
    _validate_input_digests(record["input_digests"])
    _validate_assumptions(record["assumptions"])
    _validate_work_units(record["work_units"])
    _validate_edges(record["edges"], record["work_units"])
    _validate_datetime(record["created_at"])

    # Digest recomputation AFTER structural checks.
    if record["plan_digest"] != compute_plan_digest(record):
        raise PlanRecordInvalid("plan_digest does not recompute")
    if record["plan_revision_id"] != derive_plan_revision_id(record["plan_digest"]):
        raise PlanRecordInvalid("plan_revision_id does not derive from plan_digest")
    if record["record_digest"] != compute_record_digest(record):
        raise PlanRecordInvalid("record_digest does not recompute")
    if record["record_id"] != derive_record_id(record["record_digest"]):
        raise PlanRecordInvalid("record_id does not derive from record_digest")


# ---------------------------------------------------------------------------
# Lifecycle builder


def build_lifecycle_record(
    previous_record: dict[str, Any],
    lifecycle_status: str,
    created_at: str,
) -> dict[str, Any]:
    """Build the next immutable lifecycle record from a previous record."""
    validate_plan_revision_record(previous_record)
    if not validate_lifecycle_transition(previous_record["lifecycle_status"], lifecycle_status):
        raise PlanLifecycleInvalid(
            f"illegal lifecycle transition {previous_record['lifecycle_status']} -> {lifecycle_status}"
        )
    if lifecycle_status == "active":
        raise PlanActivationAuthorityRequired(
            "creating a previously absent active record requires governed activation authority (P1-012)"
        )
    _validate_datetime(created_at)
    payload = plan_payload(previous_record)
    record = {
        **payload,
        "lifecycle_status": lifecycle_status,
        "previous_record_id": previous_record["record_id"],
        "created_at": created_at,
        "plan_digest": previous_record["plan_digest"],
        "plan_revision_id": previous_record["plan_revision_id"],
        "record_digest": "",
        "record_id": "",
    }
    record["record_digest"] = compute_record_digest(record)
    record["record_id"] = derive_record_id(record["record_digest"])
    validate_plan_revision_record(record)
    return record


# ---------------------------------------------------------------------------
# Dedicated plan-store lock


@contextmanager
def _plan_store_lock(campaign_dir: pathlib.Path) -> Iterator[None]:
    """Exclusive per-campaign plan-store lock; fail closed on unavailability."""
    try:
        import fcntl
    except ImportError as exc:
        raise PlanStoreBackendUnavailable("fcntl is unavailable; plan-store locking fails closed") from exc
    intel_dir = pathlib.Path(campaign_dir) / "intelligence"
    if intel_dir.is_symlink():
        raise PlanStoreBackendUnavailable("intelligence directory must not be a symlink")
    if not intel_dir.exists():
        try:
            intel_dir.mkdir(mode=0o700, parents=False)
        except OSError as exc:
            raise PlanStoreBackendUnavailable("cannot create intelligence directory") from exc
    if not intel_dir.is_dir():
        raise PlanStoreBackendUnavailable("intelligence path is not a directory")
    lock_path = intel_dir / "plan-store.lock"
    if lock_path.is_symlink():
        raise PlanStoreBackendUnavailable("plan-store.lock must not be a symlink")
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as exc:
        raise PlanStoreBackendUnavailable("cannot open plan-store.lock") from exc
    try:
        st = os.fstat(fd)
        if not os.path.isfile(lock_path) or st.st_mode & 0o170000 != 0o100000:
            raise PlanStoreBackendUnavailable("plan-store.lock must be a regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError as exc:
            raise PlanStoreBackendUnavailable("plan-store locking unavailable") from exc
        try:
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Chain reconstruction


def _list_plan_record_envelopes(campaign_dir, campaign_id) -> list[dict[str, Any]]:
    result = intel.list_artifacts(pathlib.Path(campaign_dir), campaign_id, kind="plan")
    status = result.get("status")
    if status in ("projection_missing", "projection_stale"):
        try:
            intel.rebuild_projection(pathlib.Path(campaign_dir), campaign_id)
        except Exception as exc:
            raise PlanStoreBackendUnavailable(
                "plan projection rebuild failed; durable history cannot be reconstructed"
            ) from exc
        result = intel.list_artifacts(pathlib.Path(campaign_dir), campaign_id, kind="plan")
        status = result.get("status")
    if status != "healthy":
        raise PlanStoreBackendUnavailable(
            "plan projection is not healthy; durable plan history cannot be trusted as empty"
        )
    artifacts = result.get("artifacts")
    if isinstance(artifacts, dict):
        plan_descriptors = artifacts.get("plan", [])
    else:
        plan_descriptors = []
    envelopes = []
    for desc in plan_descriptors or []:
        artifact_id = desc.get("artifact_id") if isinstance(desc, dict) else None
        if not isinstance(artifact_id, str) or not artifact_id.startswith(ARTIFACT_NAMESPACE):
            continue
        env = intel.get_artifact(pathlib.Path(campaign_dir), campaign_id, artifact_id)
        if not isinstance(env, dict) or not isinstance(env.get("artifact"), dict):
            raise PlanStoreBackendUnavailable(f"plan artifact invalid: {artifact_id}")
        artifact = env["artifact"]
        if artifact.get("kind") != "plan":
            raise PlanLifecycleInvalid("plan-store namespace artifact has non-plan kind")
        payload = artifact.get("payload")
        validate_plan_revision_record(payload)
        expected_id = ARTIFACT_NAMESPACE + payload["record_id"]
        if artifact.get("artifact_id") != expected_id:
            raise PlanLifecycleInvalid("plan artifact ID does not derive from record_id")
        envelopes.append(payload)
    return envelopes


def _load_plan_chain(campaign_dir, campaign_id, plan_revision_id: str) -> list[dict[str, Any]]:
    """Reconstruct the linear lifecycle chain for one plan revision."""
    records = _list_plan_record_envelopes(campaign_dir, campaign_id)
    revision_records = [r for r in records if r["plan_revision_id"] == plan_revision_id]
    if not revision_records:
        return []
    by_id = {r["record_id"]: r for r in revision_records}
    roots = [r for r in revision_records if r["previous_record_id"] is None]
    if len(roots) != 1:
        raise PlanLifecycleInvalid("plan revision must have exactly one root")
    root = roots[0]
    if root["lifecycle_status"] != "proposed":
        raise PlanLifecycleInvalid("plan revision root must be proposed")
    chain = [root]
    current = root
    visited = {current["record_id"]}
    while True:
        children = [r for r in revision_records if r["previous_record_id"] == current["record_id"]]
        if not children:
            break
        if len(children) > 1:
            raise PlanLifecycleConflict("plan lifecycle fork detected")
        child = children[0]
        if child["record_id"] in visited:
            raise PlanLifecycleInvalid("plan lifecycle cycle detected")
        if not same_plan_content(root, child):
            raise PlanLifecycleInvalid("plan payload drift across lifecycle records")
        if not validate_lifecycle_transition(current["lifecycle_status"], child["lifecycle_status"]):
            raise PlanLifecycleInvalid(
                f"illegal lifecycle transition {current['lifecycle_status']} -> {child['lifecycle_status']}"
            )
        visited.add(child["record_id"])
        chain.append(child)
        current = child
    # no disconnected records
    if len(chain) != len(revision_records):
        raise PlanLifecycleInvalid("plan lifecycle has disconnected records")
    return chain


def get_plan_record(campaign_dir, campaign_id, record_id: str) -> dict[str, Any]:
    if not isinstance(record_id, str) or not RECORD_ID_RE.fullmatch(record_id):
        raise PlanRecordInvalid("record_id must match ^planrec_[0-9a-f]{64}$")
    artifact_id = ARTIFACT_NAMESPACE + record_id
    try:
        env = intel.get_artifact(pathlib.Path(campaign_dir), campaign_id, artifact_id)
    except intel.IntelligenceArtifactInvalid as exc:
        raise PlanRecordNotFound(f"plan record not found: {record_id}") from exc
    if not isinstance(env, dict) or not isinstance(env.get("artifact"), dict):
        raise PlanRecordNotFound(f"plan record not found: {record_id}")
    artifact = env["artifact"]
    # Direct lookup is also a trust boundary: enforce kind + namespace + ID derivation.
    if artifact.get("kind") != "plan":
        raise PlanLifecycleInvalid("plan-store namespace artifact has non-plan kind")
    if artifact.get("artifact_id") != artifact_id:
        raise PlanLifecycleInvalid("plan artifact ID does not match requested record")
    payload = artifact.get("payload")
    validate_plan_revision_record(payload)
    if artifact.get("artifact_id") != ARTIFACT_NAMESPACE + payload["record_id"]:
        raise PlanLifecycleInvalid("plan artifact ID does not derive from payload record_id")
    return payload


def get_plan_head(campaign_dir, campaign_id, plan_revision_id: str) -> dict[str, Any]:
    if not isinstance(plan_revision_id, str) or not PLAN_REVISION_ID_RE.fullmatch(plan_revision_id):
        raise PlanRecordInvalid("plan_revision_id must match ^plan_[0-9a-f]{64}$")
    chain = _load_plan_chain(campaign_dir, campaign_id, plan_revision_id)
    if not chain:
        raise PlanRecordNotFound(f"no records for plan revision {plan_revision_id}")
    return chain[-1]


def _policy_reject(record: dict[str, Any]) -> None:
    try:
        intel.reject_secret_or_raw(record)
    except intel.IntelligencePayloadRejected as exc:
        raise PlanRecordInvalid(
            "plan record rejected by secret/raw-content policy"
        ) from exc


def store_plan_record(campaign_dir, campaign_id, campaign_revision: int, record: dict[str, Any]) -> dict[str, Any]:
    """Persist one immutable plan lifecycle record with linear-chain enforcement."""
    if isinstance(campaign_revision, bool) or not isinstance(campaign_revision, int) or campaign_revision < 0:
        raise PlanRecordInvalid("campaign_revision must be a non-negative integer")
    validate_plan_revision_record(record)
    if record["campaign_id"] != campaign_id:
        raise PlanLifecycleInvalid("record campaign_id does not match requested campaign")
    _policy_reject(record)

    plan_revision_id = record["plan_revision_id"]
    with _plan_store_lock(pathlib.Path(campaign_dir)):
        chain = _load_plan_chain(campaign_dir, campaign_id, plan_revision_id)
        by_id = {r["record_id"]: r for r in chain}

        # Exact existing record: idempotent reuse.
        if record["record_id"] in by_id:
            stored = by_id[record["record_id"]]
            if canonical_json_bytes(stored) != canonical_json_bytes(record):
                raise PlanStoreConflict("exact record ID exists with different payload")
            return _result(stored, "reused", chain)

        # Activation authority gate for previously absent active records.
        if record["lifecycle_status"] == "active":
            raise PlanActivationAuthorityRequired(
                "creating a previously absent active record requires governed activation authority (P1-012)"
            )

        if not chain:
            # First record for this revision.
            if record["lifecycle_status"] != "proposed" or record["previous_record_id"] is not None:
                raise PlanLifecycleInvalid("first record for a plan revision must be proposed with previous_record_id=null")
        else:
            head = chain[-1]
            prev = record["previous_record_id"]
            if prev is None:
                raise PlanLifecycleInvalid("successor record must reference the current head")
            if prev not in by_id:
                raise PlanLifecycleInvalid("previous_record_id references a non-head or absent record")
            if prev != head["record_id"]:
                raise PlanLifecycleConflict("cannot append to a non-head record (stale head)")
            if record["plan_revision_id"] != head["plan_revision_id"]:
                raise PlanLifecycleInvalid("successor plan revision mismatch")
            if not same_plan_content(head, record):
                raise PlanLifecycleInvalid("successor plan payload differs from head")
            if not validate_lifecycle_transition(head["lifecycle_status"], record["lifecycle_status"]):
                raise PlanLifecycleInvalid(
                    f"illegal lifecycle transition {head['lifecycle_status']} -> {record['lifecycle_status']}"
                )

        artifact_id = ARTIFACT_NAMESPACE + record["record_id"]
        try:
            put = intel.put_artifact(
                pathlib.Path(campaign_dir), campaign_id, campaign_revision,
                "plan", artifact_id, record,
            )
        except intel.IntelligenceArtifactConflict as exc:
            raise PlanStoreConflict(str(exc)) from exc
        except Exception as exc:
            raise PlanStoreBackendUnavailable("plan artifact persistence failed") from exc
        status = put.get("status", "created")
        # Re-fetch and re-validate, reconstruct the chain.
        fetched = get_plan_record(campaign_dir, campaign_id, record["record_id"])
        new_chain = _load_plan_chain(campaign_dir, campaign_id, plan_revision_id)
        if len(new_chain) > 1 and new_chain[-1]["record_id"] != record["record_id"]:
            raise PlanStoreConflict("new record is not the deterministic lifecycle head")
        return _result(fetched, status, new_chain)


def _result(record: dict[str, Any], status: str, chain: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "ok": True,
        "status": status,
        "artifact_id": ARTIFACT_NAMESPACE + record["record_id"],
        "plan_revision_id": record["plan_revision_id"],
        "record_id": record["record_id"],
        "plan_digest": record["plan_digest"],
        "record_digest": record["record_digest"],
        "lifecycle_status": record["lifecycle_status"],
        "chain_length": len(chain),
        "head_record_id": chain[-1]["record_id"] if chain else record["record_id"],
    }
