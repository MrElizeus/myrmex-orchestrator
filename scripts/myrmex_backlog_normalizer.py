#!/usr/bin/env python3
"""Immutable durable backlog normalization for Myrmex P1-006.

Converts validated P1-004 local neutral representations and P1-005 GitHub
neutral representations into immutable normalized backlog item versions plus
one immutable normalized backlog snapshot, stored entirely in the P1-002
Campaign Intelligence sidecar.

Safety/design:
  * source observations are loaded from P1-002 via get_artifact(); the
    caller-supplied neutral must recompute to the durable observation
    content_digest;
  * only ``changed`` and ``unchanged`` observations are normalizable;
  * raw issue bodies, milestone descriptions, local source text, comments,
    users, URLs, and P1-004 source locations are never persisted;
  * ``backlog_item_id`` is stable under source reordering and mutable
    metadata changes; ``item_digest`` versions semantic metadata;
  * the snapshot is written LAST and is the authoritative completion marker;
  * no WU, DAG, plan, or campaign mutation is performed.
"""
from __future__ import annotations

import hashlib
import pathlib
import re
import sys
from typing import Any


class BacklogNormalizationError(Exception):
    """Base error for backlog normalization."""


try:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import myrmex_campaign_intelligence as intel  # type: ignore
    import myrmex_backlog_import as backlog_import  # type: ignore
    import myrmex_roadmap_reader as roadmap_reader  # type: ignore
    import myrmex_github_reader as github_reader  # type: ignore
except Exception as exc:  # pragma: no cover - import-time only
    raise BacklogNormalizationError(
        "required P1-002/P1-003/P1-004/P1-005 backend is unavailable; normalization fails closed"
    ) from exc


class BacklogNormalizationInvalid(BacklogNormalizationError):
    """Source descriptor, neutral, or normalized payload is invalid."""


class BacklogNormalizationSourceNotReady(BacklogNormalizationError):
    """Source observation outcome is not changed/unchanged."""


class BacklogNormalizationSourceMismatch(BacklogNormalizationError):
    """Neutral representation does not match the durable source observation."""


class BacklogNormalizationConflict(BacklogNormalizationError):
    """Derived-ID collision or conflicting immutable artifact."""


NORMALIZED_ITEM_SCHEMA = "myrmex.normalized-backlog-item/v1"
NORMALIZED_SNAPSHOT_SCHEMA = "myrmex.normalized-backlog-snapshot/v1"

LOCAL_SOURCE_TYPES = ("roadmap_markdown", "manifest_json", "manifest_yaml")
LOCAL_ADAPTER_KINDS = {
    "roadmap_markdown": ("local-markdown-roadmap/v1", "local-roadmap"),
    "manifest_json": ("local-manifest-json/v1", "local-manifest"),
    "manifest_yaml": ("local-manifest-yaml/v1", "local-manifest"),
}

NORMALIZED_ITEM_FIELDS = (
    "schema", "backlog_item_id", "item_digest", "source_adapter", "source_identity",
    "source_entity_type", "source_entity_id", "title", "priority", "state",
    "dependency_hints", "constraints", "context_constraints", "labels", "group_ref",
)
SNAPSHOT_FIELDS = (
    "schema", "snapshot_record_id", "snapshot_record_digest", "snapshot_digest",
    "source_count", "sources", "item_count", "items",
)
SOURCE_DESCRIPTOR_FIELDS = ("operation_id", "neutral")
GROUP_REF_LOCAL_FIELDS = ("kind", "id", "title")
GROUP_REF_GITHUB_FIELDS = ("kind", "id", "number", "title", "state", "due_on")

SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
OPERATION_ID_RE = re.compile(r"^importop-[0-9a-f]{24}$")


def canonical_json_bytes(value: Any) -> bytes:
    return json_dumps(value)


def json_dumps(value: Any) -> bytes:
    import json
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _policy_reject(value: Any, where: str = "$") -> None:
    try:
        intel.reject_secret_or_raw(value, where)
    except intel.IntelligencePayloadRejected as exc:
        raise BacklogNormalizationInvalid(
            f"normalized payload rejected by secret/raw-content policy at {where}"
        ) from exc


def _obj_fields(value: Any, allowed: tuple[str, ...], label: str) -> None:
    if not isinstance(value, dict):
        raise BacklogNormalizationInvalid(f"{label} must be an object")
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise BacklogNormalizationInvalid(f"{label} has unknown fields: {', '.join(unknown)}")
    missing = [f for f in allowed if f not in value]
    if missing:
        raise BacklogNormalizationInvalid(f"{label} is missing: {', '.join(missing)}")


# ---------------------------------------------------------------------------
# Source loading + digest binding


def _load_source_observation(campaign_dir, campaign_id, operation_id: str) -> dict[str, Any]:
    if not isinstance(operation_id, str) or not OPERATION_ID_RE.fullmatch(operation_id):
        raise BacklogNormalizationInvalid("operation_id must match ^importop-[0-9a-f]{24}$")
    artifact_id = f"source-observation/{operation_id}"
    try:
        envelope = intel.get_artifact(pathlib.Path(campaign_dir), campaign_id, artifact_id)
    except Exception as exc:
        raise BacklogNormalizationInvalid(f"source observation not found: {operation_id}") from exc
    if not isinstance(envelope, dict) or not isinstance(envelope.get("artifact"), dict):
        raise BacklogNormalizationInvalid("source observation envelope is invalid")
    artifact = envelope["artifact"]
    if artifact.get("kind") != "backlog" or artifact.get("artifact_id") != artifact_id:
        raise BacklogNormalizationInvalid("source observation artifact kind/id mismatch")
    payload = artifact.get("payload")
    if not isinstance(payload, dict):
        raise BacklogNormalizationInvalid("source observation payload must be an object")
    errors = backlog_import.validate_source_observation_semantics(payload)
    if errors:
        raise BacklogNormalizationInvalid("source observation failed semantic validation: " + "; ".join(errors))
    if payload.get("operation_id") != operation_id:
        raise BacklogNormalizationInvalid("source observation operation_id mismatch")
    outcome = payload.get("outcome")
    if outcome not in ("changed", "unchanged"):
        raise BacklogNormalizationSourceNotReady(
            f"source observation outcome {outcome!r} is not normalizable (changed/unchanged required)"
        )
    content_digest = payload.get("content_digest")
    if not isinstance(content_digest, str) or not SHA256_HEX_RE.fullmatch(content_digest):
        raise BacklogNormalizationInvalid("source observation content_digest must be a 64-hex digest")
    return payload


def _bind_source(
    campaign_dir, campaign_id, source: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate one source descriptor; return (observation_payload, neutral)."""
    if not isinstance(source, dict):
        raise BacklogNormalizationInvalid("source must be an object")
    _obj_fields(source, SOURCE_DESCRIPTOR_FIELDS, "source descriptor")
    operation_id = source["operation_id"]
    neutral = source["neutral"]
    if not isinstance(neutral, dict):
        raise BacklogNormalizationInvalid("neutral must be an object")
    observation = _load_source_observation(campaign_dir, campaign_id, operation_id)

    schema = neutral.get("schema")
    adapter = observation.get("adapter")
    source_identity = observation.get("source_identity")

    if schema == "myrmex.local-source-neutral/v1":
        roadmap_reader.validate_neutral_representation(neutral)
        if neutral.get("ambiguities"):
            raise BacklogNormalizationSourceNotReady("local neutral has ambiguities")
        recomputed = roadmap_reader.semantic_source_digest(neutral)
        if recomputed != observation.get("content_digest"):
            raise BacklogNormalizationSourceMismatch("local neutral digest does not match source observation")
        source_type = neutral.get("source_type")
        if source_type not in LOCAL_ADAPTER_KINDS:
            raise BacklogNormalizationInvalid(f"unsupported local source_type {source_type!r}")
        expected_adapter, expected_kind = LOCAL_ADAPTER_KINDS[source_type]
        if adapter != expected_adapter or not isinstance(source_identity, dict) or source_identity.get("kind") != expected_kind:
            raise BacklogNormalizationSourceMismatch("local adapter/source-kind mismatch")
        # Every source location path must equal the durable source identity canonical path.
        canonical_path = source_identity.get("canonical_id")
        for obj in neutral.get("objectives", []):
            if obj.get("source_location", {}).get("path") != canonical_path:
                raise BacklogNormalizationSourceMismatch("local source-location path mismatch")
        for item in neutral.get("items", []):
            if item.get("source_location", {}).get("path") != canonical_path:
                raise BacklogNormalizationSourceMismatch("local source-location path mismatch")
    elif schema == "myrmex.github-source-neutral/v1":
        github_reader.validate_github_neutral_snapshot(neutral)
        if neutral.get("ambiguities"):
            raise BacklogNormalizationSourceNotReady("github neutral has ambiguities")
        recomputed = github_reader.github_content_digest(neutral)
        if recomputed != observation.get("content_digest"):
            raise BacklogNormalizationSourceMismatch("github neutral digest does not match source observation")
        if adapter != "github-issues-milestones/v1":
            raise BacklogNormalizationSourceMismatch("github adapter mismatch")
        if not isinstance(source_identity, dict) or source_identity.get("kind") != "github-issues-milestones":
            raise BacklogNormalizationSourceMismatch("github source-kind mismatch")
        if neutral.get("repository") != source_identity.get("canonical_id"):
            raise BacklogNormalizationSourceMismatch("github repository mismatch")
    else:
        raise BacklogNormalizationInvalid(f"unsupported neutral schema {schema!r}")

    return observation, neutral


# ---------------------------------------------------------------------------
# Normalized item construction


def compute_backlog_item_id(source_adapter: str, source_identity: Any, source_entity_id: str) -> str:
    core = {
        "source_adapter": source_adapter,
        "source_identity": source_identity,
        "source_entity_id": source_entity_id,
    }
    return "backlog_" + sha256_hex(canonical_json_bytes(core))


def compute_item_digest(item: dict[str, Any]) -> str:
    core = {k: v for k, v in item.items() if k != "item_digest"}
    return sha256_hex(canonical_json_bytes(core))


def _sorted_unique(values: list[str]) -> list[str]:
    return sorted({roadmap_reader.normalize_text(str(v)) for v in values})


def _build_local_item(neutral: dict[str, Any], item: dict[str, Any], source_identity: Any, adapter: str) -> dict[str, Any]:
    doc_constraints = neutral.get("constraints", [])
    objective = next(
        (o for o in neutral.get("objectives", []) if o["objective_id"] == item.get("objective_id")),
        None,
    )
    context = list(doc_constraints)
    if objective is not None:
        context.extend(objective.get("constraints", []))
    context_constraints = _sorted_unique(context)
    group_ref = None
    if objective is not None:
        group_ref = {
            "kind": "local-objective",
            "id": objective["objective_id"],
            "title": roadmap_reader.normalize_text(objective["title"]),
        }
    normalized = {
        "schema": NORMALIZED_ITEM_SCHEMA,
        "backlog_item_id": "",  # filled after computing identity core
        "item_digest": "",
        "source_adapter": adapter,
        "source_identity": source_identity,
        "source_entity_type": "local-item",
        "source_entity_id": item["item_id"],
        "title": roadmap_reader.normalize_text(item["title"]),
        "priority": roadmap_reader.normalize_text(item["priority"]) if item.get("priority") is not None else None,
        "state": None,
        "dependency_hints": _sorted_unique(item.get("dependency_hints", [])),
        "constraints": _sorted_unique(item.get("constraints", [])),
        "context_constraints": context_constraints,
        "labels": [],
        "group_ref": group_ref,
    }
    normalized["backlog_item_id"] = compute_backlog_item_id(
        adapter, source_identity, item["item_id"]
    )
    normalized["item_digest"] = compute_item_digest(normalized)
    return normalized


def _build_github_item(neutral: dict[str, Any], issue: dict[str, Any], source_identity: Any, adapter: str) -> dict[str, Any]:
    group_ref = None
    if issue.get("milestone_number") is not None:
        ms = next(
            (m for m in neutral.get("milestones", []) if m["number"] == issue["milestone_number"]),
            None,
        )
        if ms is None:
            raise BacklogNormalizationInvalid("github issue references a missing milestone")
        group_ref = {
            "kind": "github-milestone",
            "id": ms["milestone_id"],
            "number": ms["number"],
            "title": ms["title"],
            "state": ms["state"],
            "due_on": ms.get("due_on"),
        }
    normalized = {
        "schema": NORMALIZED_ITEM_SCHEMA,
        "backlog_item_id": "",
        "item_digest": "",
        "source_adapter": adapter,
        "source_identity": source_identity,
        "source_entity_type": "github-issue",
        "source_entity_id": issue["issue_id"],
        "title": issue["title"],
        "priority": None,
        "state": issue["state"],
        "dependency_hints": [],
        "constraints": [],
        "context_constraints": [],
        "labels": issue["labels"],
        "group_ref": group_ref,
    }
    normalized["backlog_item_id"] = compute_backlog_item_id(
        adapter, source_identity, issue["issue_id"]
    )
    normalized["item_digest"] = compute_item_digest(normalized)
    return normalized


BACKLOG_ITEM_ID_RE = re.compile(r"^backlog_[0-9a-f]{64}$")
ITEM_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
LOCAL_ENTITY_ID_RE = re.compile(r"^srcitem_[0-9a-f]{64}$")
LOCAL_OBJECTIVE_ID_RE = re.compile(r"^srcobj_[0-9a-f]{64}$")
GITHUB_ISSUE_ID_RE = re.compile(r"^ghissue_[0-9a-f]{64}$")
GITHUB_MILESTONE_ID_RE = re.compile(r"^ghmilestone_[0-9a-f]{64}$")

ADAPTER_ENTITY_KINDS = {
    ("local-markdown-roadmap/v1", "local-item"): "local-roadmap",
    ("local-manifest-json/v1", "local-item"): "local-manifest",
    ("local-manifest-yaml/v1", "local-item"): "local-manifest",
    ("github-issues-milestones/v1", "github-issue"): "github-issues-milestones",
}


def _require_sorted_unique_strings(value: Any, label: str) -> None:
    if not isinstance(value, list):
        raise BacklogNormalizationInvalid(f"{label} must be an array of strings")
    for entry in value:
        if not isinstance(entry, str) or not entry:
            raise BacklogNormalizationInvalid(f"{label} entries must be non-empty strings")
    if value != sorted(set(value)):
        raise BacklogNormalizationInvalid(f"{label} must be sorted and unique")


def validate_normalized_item(item: Any) -> None:
    _obj_fields(item, NORMALIZED_ITEM_FIELDS, "normalized item")
    if item["schema"] != NORMALIZED_ITEM_SCHEMA:
        raise BacklogNormalizationInvalid("normalized item schema mismatch")
    if not isinstance(item.get("backlog_item_id"), str) or not BACKLOG_ITEM_ID_RE.fullmatch(item["backlog_item_id"]):
        raise BacklogNormalizationInvalid("backlog_item_id must match ^backlog_[0-9a-f]{64}$")
    if not isinstance(item.get("item_digest"), str) or not ITEM_DIGEST_RE.fullmatch(item["item_digest"]):
        raise BacklogNormalizationInvalid("item_digest must be a 64-hex digest")
    if item["backlog_item_id"] != compute_backlog_item_id(
        item["source_adapter"], item["source_identity"], item["source_entity_id"]
    ):
        raise BacklogNormalizationInvalid("backlog_item_id does not recompute")
    if item["item_digest"] != compute_item_digest(item):
        raise BacklogNormalizationInvalid("item_digest does not recompute")
    # exact source_identity {kind, canonical_id}
    si = item["source_identity"]
    if not isinstance(si, dict) or sorted(si.keys()) != ["canonical_id", "kind"]:
        raise BacklogNormalizationInvalid("source_identity must be exactly {kind, canonical_id}")
    if not isinstance(si.get("kind"), str) or not si["kind"]:
        raise BacklogNormalizationInvalid("source_identity.kind must be a non-empty string")
    if not isinstance(si.get("canonical_id"), str) or not si["canonical_id"]:
        raise BacklogNormalizationInvalid("source_identity.canonical_id must be a non-empty string")
    # exact adapter/entity/source-kind combination
    combo = (item["source_adapter"], item["source_entity_type"])
    if combo not in ADAPTER_ENTITY_KINDS:
        raise BacklogNormalizationInvalid("unknown adapter/entity-type combination")
    if ADAPTER_ENTITY_KINDS[combo] != si["kind"]:
        raise BacklogNormalizationInvalid("source_identity.kind does not match adapter/entity-type")
    # source entity ID format by type
    if item["source_entity_type"] == "local-item":
        if not LOCAL_ENTITY_ID_RE.fullmatch(item["source_entity_id"]):
            raise BacklogNormalizationInvalid("local source_entity_id must match ^srcitem_[0-9a-f]{64}$")
    elif item["source_entity_type"] == "github-issue":
        if not GITHUB_ISSUE_ID_RE.fullmatch(item["source_entity_id"]):
            raise BacklogNormalizationInvalid("github source_entity_id must match ^ghissue_[0-9a-f]{64}$")
    else:
        raise BacklogNormalizationInvalid("unknown source_entity_type")
    # arrays sorted unique
    for key in ("dependency_hints", "constraints", "context_constraints", "labels"):
        _require_sorted_unique_strings(item.get(key), key)
    # type-level shape
    if not isinstance(item.get("title"), str) or not item["title"]:
        raise BacklogNormalizationInvalid("title must be a non-empty string")
    if item.get("priority") is not None and not isinstance(item["priority"], str):
        raise BacklogNormalizationInvalid("priority must be string or null")
    if item["source_entity_type"] == "local-item":
        if item.get("state") is not None:
            raise BacklogNormalizationInvalid("local items must have state=null")
        if item.get("labels") != []:
            raise BacklogNormalizationInvalid("local items must have labels=[]")
        gr = item.get("group_ref")
        if gr is not None:
            _obj_fields(gr, GROUP_REF_LOCAL_FIELDS, "local group_ref")
            if gr["kind"] != "local-objective":
                raise BacklogNormalizationInvalid("local group_ref kind must be local-objective")
            if not LOCAL_OBJECTIVE_ID_RE.fullmatch(gr["id"]):
                raise BacklogNormalizationInvalid("local group_ref.id must match ^srcobj_[0-9a-f]{64}$")
            if not isinstance(gr["title"], str) or not gr["title"]:
                raise BacklogNormalizationInvalid("local group_ref.title must be non-empty")
    else:  # github-issue
        if item.get("priority") is not None:
            raise BacklogNormalizationInvalid("github items must have priority=null")
        if item.get("dependency_hints") != [] or item.get("constraints") != [] or item.get("context_constraints") != []:
            raise BacklogNormalizationInvalid("github items must have empty dependency/constraint arrays")
        if item.get("state") not in ("open", "closed"):
            raise BacklogNormalizationInvalid("github items must have state open|closed")
        gr = item.get("group_ref")
        if gr is not None:
            _obj_fields(gr, GROUP_REF_GITHUB_FIELDS, "github group_ref")
            if gr["kind"] != "github-milestone":
                raise BacklogNormalizationInvalid("github group_ref kind must be github-milestone")
            if not GITHUB_MILESTONE_ID_RE.fullmatch(gr["id"]):
                raise BacklogNormalizationInvalid("github group_ref.id must match ^ghmilestone_[0-9a-f]{64}$")
            if isinstance(gr.get("number"), bool) or not isinstance(gr.get("number"), int) or gr["number"] < 1:
                raise BacklogNormalizationInvalid("github group_ref.number must be a positive integer")
            if not isinstance(gr.get("title"), str) or not gr["title"]:
                raise BacklogNormalizationInvalid("github group_ref.title must be non-empty")
            if gr.get("state") not in ("open", "closed"):
                raise BacklogNormalizationInvalid("github group_ref.state must be open|closed")
            if gr.get("due_on") is not None and (not isinstance(gr["due_on"], str) or not gr["due_on"]):
                raise BacklogNormalizationInvalid("github group_ref.due_on must be non-empty string or null")
            # milestone id must derive from same repository + milestone number
            expected_ms_id = github_reader._milestone_id(si["canonical_id"], gr["number"])
            if gr["id"] != expected_ms_id:
                raise BacklogNormalizationInvalid("github group_ref.id does not derive from repository+number")


def build_normalized_items(sources: list[dict[str, Any]], observations: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Build normalized items for one source list (used by tests for pure verification)."""
    items: list[dict[str, Any]] = []
    for source in sources:
        observation = observations[source["operation_id"]]
        neutral = source["neutral"]
        schema = neutral.get("schema")
        adapter = observation.get("adapter")
        source_identity = observation.get("source_identity")
        if schema == "myrmex.local-source-neutral/v1":
            for item in neutral.get("items", []):
                items.append(_build_local_item(neutral, item, source_identity, adapter))
        else:
            for issue in neutral.get("issues", []):
                items.append(_build_github_item(neutral, issue, source_identity, adapter))
    items.sort(key=lambda i: i["backlog_item_id"])
    for item in items:
        validate_normalized_item(item)
    return items


# ---------------------------------------------------------------------------
# Snapshot


def compute_snapshot_digest(sources: list[dict[str, Any]], observations: dict[str, dict[str, Any]], items: list[dict[str, Any]]) -> str:
    semantic_sources = []
    for source in sources:
        obs = observations[source["operation_id"]]
        semantic_sources.append({
            "adapter": obs.get("adapter"),
            "source_identity": obs.get("source_identity"),
            "request_digest": obs.get("request_digest"),
            "content_digest": obs.get("content_digest"),
        })
    semantic_sources.sort(key=canonical_json_bytes)
    item_descriptors = sorted(
        [{"backlog_item_id": i["backlog_item_id"], "item_digest": i["item_digest"]} for i in items],
        key=lambda d: d["backlog_item_id"],
    )
    core = {
        "schema": NORMALIZED_SNAPSHOT_SCHEMA,
        "sources": semantic_sources,
        "items": item_descriptors,
    }
    return sha256_hex(canonical_json_bytes(core))


def compute_snapshot_record_digest(payload: dict[str, Any]) -> str:
    core = {k: v for k, v in payload.items() if k not in ("snapshot_record_id", "snapshot_record_digest")}
    return sha256_hex(canonical_json_bytes(core))


def compute_snapshot_digest_from_snapshot(snapshot: dict[str, Any]) -> str:
    """Recompute the semantic snapshot_digest directly from a snapshot payload."""
    semantic_sources = []
    for prov in snapshot.get("sources", []):
        semantic_sources.append({
            "adapter": prov.get("adapter"),
            "source_identity": prov.get("source_identity"),
            "request_digest": prov.get("request_digest"),
            "content_digest": prov.get("content_digest"),
        })
    semantic_sources.sort(key=canonical_json_bytes)
    item_descriptors = sorted(
        [{"backlog_item_id": d["backlog_item_id"], "item_digest": d["item_digest"]} for d in snapshot.get("items", [])],
        key=lambda d: d["backlog_item_id"],
    )
    core = {
        "schema": NORMALIZED_SNAPSHOT_SCHEMA,
        "sources": semantic_sources,
        "items": item_descriptors,
    }
    return sha256_hex(canonical_json_bytes(core))


def validate_normalized_snapshot(snapshot: Any) -> None:
    _obj_fields(snapshot, SNAPSHOT_FIELDS, "normalized snapshot")
    if snapshot["schema"] != NORMALIZED_SNAPSHOT_SCHEMA:
        raise BacklogNormalizationInvalid("snapshot schema mismatch")
    if not isinstance(snapshot.get("snapshot_digest"), str) or not SHA256_HEX_RE.fullmatch(snapshot["snapshot_digest"]):
        raise BacklogNormalizationInvalid("snapshot_digest must be a 64-hex digest")
    if not isinstance(snapshot.get("snapshot_record_digest"), str) or not SHA256_HEX_RE.fullmatch(snapshot["snapshot_record_digest"]):
        raise BacklogNormalizationInvalid("snapshot_record_digest must be a 64-hex digest")
    if not isinstance(snapshot.get("snapshot_record_id"), str) or not re.fullmatch(r"^blsnaprec_[0-9a-f]{64}$", snapshot["snapshot_record_id"]):
        raise BacklogNormalizationInvalid("snapshot_record_id must match ^blsnaprec_[0-9a-f]{64}$")
    if isinstance(snapshot.get("source_count"), bool) or not isinstance(snapshot.get("source_count"), int) or snapshot["source_count"] < 0:
        raise BacklogNormalizationInvalid("source_count must be a non-negative integer")
    if isinstance(snapshot.get("item_count"), bool) or not isinstance(snapshot.get("item_count"), int) or snapshot["item_count"] < 1:
        raise BacklogNormalizationInvalid("item_count must be a positive integer")

    # Semantic snapshot_digest recomputation must match exactly.
    if compute_snapshot_digest_from_snapshot(snapshot) != snapshot["snapshot_digest"]:
        raise BacklogNormalizationInvalid("snapshot_digest does not recompute from semantic descriptors")

    # Source provenance exact shapes + deterministic ordering + no duplicates.
    sources = snapshot.get("sources")
    if not isinstance(sources, list) or len(sources) != snapshot["source_count"]:
        raise BacklogNormalizationInvalid("source_count mismatch")
    seen_sources: list[str] = []
    for prov in sources:
        _obj_fields(prov, ("operation_id", "observation_id", "observation_digest", "request_digest", "content_digest", "outcome", "adapter", "source_identity"), "source provenance")
        if not OPERATION_ID_RE.fullmatch(prov["operation_id"]):
            raise BacklogNormalizationInvalid("source provenance operation_id format")
        if not re.fullmatch(r"^srcobs_[0-9a-f]{64}$", prov["observation_id"]):
            raise BacklogNormalizationInvalid("source provenance observation_id format")
        for key in ("observation_digest", "request_digest", "content_digest"):
            if not SHA256_HEX_RE.fullmatch(prov[key]):
                raise BacklogNormalizationInvalid(f"source provenance {key} format")
        if prov["outcome"] not in ("changed", "unchanged"):
            raise BacklogNormalizationInvalid("source provenance outcome must be changed|unchanged")
        if not isinstance(prov.get("adapter"), str) or not prov["adapter"]:
            raise BacklogNormalizationInvalid("source provenance adapter must be non-empty")
        si = prov.get("source_identity")
        if not isinstance(si, dict) or sorted(si.keys()) != ["canonical_id", "kind"]:
            raise BacklogNormalizationInvalid("source provenance source_identity must be {kind, canonical_id}")
        seen_sources.append(prov["operation_id"])
    if len(seen_sources) != len(set(seen_sources)):
        raise BacklogNormalizationInvalid("duplicate source provenance operation_id")
    if sources != sorted(sources, key=canonical_json_bytes):
        raise BacklogNormalizationInvalid("sources must be deterministically sorted")

    # Item descriptors exact shapes + deterministic ordering + no duplicates.
    items = snapshot.get("items")
    if not isinstance(items, list) or len(items) != snapshot["item_count"]:
        raise BacklogNormalizationInvalid("item_count mismatch")
    seen_items: list[str] = []
    for desc in items:
        _obj_fields(desc, ("backlog_item_id", "item_digest", "artifact_id"), "snapshot item descriptor")
        if not BACKLOG_ITEM_ID_RE.fullmatch(desc["backlog_item_id"]):
            raise BacklogNormalizationInvalid("snapshot item backlog_item_id format")
        if not ITEM_DIGEST_RE.fullmatch(desc["item_digest"]):
            raise BacklogNormalizationInvalid("snapshot item item_digest format")
        expected_artifact = f"normalized-backlog/item/{desc['backlog_item_id']}/{desc['item_digest']}"
        if desc["artifact_id"] != expected_artifact:
            raise BacklogNormalizationInvalid("snapshot item artifact_id does not derive from id+digest")
        seen_items.append(desc["backlog_item_id"])
    if len(seen_items) != len(set(seen_items)):
        raise BacklogNormalizationInvalid("duplicate snapshot backlog_item_id")
    if [d["backlog_item_id"] for d in items] != sorted(seen_items):
        raise BacklogNormalizationInvalid("items must be deterministically sorted by backlog_item_id")

    # Record digest/id recomputation (after semantic structure).
    if snapshot["snapshot_record_digest"] != compute_snapshot_record_digest(snapshot):
        raise BacklogNormalizationInvalid("snapshot_record_digest does not recompute")
    if snapshot["snapshot_record_id"] != "blsnaprec_" + snapshot["snapshot_record_digest"]:
        raise BacklogNormalizationInvalid("snapshot_record_id does not derive from digest")


def _snapshot_payload(
    sources: list[dict[str, Any]],
    observations: dict[str, dict[str, Any]],
    items: list[dict[str, Any]],
    snapshot_digest: str,
) -> dict[str, Any]:
    provenance = []
    for source in sources:
        obs = observations[source["operation_id"]]
        provenance.append({
            "operation_id": obs.get("operation_id"),
            "observation_id": obs.get("observation_id"),
            "observation_digest": obs.get("observation_digest"),
            "request_digest": obs.get("request_digest"),
            "content_digest": obs.get("content_digest"),
            "outcome": obs.get("outcome"),
            "adapter": obs.get("adapter"),
            "source_identity": obs.get("source_identity"),
        })
    provenance.sort(key=canonical_json_bytes)
    item_descriptors = []
    for item in items:
        item_descriptors.append({
            "backlog_item_id": item["backlog_item_id"],
            "item_digest": item["item_digest"],
            "artifact_id": f"normalized-backlog/item/{item['backlog_item_id']}/{item['item_digest']}",
        })
    item_descriptors.sort(key=lambda d: d["backlog_item_id"])
    payload = {
        "schema": NORMALIZED_SNAPSHOT_SCHEMA,
        "snapshot_record_id": "",
        "snapshot_record_digest": "",
        "snapshot_digest": snapshot_digest,
        "source_count": len(provenance),
        "sources": provenance,
        "item_count": len(item_descriptors),
        "items": item_descriptors,
    }
    payload["snapshot_record_digest"] = compute_snapshot_record_digest(payload)
    payload["snapshot_record_id"] = "blsnaprec_" + payload["snapshot_record_digest"]
    return payload


# ---------------------------------------------------------------------------
# Primary API


def normalize_backlog_sources(
    campaign_dir,
    campaign_id,
    campaign_revision: int,
    sources: Any,
    previous_snapshot_digest: str | None = None,
) -> dict[str, Any]:
    """Normalize validated source observations into durable backlog items + snapshot."""
    if not isinstance(sources, list) or not sources:
        raise BacklogNormalizationInvalid("sources must be a non-empty list")
    if previous_snapshot_digest is not None and (
        not isinstance(previous_snapshot_digest, str) or not SHA256_HEX_RE.fullmatch(previous_snapshot_digest)
    ):
        raise BacklogNormalizationInvalid("previous_snapshot_digest must be null or a 64-hex digest")
    seen_ops: set[str] = set()
    for source in sources:
        if not isinstance(source, dict) or not isinstance(source.get("operation_id"), str):
            raise BacklogNormalizationInvalid("source operation_id required")
        if source["operation_id"] in seen_ops:
            raise BacklogNormalizationInvalid("duplicate operation_id in sources")
        seen_ops.add(source["operation_id"])

    # Validate ALL sources completely before the first write.
    observations: dict[str, dict[str, Any]] = {}
    for source in sources:
        obs, _ = _bind_source(campaign_dir, campaign_id, source)
        observations[source["operation_id"]] = obs

    # Build and validate all items before the first write.
    items = build_normalized_items(sources, observations)
    if not items:
        raise BacklogNormalizationInvalid("no normalized items produced")
    item_ids = [i["backlog_item_id"] for i in items]
    if len(item_ids) != len(set(item_ids)):
        raise BacklogNormalizationInvalid("duplicate backlog_item_id in one normalization request")

    snapshot_digest = compute_snapshot_digest(sources, observations, items)

    # Persist item artifacts.
    item_created = 0
    item_reused = 0
    for item in items:
        _policy_reject(item, "$")
        artifact_id = f"normalized-backlog/item/{item['backlog_item_id']}/{item['item_digest']}"
        try:
            result = intel.put_artifact(
                pathlib.Path(campaign_dir), campaign_id, campaign_revision, "backlog", artifact_id, item,
            )
        except intel.IntelligenceArtifactConflict as exc:
            raise BacklogNormalizationConflict(str(exc)) from exc
        status = result.get("status")
        if status == "created":
            item_created += 1
        else:
            item_reused += 1

    # Persist snapshot LAST (authoritative completion marker).
    snapshot_payload = _snapshot_payload(sources, observations, items, snapshot_digest)
    _policy_reject(snapshot_payload, "$")
    validate_normalized_snapshot(snapshot_payload)
    snapshot_artifact_id = f"normalized-backlog/snapshot/{snapshot_payload['snapshot_record_id']}"
    try:
        snap_result = intel.put_artifact(
            pathlib.Path(campaign_dir), campaign_id, campaign_revision, "backlog", snapshot_artifact_id, snapshot_payload,
        )
    except intel.IntelligenceArtifactConflict as exc:
        raise BacklogNormalizationConflict(str(exc)) from exc
    snap_status = snap_result.get("status")

    outcome = "changed"
    if previous_snapshot_digest is not None:
        outcome = "unchanged" if previous_snapshot_digest == snapshot_digest else "changed"

    return {
        "ok": True,
        "status": snap_status,
        "outcome": outcome,
        "snapshot_record_id": snapshot_payload["snapshot_record_id"],
        "snapshot_digest": snapshot_digest,
        "source_count": len(sources),
        "item_count": len(items),
        "item_artifacts_created": item_created,
        "item_artifacts_reused": item_reused,
        "snapshot_artifact_status": snap_status,
    }
