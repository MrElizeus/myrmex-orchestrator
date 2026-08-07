#!/usr/bin/env python3
"""Deterministic local roadmap and manifest adapters for Myrmex P1-004.

Transforms Markdown roadmaps and JSON/YAML manifests into one neutral
representation (``myrmex.local-source-neutral/v1``) with order-independent
semantic identities and digests, then exposes a P1-003-compatible reader.

The module is stdlib-only: no requests, urllib, subprocess GitHub access,
PyYAML, repository/campaign writes, WU API, or plan activation.

Neutral representation marker (NOT a repository JSON Schema contract):
    myrmex.local-source-neutral/v1

Semantic identity rules:
  * objective_id = "srcobj_" + sha256(canonical_json(identity_core))
  * item_id     = "srcitem_" + sha256(canonical_json(identity_core))
  * identity_core prefers an explicit manifest id, else fallback
    title/objective identity.
  * identity normalization: NFKC, strip, collapse internal whitespace to one
    ASCII space, casefold.
  * NEVER use list index, heading ordinal, line number, JSON pointer, YAML
    position, or source path in semantic identity.

Semantic digest rules:
  * content_digest = SHA256(UTF-8 canonical JSON of the semantic source
    projection) where the projection excludes source_type, paths, locations,
    line numbers, pointers, ambiguity ordering, source ordering, and raw
    digest.
  * observed_version = "sha256:" + sha256(raw_bytes).

Parser ambiguity is explicit and deterministic; duplicates are never silently
renamed or resolved by source order.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import pathlib
import re
import sys
import unicodedata
from typing import Any, Callable


class LocalSourceError(Exception):
    """Base error for local-source adapters."""


try:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import myrmex_campaign_intelligence as intel  # type: ignore
except Exception as exc:  # pragma: no cover - import-time only
    raise LocalSourceError(
        "P1-002 safety backend is unavailable; local-source adapters fail closed"
    ) from exc


NEUTRAL_SCHEMA = "myrmex.local-source-neutral/v1"

SOURCE_TYPES = ("roadmap_markdown", "manifest_json", "manifest_yaml")
FORMAT_NAMES = ("markdown", "json", "yaml")
ADAPTER_IDS = {
    "markdown": "local-markdown-roadmap/v1",
    "json": "local-manifest-json/v1",
    "yaml": "local-manifest-yaml/v1",
}
SOURCE_KINDS = {
    "markdown": "local-roadmap",
    "json": "local-manifest",
    "yaml": "local-manifest",
}
SUFFIX_TO_FORMAT = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
}

SEMANTIC_OBJECTIVE_FIELDS = ("objective_id", "explicit_id", "title", "constraints", "source_location")
SEMANTIC_ITEM_FIELDS = (
    "item_id", "explicit_id", "objective_id", "title", "priority",
    "dependency_hints", "constraints", "source_location",
)
SEMANTIC_LOCATION_FIELDS = ("path", "locator_type", "locator")
SEMANTIC_AMBIGUITY_FIELDS = ("code", "entity_id", "locations")
SEMANTIC_TOP_FIELDS = ("schema", "source_type", "title", "objectives", "items", "constraints", "ambiguities")

AMBIGUITY_CODES = {
    "duplicate_objective_identity",
    "duplicate_item_identity",
    "conflicting_priority",
    "unresolved_objective_reference",
    "multiple_document_titles",
    "orphan_item_metadata",
}

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class LocalSourceMalformed(LocalSourceError):
    """Source content is syntactically invalid or uses unsupported syntax."""


class LocalSourcePolicyError(LocalSourceError):
    """Extracted content violates the secret/raw-content policy."""


class UnsupportedLocalSource(LocalSourceError):
    """Source format or path is not supported."""


def _policy_reject(value: Any, where: str = "$") -> None:
    """Apply the P1-002 secret/raw-content policy; never echo the value.

    Fails closed: if the P1-002 backend is unavailable the module could not
    have been imported successfully (see module import), so this helper can
    rely on ``intel`` being present.
    """
    try:
        intel.reject_secret_or_raw(value, where)
    except intel.IntelligencePayloadRejected as exc:
        raise LocalSourcePolicyError(
            f"local source content rejected by secret/raw-content policy at {where}"
        ) from exc


def canonical_json_bytes(value: Any) -> bytes:
    """Canonical UTF-8 JSON bytes (sort keys, compact separators, no NaN)."""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Unicode / identity normalization


def normalize_text(value: str) -> str:
    """NFKC + strip + collapse internal whitespace + casefold."""
    nfkc = unicodedata.normalize("NFKC", value)
    stripped = nfkc.strip()
    collapsed = re.sub(r"\s+", " ", stripped)
    return collapsed.casefold()


def display_text(value: str) -> str:
    """Preserve original trimmed display text (no casefold)."""
    return value.strip()


# ---------------------------------------------------------------------------
# Neutral representation validation


def _obj_fields(value: Any, allowed: tuple[str, ...], label: str, require_all: bool = False) -> None:
    if not isinstance(value, dict):
        raise LocalSourceMalformed(f"{label} must be an object")
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise LocalSourceMalformed(
            f"{label} has unknown fields: {', '.join(sorted(unknown))}"
        )
    if require_all:
        missing = [field for field in allowed if field not in value]
        if missing:
            raise LocalSourceMalformed(
                f"{label} is missing required fields: {', '.join(sorted(missing))}"
            )


def _validate_location(value: Any) -> None:
    _obj_fields(value, SEMANTIC_LOCATION_FIELDS, "source_location", require_all=True)
    if not isinstance(value.get("path"), str) or not value["path"]:
        raise LocalSourceMalformed("source_location.path must be a non-empty string")
    if value.get("locator_type") not in ("line", "json-pointer"):
        raise LocalSourceMalformed("source_location.locator_type must be line|json-pointer")
    if not isinstance(value.get("locator"), str) or not value["locator"]:
        raise LocalSourceMalformed("source_location.locator must be a non-empty string")


def _validate_ambiguity(value: Any) -> None:
    _obj_fields(value, SEMANTIC_AMBIGUITY_FIELDS, "ambiguity", require_all=True)
    if value.get("code") not in AMBIGUITY_CODES:
        raise LocalSourceMalformed(f"ambiguity has unknown code {value.get('code')!r}")
    if value.get("entity_id") is not None and not isinstance(value["entity_id"], str):
        raise LocalSourceMalformed("ambiguity.entity_id must be string or null")
    locations = value.get("locations")
    if not isinstance(locations, list):
        raise LocalSourceMalformed("ambiguity.locations must be an array")
    for loc in locations:
        _validate_location(loc)


def _validate_objective(value: Any) -> None:
    _obj_fields(value, SEMANTIC_OBJECTIVE_FIELDS, "objective", require_all=True)
    if not isinstance(value.get("objective_id"), str) or not value["objective_id"].startswith("srcobj_"):
        raise LocalSourceMalformed("objective.objective_id must be a srcobj_ id")
    if value.get("explicit_id") is not None and not isinstance(value["explicit_id"], str):
        raise LocalSourceMalformed("objective.explicit_id must be string or null")
    if not isinstance(value.get("title"), str):
        raise LocalSourceMalformed("objective.title must be a string")
    if not isinstance(value.get("constraints"), list) or not all(
        isinstance(x, str) for x in value["constraints"]
    ):
        raise LocalSourceMalformed("objective.constraints must be an array of strings")
    _validate_location(value["source_location"])


def _validate_item(value: Any) -> None:
    _obj_fields(value, SEMANTIC_ITEM_FIELDS, "item", require_all=True)
    if not isinstance(value.get("item_id"), str) or not value["item_id"].startswith("srcitem_"):
        raise LocalSourceMalformed("item.item_id must be a srcitem_ id")
    for key in ("explicit_id", "objective_id", "priority"):
        if value.get(key) is not None and not isinstance(value[key], str):
            raise LocalSourceMalformed(f"item.{key} must be string or null")
    if not isinstance(value.get("title"), str):
        raise LocalSourceMalformed("item.title must be a string")
    for key in ("dependency_hints", "constraints"):
        if not isinstance(value.get(key), list) or not all(
            isinstance(x, str) for x in value[key]
        ):
            raise LocalSourceMalformed(f"item.{key} must be an array of strings")
    _validate_location(value["source_location"])


def _recompute_objective_id(obj: dict[str, Any]) -> str:
    if obj.get("explicit_id") is not None:
        core = {"explicit_id": normalize_text(str(obj["explicit_id"]))}
    else:
        core = {"title": normalize_text(str(obj["title"]))}
    return "srcobj_" + sha256_hex(canonical_json_bytes(core))


def _recompute_item_id(item: dict[str, Any]) -> str:
    if item.get("explicit_id") is not None:
        core = {"explicit_id": normalize_text(str(item["explicit_id"]))}
    else:
        core = {"objective_id": item.get("objective_id"), "title": normalize_text(str(item["title"]))}
    return "srcitem_" + sha256_hex(canonical_json_bytes(core))


def validate_neutral_representation(neutral: Any) -> None:
    """Validate a neutral representation; raise LocalSourceMalformed on violation."""
    _obj_fields(neutral, SEMANTIC_TOP_FIELDS, "neutral representation", require_all=True)
    if neutral.get("schema") != NEUTRAL_SCHEMA:
        raise LocalSourceMalformed(f"schema must be {NEUTRAL_SCHEMA}")
    if neutral.get("source_type") not in SOURCE_TYPES:
        raise LocalSourceMalformed("source_type must be roadmap_markdown|manifest_json|manifest_yaml")
    if neutral.get("title") is not None and not isinstance(neutral["title"], str):
        raise LocalSourceMalformed("title must be string or null")
    if not isinstance(neutral.get("constraints"), list) or not all(
        isinstance(x, str) for x in neutral["constraints"]
    ):
        raise LocalSourceMalformed("constraints must be an array of strings")

    objectives = neutral.get("objectives")
    if not isinstance(objectives, list):
        raise LocalSourceMalformed("objectives must be an array")
    for obj in objectives:
        _validate_objective(obj)
        expected = _recompute_objective_id(obj)
        if obj["objective_id"] != expected:
            raise LocalSourceMalformed("objective.objective_id does not derive from identity core")

    items = neutral.get("items")
    if not isinstance(items, list):
        raise LocalSourceMalformed("items must be an array")
    objective_ids = {obj["objective_id"] for obj in objectives}
    for item in items:
        _validate_item(item)
        expected = _recompute_item_id(item)
        if item["item_id"] != expected:
            raise LocalSourceMalformed("item.item_id does not derive from identity core")
        if item.get("objective_id") is not None and item["objective_id"] not in objective_ids:
            raise LocalSourceMalformed("item.objective_id references an unknown objective")

    ambiguities = neutral.get("ambiguities")
    if not isinstance(ambiguities, list):
        raise LocalSourceMalformed("ambiguities must be an array")
    for amb in ambiguities:
        _validate_ambiguity(amb)


# ---------------------------------------------------------------------------
# Semantic digest


def _sorted_unique(values: list[str]) -> list[str]:
    return sorted({normalize_text(str(v)) for v in values})


def semantic_source_digest(neutral: dict[str, Any]) -> str:
    """SHA-256 of the canonical semantic source projection."""
    objectives = []
    for obj in neutral["objectives"]:
        objectives.append({
            "objective_id": obj["objective_id"],
            "title": normalize_text(obj["title"]),
            "constraints": _sorted_unique(obj["constraints"]),
        })
    objectives.sort(key=lambda o: o["objective_id"])

    items = []
    for item in neutral["items"]:
        items.append({
            "item_id": item["item_id"],
            "objective_id": item.get("objective_id"),
            "title": normalize_text(item["title"]),
            "priority": normalize_text(item["priority"]) if item.get("priority") is not None else None,
            "dependency_hints": _sorted_unique(item["dependency_hints"]),
            "constraints": _sorted_unique(item["constraints"]),
        })
    items.sort(key=lambda i: i["item_id"])

    projection = {
        "schema": NEUTRAL_SCHEMA,
        "title": normalize_text(neutral["title"]) if neutral.get("title") is not None else None,
        "objectives": objectives,
        "items": items,
        "constraints": _sorted_unique(neutral["constraints"]),
    }
    return sha256_hex(canonical_json_bytes(projection))


# ---------------------------------------------------------------------------
# Source location helpers


def _line_location(path: str, line: int) -> dict[str, Any]:
    return {"path": path, "locator_type": "line", "locator": str(line)}


# ---------------------------------------------------------------------------
# Markdown parsing


_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_H1_RE = re.compile(r"^\s*#\s+(.+?)\s*$")
_H2_RE = re.compile(r"^\s*##\s+(.+?)\s*$")
_H3_RE = re.compile(r"^\s*#{3,}\s+(.+?)\s*$")
_LIST_RE = re.compile(r"^\s*(?:[-*]|\d+\.)\s*(?:\[[ xX]\])?\s*(.+?)\s*$")
_META_RE = re.compile(
    r"^\s*(?:[-*]\s+)?(Priority|Depends on|Constraints):\s*(.+?)\s*$",
    re.IGNORECASE,
)


def _split_lines(text: str) -> list[str]:
    return text.splitlines()


def parse_markdown_roadmap(text: str, source_path: str) -> dict[str, Any]:
    """Parse a bounded Markdown roadmap into a neutral representation."""
    lines = _split_lines(text)
    titles: list[tuple[str, int]] = []
    objectives: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    constraints: list[str] = []
    ambiguities: list[dict[str, Any]] = []

    current_objective: dict[str, Any] | None = None
    current_item: dict[str, Any] | None = None
    in_fence: str | None = None
    fence_start: int | None = None

    objective_index: dict[str, int] = {}
    item_index: dict[str, int] = {}

    def add_ambiguity(code: str, entity_id: str | None, line: int) -> None:
        ambiguities.append({
            "code": code,
            "entity_id": entity_id,
            "locations": [_line_location(source_path, line)],
        })

    def make_objective(title: str, line: int) -> dict[str, Any]:
        core = {"title": normalize_text(title)}
        oid = "srcobj_" + sha256_hex(canonical_json_bytes(core))
        return {
            "objective_id": oid,
            "explicit_id": None,
            "title": display_text(title),
            "constraints": [],
            "source_location": _line_location(source_path, line),
        }

    def make_item(title: str, obj: dict[str, Any] | None, line: int) -> dict[str, Any]:
        core = {"objective_id": obj["objective_id"] if obj else None, "title": normalize_text(title)}
        iid = "srcitem_" + sha256_hex(canonical_json_bytes(core))
        return {
            "item_id": iid,
            "explicit_id": None,
            "objective_id": obj["objective_id"] if obj else None,
            "title": display_text(title),
            "priority": None,
            "dependency_hints": [],
            "constraints": [],
            "source_location": _line_location(source_path, line),
        }

    for lineno, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if in_fence is not None:
            m = _FENCE_RE.match(raw)
            if m and m.group(1) == in_fence:
                in_fence = None
            continue
        m = _FENCE_RE.match(raw)
        if m:
            in_fence = m.group(1)
            fence_start = lineno
            current_item = None
            continue
        if not stripped:
            continue

        m = _H1_RE.match(raw)
        if m:
            titles.append((display_text(m.group(1)), lineno))
            current_item = None
            continue
        m = _H2_RE.match(raw)
        if m:
            title = display_text(m.group(1))
            obj = make_objective(title, lineno)
            norm = normalize_text(title)
            if norm in objective_index:
                add_ambiguity("duplicate_objective_identity", None, lineno)
            else:
                objective_index[norm] = len(objectives)
                objectives.append(obj)
            current_objective = obj
            current_item = None
            continue
        m = _H3_RE.match(raw)
        if m:
            title = display_text(m.group(1))
            item = make_item(title, current_objective, lineno)
            norm = normalize_text(title)
            key = (current_objective["objective_id"] if current_objective else None, norm)
            if key in item_index:
                add_ambiguity("duplicate_item_identity", item["item_id"], lineno)
            else:
                item_index[key] = len(items)
                items.append(item)
            current_item = item
            continue
        m = _META_RE.match(raw)
        if m:
            key = m.group(1).strip().lower()
            value = m.group(2).strip()
            if key == "priority":
                if current_item is None:
                    add_ambiguity("orphan_item_metadata", None, lineno)
                else:
                    if current_item["priority"] is not None and current_item["priority"] != value:
                        add_ambiguity("conflicting_priority", current_item["item_id"], lineno)
                    else:
                        current_item["priority"] = value
            elif key == "depends on":
                if current_item is None:
                    add_ambiguity("orphan_item_metadata", None, lineno)
                else:
                    current_item["dependency_hints"].extend(
                        [part.strip() for part in value.split(",") if part.strip()]
                    )
            elif key == "constraints":
                if current_item is not None:
                    current_item["constraints"].extend(
                        [part.strip() for part in value.split(";") if part.strip()]
                    )
                elif current_objective is not None:
                    current_objective["constraints"].extend(
                        [part.strip() for part in value.split(";") if part.strip()]
                    )
                else:
                    constraints.extend([part.strip() for part in value.split(";") if part.strip()])
            continue
        m = _LIST_RE.match(raw)
        if m:
            title = display_text(m.group(1))
            item = make_item(title, current_objective, lineno)
            norm = normalize_text(title)
            key = (current_objective["objective_id"] if current_objective else None, norm)
            if key in item_index:
                add_ambiguity("duplicate_item_identity", item["item_id"], lineno)
            else:
                item_index[key] = len(items)
                items.append(item)
            current_item = item
            continue
        # Any other non-empty line: treat as document prose; clear item context.
        current_item = None

    if in_fence is not None:
        raise LocalSourceMalformed(
            f"unclosed fenced code block starting at line {fence_start}"
        )
    if len(titles) > 1:
        for title_text, title_line in titles[1:]:
            add_ambiguity("multiple_document_titles", None, title_line)

    return {
        "schema": NEUTRAL_SCHEMA,
        "source_type": "roadmap_markdown",
        "title": titles[0][0] if titles else None,
        "objectives": objectives,
        "items": items,
        "constraints": constraints,
        "ambiguities": ambiguities,
    }


# ---------------------------------------------------------------------------
# Manifest (JSON / YAML subset) parsing


_MANIFEST_TOP_FIELDS = {"title", "constraints", "objectives", "items"}
_MANIFEST_OBJECTIVE_FIELDS = {"id", "title", "constraints", "items"}
_MANIFEST_ITEM_FIELDS = {"id", "title", "objective", "priority", "depends_on", "constraints"}


def _normalize_priority(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise LocalSourceMalformed("priority must not be a boolean")
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    raise LocalSourceMalformed("priority must be a string, number, or null")


def _require_string_array(value: Any, field: str, allow_omitted: bool = True) -> list[str]:
    """Require an array of strings for one manifest field (no coercion)."""
    if value is None and allow_omitted:
        return []
    if not isinstance(value, list):
        raise LocalSourceMalformed(f"manifest {field} must be an array of strings")
    for entry in value:
        if not isinstance(entry, str):
            raise LocalSourceMalformed(f"manifest {field} entries must be strings")
    return list(value)


def parse_manifest_object(
    data: Any, source_path: str, source_type: str,
) -> dict[str, Any]:
    """Transform a parsed manifest object into a neutral representation."""
    if not isinstance(data, dict):
        raise LocalSourceMalformed("manifest top level must be an object")
    _obj_fields(data, tuple(_MANIFEST_TOP_FIELDS), "manifest")

    ambiguities: list[dict[str, Any]] = []
    objectives: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    constraints = _require_string_array(data.get("constraints"), "constraints")

    title = data.get("title")
    if title is not None and not isinstance(title, str):
        raise LocalSourceMalformed("manifest title must be a string or null")

    def add_ambiguity(code: str, entity_id: str | None, pointer: str) -> None:
        ambiguities.append({
            "code": code,
            "entity_id": entity_id,
            "locations": [{"path": source_path, "locator_type": "json-pointer", "locator": pointer}],
        })

    explicit_objective_index: dict[str, list[int]] = {}
    title_objective_index: dict[str, list[int]] = {}
    item_id_index: dict[str, int] = {}

    def make_objective(obj: dict[str, Any], pointer: str) -> dict[str, Any]:
        _obj_fields(obj, tuple(_MANIFEST_OBJECTIVE_FIELDS), "manifest objective")
        if not isinstance(obj.get("title"), str) or not obj["title"]:
            raise LocalSourceMalformed("manifest objective.title must be a non-empty string")
        explicit = obj.get("id")
        if explicit is not None and (not isinstance(explicit, str) or not explicit):
            raise LocalSourceMalformed("manifest objective.id must be a non-empty string or omitted")
        if explicit is not None:
            core = {"explicit_id": normalize_text(explicit)}
        else:
            core = {"title": normalize_text(obj["title"])}
        oid = "srcobj_" + sha256_hex(canonical_json_bytes(core))
        cstr = _require_string_array(obj.get("constraints"), "objective.constraints")
        return {
            "objective_id": oid,
            "explicit_id": explicit,
            "title": obj["title"],
            "constraints": cstr,
            "source_location": {"path": source_path, "locator_type": "json-pointer", "locator": pointer},
        }

    def make_item(item: dict[str, Any], obj: dict[str, Any] | None, pointer: str, *, nested: bool = False) -> dict[str, Any]:
        _obj_fields(item, tuple(_MANIFEST_ITEM_FIELDS), "manifest item")
        if not isinstance(item.get("title"), str) or not item["title"]:
            raise LocalSourceMalformed("manifest item.title must be a non-empty string")
        explicit = item.get("id")
        if explicit is not None and (not isinstance(explicit, str) or not explicit):
            raise LocalSourceMalformed("manifest item.id must be a non-empty string or omitted")
        if nested and "objective" in item:
            raise LocalSourceMalformed(
                "manifest item must not specify objective when nested inside an objective"
            )
        obj_ref = item.get("objective")
        if obj_ref is not None and not isinstance(obj_ref, str):
            raise LocalSourceMalformed("manifest item.objective must be a string, null, or omitted")
        if explicit is not None:
            core = {"explicit_id": normalize_text(explicit)}
        else:
            core = {"objective_id": obj["objective_id"] if obj else None, "title": normalize_text(item["title"])}
        iid = "srcitem_" + sha256_hex(canonical_json_bytes(core))
        depends = _require_string_array(item.get("depends_on"), "item.depends_on")
        cstr = _require_string_array(item.get("constraints"), "item.constraints")
        return {
            "item_id": iid,
            "explicit_id": explicit,
            "objective_id": obj["objective_id"] if obj else None,
            "title": item["title"],
            "priority": _normalize_priority(item.get("priority")),
            "dependency_hints": depends,
            "constraints": cstr,
            "source_location": {"path": source_path, "locator_type": "json-pointer", "locator": pointer},
        }

    objectives_raw = data.get("objectives")
    if objectives_raw is not None and not isinstance(objectives_raw, list):
        raise LocalSourceMalformed("manifest objectives must be an array")
    for idx, obj in enumerate(objectives_raw or []):
        pointer = f"/objectives/{idx}"
        parsed = make_objective(obj, pointer)
        norm_explicit = normalize_text(parsed["explicit_id"]) if parsed["explicit_id"] else None
        duplicate = False
        if norm_explicit and norm_explicit in explicit_objective_index:
            duplicate = True
        norm_title = normalize_text(parsed["title"])
        if norm_title in title_objective_index:
            duplicate = True
        if duplicate:
            add_ambiguity("duplicate_objective_identity", parsed["objective_id"], pointer)
        # Always index every objective (including duplicates) so reference
        # resolution can detect zero/multiple matches and never pick a
        # first-by-order winner.
        if norm_explicit:
            explicit_objective_index.setdefault(norm_explicit, []).append(len(objectives))
        title_objective_index.setdefault(norm_title, []).append(len(objectives))
        objectives.append(parsed)
        nested_items = obj.get("items")
        if nested_items is not None and not isinstance(nested_items, list):
            raise LocalSourceMalformed("manifest objective.items must be an array")
        for jdx, nitem in enumerate(nested_items or []):
            parsed_item = make_item(nitem, parsed, f"/objectives/{idx}/items/{jdx}", nested=True)
            if parsed_item["item_id"] in item_id_index:
                add_ambiguity("duplicate_item_identity", parsed_item["item_id"], f"/objectives/{idx}/items/{jdx}")
            else:
                item_id_index[parsed_item["item_id"]] = len(items)
                items.append(parsed_item)

    items_raw = data.get("items")
    if items_raw is not None and not isinstance(items_raw, list):
        raise LocalSourceMalformed("manifest items must be an array")
    for idx, item in enumerate(items_raw or []):
        pointer = f"/items/{idx}"
        if not isinstance(item, dict):
            raise LocalSourceMalformed("manifest item must be an object")
        obj_ref = item.get("objective")
        target_obj: dict[str, Any] | None = None
        if obj_ref is not None:
            if not isinstance(obj_ref, str):
                raise LocalSourceMalformed("manifest item.objective must be a string, null, or omitted")
            norm_ref = normalize_text(obj_ref)
            explicit_matches = explicit_objective_index.get(norm_ref, [])
            if len(explicit_matches) == 1:
                target_obj = objectives[explicit_matches[0]]
            elif len(explicit_matches) > 1:
                add_ambiguity("unresolved_objective_reference", None, pointer)
            else:
                title_matches = title_objective_index.get(norm_ref, [])
                if len(title_matches) == 1:
                    target_obj = objectives[title_matches[0]]
                else:
                    add_ambiguity("unresolved_objective_reference", None, pointer)
        parsed_item = make_item(item, target_obj, pointer)
        if parsed_item["item_id"] in item_id_index:
            add_ambiguity("duplicate_item_identity", parsed_item["item_id"], pointer)
        else:
            item_id_index[parsed_item["item_id"]] = len(items)
            items.append(parsed_item)

    return {
        "schema": NEUTRAL_SCHEMA,
        "source_type": source_type,
        "title": title,
        "objectives": objectives,
        "items": items,
        "constraints": constraints,
        "ambiguities": ambiguities,
    }


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LocalSourceMalformed(f"duplicate mapping key {key!r}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> Any:
    raise LocalSourceMalformed(f"non-standard JSON constant {value!r}")


def parse_json_manifest(text: str, source_path: str) -> dict[str, Any]:
    try:
        data = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except LocalSourceMalformed:
        raise
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise LocalSourceMalformed(f"malformed JSON manifest: {exc}") from exc
    return parse_manifest_object(data, source_path, "manifest_json")


# ---------------------------------------------------------------------------
# Safe YAML-subset parser (no PyYAML)


class _YamlSubsetError(Exception):
    pass


def _has_yaml_meta_token(raw: str) -> bool:
    """Return True when a YAML meta-syntax character appears outside quoted text."""
    in_single = False
    in_double = False
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch in ("&", "*", "!", "|", ">", "%") and not in_single and not in_double:
            return True
        i += 1
    return False


def _yaml_parse(text: str) -> Any:
    """Parse a deliberately narrow YAML subset into Python objects.

    Supported: UTF-8, spaces-only indentation, mappings, sequences (including
    sequences of mappings), nesting, plain scalars, double/single-quoted
    strings (which may contain literal & * ! | > % characters), null,
    true/false, integers, finite decimals, inline JSON []/{}, full-line
    comments.

    Rejected (raises _YamlSubsetError): tabs indentation, anchors, aliases,
    tags, merge keys, block scalars, directives, multiple document streams,
    duplicate mapping keys, uneven/non-two indentation jumps, malformed
    structure, non-finite numbers.
    """
    if "\t" in text:
        raise _YamlSubsetError("tabs are not allowed in YAML indentation")
    lines: list[tuple[int, str, int]] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        if raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent % 2 != 0:
            raise _YamlSubsetError(f"indentation must be a multiple of 2 (line {lineno})")
        lines.append((indent, raw.strip(), lineno))
    if not lines:
        return {}
    for _, raw, lineno in lines:
        if _has_yaml_meta_token(raw):
            raise _YamlSubsetError(
                f"unsupported YAML construct on line {lineno}: {raw.split(':', 1)[0]!r}"
            )
    if sum(1 for _, raw, _ in lines if raw == "---") > 1:
        raise _YamlSubsetError("multiple YAML document streams are not supported")

    def parse_block(idx: int, indent: int) -> tuple[Any, int]:
        if idx >= len(lines):
            return None, idx
        cur_indent, raw, lineno = lines[idx]
        if cur_indent < indent:
            return None, idx
        if cur_indent > indent:
            raise _YamlSubsetError(f"unexpected indentation jump at line {lineno}")
        if raw.startswith("- "):
            return _parse_sequence(idx, indent)
        return _parse_mapping(idx, indent)

    def _parse_scalar_line(raw: str, lineno: int) -> Any:
        if raw.startswith("[") or raw.startswith("{"):
            try:
                return json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
            except Exception as exc:
                raise _YamlSubsetError(f"invalid inline JSON value (line {lineno})") from exc
        return _yaml_scalar(raw, lineno)

    def _consume_value(rest: str, idx: int, indent: int, lineno: int) -> tuple[Any, int]:
        """Parse a mapping value: inline scalar/JSON or nested block.

        ``idx`` points at the CURRENT mapping/sequence entry line; the
        value's inline content is on that same line. An inline value
        therefore advances to ``idx + 1``; an empty value may consume a
        nested block starting at ``idx + 1`` which must advance EXACTLY two
        spaces from the parent indentation.
        """
        nxt = idx + 1
        if rest == "":
            if nxt < len(lines) and lines[nxt][0] == indent + 2:
                return parse_block(nxt, lines[nxt][0])
            if nxt < len(lines) and lines[nxt][0] > indent:
                raise _YamlSubsetError(
                    f"nested block must advance exactly two spaces (line {lineno})"
                )
            return None, nxt
        if rest.startswith("[") or rest.startswith("{"):
            try:
                return json.loads(rest, object_pairs_hook=_reject_duplicate_keys), nxt
            except Exception as exc:
                raise _YamlSubsetError(f"invalid inline JSON value (line {lineno})") from exc
        return _yaml_scalar(rest, lineno), nxt

    def _parse_mapping(idx: int, indent: int) -> tuple[dict[str, Any], int]:
        mapping: dict[str, Any] = {}
        while idx < len(lines):
            cur_indent, raw, lineno = lines[idx]
            if cur_indent < indent:
                break
            if cur_indent > indent:
                raise _YamlSubsetError(f"unexpected indentation jump at line {lineno}")
            if raw.startswith("- "):
                break
            if ":" not in raw:
                raise _YamlSubsetError(f"expected a mapping entry at line {lineno}")
            key_part, _, value_part = raw.partition(":")
            key = _yaml_scalar(key_part.strip(), lineno)
            if not isinstance(key, str):
                raise _YamlSubsetError(f"mapping key must be a string (line {lineno})")
            if key in mapping:
                raise _YamlSubsetError(f"duplicate mapping key {key!r} (line {lineno})")
            value, idx = _consume_value(value_part.strip(), idx, indent, lineno)
            mapping[key] = value
        return mapping, idx

    def _parse_sequence(idx: int, indent: int) -> tuple[list[Any], int]:
        seq: list[Any] = []
        while idx < len(lines):
            cur_indent, raw, lineno = lines[idx]
            if cur_indent < indent:
                break
            if cur_indent > indent:
                raise _YamlSubsetError(f"unexpected indentation jump at line {lineno}")
            if not (raw == "-" or raw.startswith("- ")):
                break
            rest = raw[1:].strip() if raw != "-" else ""
            if rest == "":
                # Nested block on following indented lines (must advance exactly two spaces).
                nxt = idx + 1
                if nxt < len(lines) and lines[nxt][0] == indent + 2:
                    value, idx = parse_block(nxt, lines[nxt][0])
                elif nxt < len(lines) and lines[nxt][0] > indent:
                    raise _YamlSubsetError(
                        f"nested block must advance exactly two spaces (line {lineno})"
                    )
                else:
                    value, idx = None, nxt
                seq.append(value)
                continue
            if ":" in rest and not rest.startswith(("[", "{", "'", '"')):
                # Sequence item is a mapping whose first entry is on this line.
                key_part, _, value_part = rest.partition(":")
                key = _yaml_scalar(key_part.strip(), lineno)
                if not isinstance(key, str):
                    raise _YamlSubsetError(f"mapping key must be a string (line {lineno})")
                item_map: dict[str, Any] = {key: None}
                value, idx = _consume_value(value_part.strip(), idx, indent + 2, lineno)
                item_map[key] = value
                # Continue sibling mapping entries at the item's indent.
                item_indent = indent + 2
                while idx < len(lines):
                    cind, craw, cline = lines[idx]
                    if cind < item_indent:
                        break
                    if cind > item_indent:
                        raise _YamlSubsetError(f"unexpected indentation jump at line {cline}")
                    if craw.startswith("- "):
                        break
                    if ":" not in craw:
                        raise _YamlSubsetError(f"expected a mapping entry at line {cline}")
                    kpart, _, vpart = craw.partition(":")
                    k2 = _yaml_scalar(kpart.strip(), cline)
                    if not isinstance(k2, str):
                        raise _YamlSubsetError(f"mapping key must be a string (line {cline})")
                    if k2 in item_map:
                        raise _YamlSubsetError(f"duplicate mapping key {k2!r} (line {cline})")
                    v2, idx = _consume_value(vpart.strip(), idx, item_indent, cline)
                    item_map[k2] = v2
                seq.append(item_map)
                continue
            value, idx = _parse_scalar_line(rest, lineno), idx + 1
            seq.append(value)
        return seq, idx

    value, _ = parse_block(0, lines[0][0])
    return value


def _yaml_scalar(raw: str, lineno: int) -> Any:
    if raw == "":
        return None
    if raw.startswith('"'):
        if not raw.endswith('"'):
            raise _YamlSubsetError(f"unterminated double-quoted string (line {lineno})")
        try:
            return json.loads(raw)
        except Exception as exc:
            raise _YamlSubsetError(f"invalid double-quoted string (line {lineno})") from exc
    if raw.startswith("'"):
        if not raw.endswith("'"):
            raise _YamlSubsetError(f"unterminated single-quoted string (line {lineno})")
        return raw[1:-1]
    low = raw.lower()
    if low in ("null", "~"):
        return None
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if re.fullmatch(r"[-+]?\d+", raw):
        return int(raw)
    if re.fullmatch(r"[-+]?(?:\d+\.\d*|\.\d+)(?:[eE][-+]?\d+)?", raw):
        value = float(raw)
        if value != value or value in (float("inf"), float("-inf")):
            raise _YamlSubsetError(f"non-finite number (line {lineno})")
        return value
    if raw.startswith(("[", "{")):
        try:
            return json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
        except Exception as exc:
            raise _YamlSubsetError(f"invalid inline JSON value (line {lineno})") from exc
    return raw


def parse_yaml_manifest(text: str, source_path: str) -> dict[str, Any]:
    try:
        data = _yaml_parse(text)
    except _YamlSubsetError as exc:
        raise LocalSourceMalformed(f"unsupported or malformed YAML: {exc}") from exc
    return parse_manifest_object(data, source_path, "manifest_yaml")


# ---------------------------------------------------------------------------
# Combined local source reader


def parse_local_source_bytes(
    raw_bytes: bytes, source_path: str, source_format: str,
) -> dict[str, Any]:
    """Parse raw bytes into a neutral representation for one source format."""
    if source_format not in FORMAT_NAMES:
        raise UnsupportedLocalSource(f"unsupported source format {source_format!r}")
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LocalSourceMalformed("source is not valid UTF-8") from exc
    if source_format == "markdown":
        neutral = parse_markdown_roadmap(text, source_path)
    elif source_format == "json":
        neutral = parse_json_manifest(text, source_path)
    else:
        neutral = parse_yaml_manifest(text, source_path)
    validate_neutral_representation(neutral)
    _policy_reject(neutral, "$")
    return neutral


def _format_from_path(source_path: str) -> str:
    suffix = pathlib.Path(source_path).suffix.lower()
    fmt = SUFFIX_TO_FORMAT.get(suffix)
    if fmt is None:
        raise UnsupportedLocalSource(f"unsupported source suffix {suffix!r}")
    return fmt


def _normalize_relative_path(source_path: str) -> str:
    """Lexically validate and normalize a repository-relative POSIX source path.

    Purely lexical: performs NO filesystem resolution (no resolve/exists/
    lstat/stat/open/read). Separator canonicalization happens BEFORE component
    validation so backslash traversal variants are rejected before any
    operation intent is created.
    """
    if not isinstance(source_path, str) or not source_path:
        raise LocalSourceError("source_path must be a non-empty string")
    canonical = source_path.replace("\\", "/")
    if canonical.startswith("/"):
        raise LocalSourceError("source_path must be repository-relative, not absolute")
    if re.match(r"^[A-Za-z]:", canonical):
        raise LocalSourceError("source_path must not be a Windows drive path")
    if canonical.startswith("//"):
        raise LocalSourceError("source_path must not be a UNC path")
    parts = pathlib.PurePosixPath(canonical).parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise LocalSourceError("source_path contains invalid or traversal components")
    if canonical in ("", "."):
        raise LocalSourceError("source_path must not resolve to the repository root itself")
    return canonical


def make_local_source_reader(repository_root: str | pathlib.Path) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Return a P1-003-compatible reader for local roadmap/manifest files."""
    root = pathlib.Path(repository_root).resolve()

    def reader(context: dict[str, Any]) -> dict[str, Any]:
        request = copy.deepcopy(context["request"])
        source_path = request["source_path"]
        source_format = request["source_format"]
        candidate = (root / source_path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise LocalSourceError("source path escapes the repository root") from exc
        # Symlink safety: reject final or intermediate symlinks.
        current = root
        for part in pathlib.PurePosixPath(source_path).parts:
            current = current / part
            if current.is_symlink():
                raise LocalSourceError(f"source path contains a symbolic link: {part}")
        if not candidate.exists():
            return {"status": "unavailable", "reason_code": "source_missing"}
        if not candidate.is_file():
            return {"status": "unavailable", "reason_code": "not_a_regular_file"}
        try:
            raw = candidate.read_bytes()
        except PermissionError:
            return {"status": "unavailable", "reason_code": "permission_denied"}
        except FileNotFoundError:
            return {"status": "unavailable", "reason_code": "source_missing"}
        raw_digest = sha256_hex(raw)
        observed_version = "sha256:" + raw_digest
        neutral = parse_local_source_bytes(raw, source_path, source_format)
        if neutral["ambiguities"]:
            return {
                "status": "ambiguous",
                "reason_code": "parser_ambiguity",
                "observed_version": observed_version,
            }
        return {
            "status": "observed",
            "observed_version": observed_version,
            "content_digest": semantic_source_digest(neutral),
        }

    return reader


def execute_local_import(
    campaign_dir: str | pathlib.Path,
    campaign_id: str,
    campaign_revision: int,
    idempotency_key: str,
    repository_root: str | pathlib.Path,
    source_path: str,
    source_format: str = "auto",
    previous_content_digest: str | None = None,
) -> dict[str, Any]:
    """Execute one state-first local import through the P1-003 lifecycle.

    This wrapper does NOT read repository content. It validates the path
    lexically, constructs source identity/adapter/request and the reader
    callback, and delegates to ``execute_import_operation``.
    """
    from myrmex_backlog_import import execute_import_operation  # type: ignore

    normalized = _normalize_relative_path(source_path)
    if source_format == "auto":
        fmt = _format_from_path(normalized)
    else:
        if source_format not in FORMAT_NAMES:
            raise UnsupportedLocalSource(f"unsupported source format {source_format!r}")
        fmt = source_format
    source_identity = {
        "kind": SOURCE_KINDS[fmt],
        "canonical_id": normalized,
    }
    adapter = ADAPTER_IDS[fmt]
    request = {
        "source_path": normalized,
        "source_format": fmt,
    }
    reader = make_local_source_reader(repository_root)
    return execute_import_operation(
        campaign_dir=campaign_dir,
        campaign_id=campaign_id,
        campaign_revision=campaign_revision,
        idempotency_key=idempotency_key,
        source_identity=source_identity,
        adapter=adapter,
        request=request,
        previous_content_digest=previous_content_digest,
        reader=reader,
    )
