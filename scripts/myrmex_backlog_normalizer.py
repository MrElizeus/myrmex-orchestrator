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


def validate_normalized_item(item: Any) -> None:
    _obj_fields(item, NORMALIZED_ITEM_FIELDS, "normalized item")
    if item["schema"] != NORMALIZED_ITEM_SCHEMA:
        raise BacklogNormalizationInvalid("normalized item schema mismatch")
    if item["backlog_item_id"] != compute_backlog_item_id(
        item["source_adapter"], item["source_identity"], item["source_entity_id"]
    ):
        raise BacklogNormalizationInvalid("backlog_item_id does not recompute")
    if item["item_digest"] != compute_item_digest(item):
        raise BacklogNormalizationInvalid("item_digest does not recompute")
    if not isinstance(item["source_identity"], dict) or not item["source_identity"]:
        raise BacklogNormalizationInvalid("source_identity must be a non-empty object")
    if item["source_entity_type"] not in ("local-item", "github-issue"):
        raise BacklogNormalizationInvalid("unknown source_entity_type")
    for key in ("dependency_hints", "constraints", "context_constraints", "labels"):
        if not isinstance(item.get(key), list):
            raise BacklogNormalizationInvalid(f"{key} must be an array")
    for key in ("title",):
        if not isinstance(item.get(key), str) or not item[key]:
            raise BacklogNormalizationInvalid("title must be a non-empty string")
    if item.get("priority") is not None and not isinstance(item["priority"], str):
        raise BacklogNormalizationInvalid("priority must be string or null")
    if item.get("state") is not None and item["state"] not in ("open", "closed"):
        raise BacklogNormalizationInvalid("state must be open|closed|null")
    if item.get("group_ref") is not None:
        gr = item["group_ref"]
        if gr.get("kind") == "local-objective":
            _obj_fields(gr, GROUP_REF_LOCAL_FIELDS, "local group_ref")
        elif gr.get("kind") == "github-milestone":
            _obj_fields(gr, GROUP_REF_GITHUB_FIELDS, "github group_ref")
            if not isinstance(gr.get("number"), int) or gr["number"] < 1:
                raise BacklogNormalizationInvalid("github group_ref.number must be a positive integer")
        else:
            raise BacklogNormalizationInvalid("unknown group_ref kind")


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


def validate_normalized_snapshot(snapshot: Any) -> None:
    _obj_fields(snapshot, SNAPSHOT_FIELDS, "normalized snapshot")
    if snapshot["schema"] != NORMALIZED_SNAPSHOT_SCHEMA:
        raise BacklogNormalizationInvalid("snapshot schema mismatch")
    if snapshot["snapshot_record_digest"] != compute_snapshot_record_digest(snapshot):
        raise BacklogNormalizationInvalid("snapshot_record_digest does not recompute")
    if snapshot["snapshot_record_id"] != "blsnaprec_" + snapshot["snapshot_record_digest"]:
        raise BacklogNormalizationInvalid("snapshot_record_id does not derive from digest")
    if not isinstance(snapshot.get("sources"), list) or len(snapshot.get("sources", [])) != snapshot.get("source_count"):
        raise BacklogNormalizationInvalid("source_count mismatch")
    if not isinstance(snapshot.get("items"), list) or len(snapshot.get("items", [])) != snapshot.get("item_count"):
        raise BacklogNormalizationInvalid("item_count mismatch")


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
