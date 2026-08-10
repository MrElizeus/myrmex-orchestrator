#!/usr/bin/env python3
"""Reconstructible, non-authoritative campaign and cost observability."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import pathlib
import re
import tempfile
from typing import Any

import myrmex_campaign_intelligence as intel

CAMPAIGN_OBSERVATION_SCHEMA = "myrmex.campaign-observation/v1"
COST_OBSERVATION_SCHEMA = "myrmex.cost-observation/v1"
PROJECTION_SCHEMA = "myrmex.campaign-observability-projection/v1"
CAMPAIGN_ID_RE = re.compile(r"^camp-[a-z0-9][a-z0-9-]{4,60}$")
WU_ID_RE = re.compile(r"^WU-[A-Z0-9][A-Z0-9-]{0,30}$")
PLAN_ID_RE = re.compile(r"^plan_[0-9a-f]{64}$")
BACKLOG_ID_RE = re.compile(r"^(?:blsnaprec_|backlog_)[0-9a-f]{64}$")
DECISION_ID_RE = re.compile(r"^(?:schedule_|routemodel_|replandec_)[0-9a-f]{64}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
TERMINAL_WU = {"completed", "cancelled", "superseded"}
CAMPAIGN_AUTHORITY = {
    "scope": "campaign_observation_only", "dispatch": False,
    "activate_plan": False, "create_work_units": False,
    "repository_write": False, "commit": False, "push": False,
}
COST_AUTHORITY = {
    "scope": "cost_observation_only", "dispatch": False,
    "invoke_provider": False, "repository_write": False,
    "commit": False, "push": False,
}
PROJECTION_AUTHORITY = {
    "scope": "observability_projection_only", "dispatch": False,
    "activate_plan": False, "create_work_units": False,
    "invoke_agent": False, "invoke_provider": False,
    "repository_write": False, "commit": False, "push": False,
}


class ObservabilityError(Exception):
    pass


class ObservabilityInputInvalid(ObservabilityError):
    pass


class ObservabilityRecordConflict(ObservabilityError):
    pass


class ObservabilityProjectionInvalid(ObservabilityError):
    pass


def _canon(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canon(value)).hexdigest()


def _time(value: Any, label: str) -> dt.datetime:
    if not isinstance(value, str) or "T" not in value:
        raise ObservabilityInputInvalid(f"{label} must be a timezone-aware RFC3339 timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ObservabilityInputInvalid(f"{label} must be a timezone-aware RFC3339 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ObservabilityInputInvalid(f"{label} must include a timezone")
    return parsed


def _identity(body: dict[str, Any], prefix: str, id_field: str, digest_field: str) -> dict[str, Any]:
    digest = _sha(body)
    return {**body, id_field: prefix + digest, digest_field: digest}


def _validate_identity(record: Any, schema: str, prefix: str, id_field: str, digest_field: str) -> dict[str, Any]:
    if not isinstance(record, dict) or record.get("schema") != schema:
        raise ObservabilityInputInvalid("observation schema invalid")
    body = {key: value for key, value in record.items() if key not in {id_field, digest_field}}
    digest = _sha(body)
    if record.get(digest_field) != digest or record.get(id_field) != prefix + digest:
        raise ObservabilityInputInvalid("observation digest identity invalid")
    return record


def _oroot(campaign_dir: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(campaign_dir) / "observability"


def _ensure_dir(path: pathlib.Path) -> None:
    if path.is_symlink():
        raise ObservabilityError(f"unsafe symbolic-link directory: {path.name}")
    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise ObservabilityError(f"observability path is not a directory: {path.name}")
    os.chmod(path, 0o700)


def _write(path: pathlib.Path, value: dict[str, Any], *, replace: bool = False) -> None:
    _ensure_dir(path.parent)
    if path.is_symlink():
        raise ObservabilityError(f"unsafe symbolic-link file: {path.name}")
    payload = _canon(value) + b"\n"
    if path.exists() and not replace:
        if path.read_bytes() == payload:
            return
        raise ObservabilityRecordConflict(f"immutable observability record conflicts: {path.name}")
    fd, temp_name = tempfile.mkstemp(prefix=".observability-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload); handle.flush(); os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _read_json(path: pathlib.Path, label: str) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ObservabilityProjectionInvalid(f"{label} unavailable or unsafe")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ObservabilityProjectionInvalid(f"{label} is not valid JSON") from error


def _events(campaign_dir: pathlib.Path, campaign_id: str) -> tuple[list[dict[str, Any]], str]:
    path = pathlib.Path(campaign_dir) / "events.jsonl"
    if not path.exists():
        return [], _sha([])
    if path.is_symlink() or not path.is_file():
        raise ObservabilityInputInvalid("campaign event log unavailable or unsafe")
    result = []
    for sequence, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError as error:
            raise ObservabilityInputInvalid("campaign event log contains invalid JSON") from error
        if not isinstance(event, dict) or event.get("campaign_id") != campaign_id or not isinstance(event.get("event_type"), str):
            raise ObservabilityInputInvalid("campaign event identity invalid")
        _time(event.get("timestamp"), "event timestamp")
        result.append({
            "sequence": sequence, "timestamp": event["timestamp"],
            "event_type": event["event_type"], "event_digest": _sha(event),
        })
    return result, _sha([event["event_digest"] for event in result])


def _artifacts(campaign_dir: pathlib.Path, campaign_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    path = pathlib.Path(campaign_dir) / "intelligence" / "artifacts"
    if not path.exists():
        return [], [], _sha([])
    if path.is_symlink() or not path.is_dir():
        raise ObservabilityInputInvalid("intelligence artifact store unavailable or unsafe")
    index = []
    payloads = []
    for artifact_path in sorted(path.glob("*.json")):
        try:
            envelope = intel._read_artifact_file(artifact_path, campaign_id)
        except intel.IntelligenceStoreError as error:
            raise ObservabilityInputInvalid("intelligence artifact store contains a corrupt artifact") from error
        index.append({
            "artifact_id": envelope["artifact_id"], "kind": envelope["kind"],
            "artifact_digest": envelope["artifact_digest"], "payload_digest": envelope["payload_digest"],
            "observed_campaign_revision": envelope["observed_campaign_revision"], "created_at": envelope["created_at"],
        })
        payloads.append(envelope["payload"])
    index.sort(key=lambda item: (item["artifact_id"], item["artifact_digest"]))
    return index, payloads, _sha(index)


def _collect_correlations(value: Any, key: str | None, sets: dict[str, set[str]]) -> None:
    if isinstance(value, dict):
        for child_key, child in value.items():
            _collect_correlations(child, child_key, sets)
    elif isinstance(value, list):
        for child in value:
            _collect_correlations(child, key, sets)
    elif isinstance(value, str):
        if PLAN_ID_RE.fullmatch(value): sets["plans"].add(value)
        if BACKLOG_ID_RE.fullmatch(value): sets["backlog"].add(value)
        if DECISION_ID_RE.fullmatch(value): sets["decisions"].add(value)
        if WU_ID_RE.fullmatch(value): sets["work_units"].add(value)
        if key and (key.endswith("task_id") or key.endswith("task_ids")) and value:
            sets["tasks"].add(value)


def _correlations(campaign: dict[str, Any], artifact_payloads: list[dict[str, Any]]) -> dict[str, Any]:
    sets = {name: set() for name in ("plans", "backlog", "decisions", "work_units", "tasks")}
    for payload in artifact_payloads:
        _collect_correlations(payload, None, sets)
    work_units = []
    for wu in sorted(campaign["work_units"], key=lambda item: item["id"]):
        sets["work_units"].add(wu["id"])
        tasks = sorted(item for item in wu.get("task_ids", []) if isinstance(item, str) and item)
        sets["tasks"].update(tasks)
        order = wu.get("work_order") if isinstance(wu.get("work_order"), dict) else {}
        provenance = order.get("plan_provenance") if isinstance(order.get("plan_provenance"), dict) else {}
        plan_id = provenance.get("plan_revision_id")
        if not isinstance(plan_id, str) or not PLAN_ID_RE.fullmatch(plan_id): plan_id = None
        if plan_id: sets["plans"].add(plan_id)
        backlog_ids = []
        for row in order.get("backlog_provenance", []) if isinstance(order.get("backlog_provenance"), list) else []:
            if isinstance(row, dict):
                for field in ("snapshot_record_id", "backlog_item_id"):
                    value = row.get(field)
                    if isinstance(value, str) and BACKLOG_ID_RE.fullmatch(value):
                        sets["backlog"].add(value); backlog_ids.append(value)
        work_units.append({
            "work_unit_id": wu["id"], "status": wu["status"], "phase": wu["phase"],
            "plan_revision_id": plan_id, "backlog_record_ids": sorted(set(backlog_ids)), "task_ids": tasks,
        })
    return {
        "backlog_record_ids": sorted(sets["backlog"]), "plan_revision_ids": sorted(sets["plans"]),
        "work_units": work_units, "task_ids": sorted(sets["tasks"]), "decision_ids": sorted(sets["decisions"]),
    }


def _critical_path(campaign: dict[str, Any]) -> dict[str, Any]:
    by_id = {wu["id"]: wu for wu in campaign["work_units"]}
    successors = {wu_id: [] for wu_id in by_id}
    for wu in campaign["work_units"]:
        for dependency in wu.get("dependencies", []):
            if dependency not in by_id:
                raise ObservabilityInputInvalid("campaign dependency is missing")
            successors[dependency].append(wu["id"])
    memo: dict[str, list[str]] = {}
    visiting: set[str] = set()
    def visit(wu_id: str) -> list[str]:
        if wu_id in memo: return memo[wu_id]
        if wu_id in visiting: raise ObservabilityInputInvalid("campaign DAG contains a cycle")
        visiting.add(wu_id)
        if by_id[wu_id]["status"] in TERMINAL_WU:
            path: list[str] = []
        else:
            children = [visit(child) for child in sorted(successors[wu_id])]
            child_path = sorted(children, key=lambda item: (-len(item), item))[0] if children else []
            path = [wu_id, *child_path]
        visiting.remove(wu_id); memo[wu_id] = path; return path
    candidates = [visit(wu_id) for wu_id in sorted(by_id)]
    selected = sorted(candidates, key=lambda item: (-len(item), item))[0] if candidates else []
    return {"length": len(selected), "work_unit_ids": selected}


def _blockers(campaign: dict[str, Any], observed: dt.datetime) -> list[dict[str, Any]]:
    values = list(campaign.get("blockers", []))
    values.extend(wu["blocker"] for wu in campaign["work_units"] if isinstance(wu.get("blocker"), dict))
    unique: dict[str, dict[str, Any]] = {}
    for blocker in values:
        if not isinstance(blocker, dict):
            raise ObservabilityInputInvalid("campaign blocker invalid")
        unique[_sha(blocker)] = blocker
    result = []
    for blocker in unique.values():
        created = _time(blocker.get("created_at"), "blocker created_at")
        if created > observed:
            raise ObservabilityInputInvalid("blocker created_at is after observation")
        wu_id = blocker.get("work_unit_id")
        if wu_id is not None and (not isinstance(wu_id, str) or not WU_ID_RE.fullmatch(wu_id)):
            raise ObservabilityInputInvalid("blocker work_unit_id invalid")
        result.append({
            "blocker_type": str(blocker.get("type") or "unknown"), "work_unit_id": wu_id,
            "created_at": blocker["created_at"], "age_seconds": int((observed - created).total_seconds()),
            "message_digest": _sha(str(blocker.get("message") or "")),
        })
    return sorted(result, key=lambda item: (item["created_at"], item["blocker_type"], item["work_unit_id"] or ""))


def build_campaign_observation(campaign_dir: pathlib.Path, campaign: dict[str, Any], observed_at: str) -> dict[str, Any]:
    if not isinstance(campaign, dict) or not CAMPAIGN_ID_RE.fullmatch(str(campaign.get("id", ""))):
        raise ObservabilityInputInvalid("campaign identity invalid")
    revision = campaign.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ObservabilityInputInvalid("campaign revision invalid")
    observed = _time(observed_at, "observed_at")
    events, event_digest = _events(campaign_dir, campaign["id"])
    artifacts, payloads, artifact_digest = _artifacts(campaign_dir, campaign["id"])
    counts: dict[str, int] = {}
    for wu in campaign.get("work_units", []):
        if not isinstance(wu, dict) or not WU_ID_RE.fullmatch(str(wu.get("id", ""))):
            raise ObservabilityInputInvalid("campaign work unit invalid")
        counts[wu["status"]] = counts.get(wu["status"], 0) + 1
    total = len(campaign["work_units"]); completed = counts.get("completed", 0)
    body = {
        "schema": CAMPAIGN_OBSERVATION_SCHEMA, "campaign_id": campaign["id"],
        "campaign_revision": revision, "observed_at": observed_at,
        "source_index": {
            "campaign_state_digest": _sha(campaign), "event_log_digest": event_digest,
            "intelligence_artifact_digest": artifact_digest, "events": events, "artifacts": artifacts,
        },
        "correlations": _correlations(campaign, payloads),
        "progress": {
            "campaign_status": campaign["status"], "total_work_units": total,
            "completed_work_units": completed,
            "completion_basis_points": 10000 if total == 0 and campaign["status"] == "completed" else (completed * 10000 // total if total else 0),
            "status_counts": {key: counts[key] for key in sorted(counts)},
        },
        "critical_path": _critical_path(campaign), "blockers": _blockers(campaign, observed),
        "authority": dict(CAMPAIGN_AUTHORITY),
    }
    return _identity(body, "campobs_", "observation_id", "observation_digest")


def validate_campaign_observation(record: Any) -> dict[str, Any]:
    value = _validate_identity(record, CAMPAIGN_OBSERVATION_SCHEMA, "campobs_", "observation_id", "observation_digest")
    expected = {"schema", "observation_id", "observation_digest", "campaign_id", "campaign_revision", "observed_at", "source_index", "correlations", "progress", "critical_path", "blockers", "authority"}
    if set(value) != expected or not CAMPAIGN_ID_RE.fullmatch(str(value.get("campaign_id", ""))) or isinstance(value.get("campaign_revision"), bool) or not isinstance(value.get("campaign_revision"), int) or value["campaign_revision"] < 1 or not isinstance(value.get("source_index"), dict) or value.get("authority") != CAMPAIGN_AUTHORITY:
        raise ObservabilityInputInvalid("campaign observation fields invalid")
    _time(value.get("observed_at"), "campaign observation observed_at")
    source = value["source_index"]
    if set(source) != {"campaign_state_digest", "event_log_digest", "intelligence_artifact_digest", "events", "artifacts"}:
        raise ObservabilityInputInvalid("campaign observation source index fields invalid")
    for field in ("campaign_state_digest", "event_log_digest", "intelligence_artifact_digest"):
        if not isinstance(source.get(field), str) or not DIGEST_RE.fullmatch(source[field]):
            raise ObservabilityInputInvalid("campaign observation source digest invalid")
    events = source.get("events")
    if not isinstance(events, list) or any(not isinstance(row, dict) or set(row) != {"sequence", "timestamp", "event_type", "event_digest"} for row in events):
        raise ObservabilityInputInvalid("campaign observation event index invalid")
    for row in events:
        _time(row["timestamp"], "indexed event timestamp")
        if isinstance(row["sequence"], bool) or not isinstance(row["sequence"], int) or row["sequence"] < 0 or not isinstance(row["event_type"], str) or not row["event_type"] or not isinstance(row["event_digest"], str) or not DIGEST_RE.fullmatch(row["event_digest"]):
            raise ObservabilityInputInvalid("campaign observation event descriptor invalid")
    if [row["sequence"] for row in events] != sorted({row["sequence"] for row in events}) or source["event_log_digest"] != _sha([row["event_digest"] for row in events]):
        raise ObservabilityInputInvalid("campaign observation event digest index invalid")
    artifacts = source.get("artifacts")
    if not isinstance(artifacts, list) or artifacts != sorted(artifacts, key=lambda row: (row.get("artifact_id", ""), row.get("artifact_digest", ""))) or source["intelligence_artifact_digest"] != _sha(artifacts):
        raise ObservabilityInputInvalid("campaign observation artifact digest index invalid")
    critical = value.get("critical_path")
    if not isinstance(critical, dict) or set(critical) != {"length", "work_unit_ids"} or critical["length"] != len(critical["work_unit_ids"]):
        raise ObservabilityInputInvalid("campaign observation critical path invalid")
    return value


def build_cost_observation(campaign_id: str, campaign_revision: int, source: Any) -> dict[str, Any]:
    if not CAMPAIGN_ID_RE.fullmatch(campaign_id) or isinstance(campaign_revision, bool) or not isinstance(campaign_revision, int) or campaign_revision < 1:
        raise ObservabilityInputInvalid("cost observation campaign identity invalid")
    if not isinstance(source, dict) or set(source) != {"observed_at", "correlation", "source_kind", "source_digest", "incurred_usd", "incurred_unknown_reason", "projected_remaining_usd", "remaining_unknown_reason"}:
        raise ObservabilityInputInvalid("cost observation input fields invalid")
    _time(source["observed_at"], "cost observed_at")
    correlation = source["correlation"]
    if not isinstance(correlation, dict) or set(correlation) != {"plan_revision_id", "work_unit_id", "task_id", "route_model_decision_id"}:
        raise ObservabilityInputInvalid("cost correlation fields invalid")
    patterns = {"plan_revision_id": PLAN_ID_RE, "work_unit_id": WU_ID_RE, "route_model_decision_id": re.compile(r"^routemodel_[0-9a-f]{64}$")}
    for field, pattern in patterns.items():
        value = correlation[field]
        if value is not None and (not isinstance(value, str) or not pattern.fullmatch(value)):
            raise ObservabilityInputInvalid(f"cost correlation {field} invalid")
    if correlation["task_id"] is not None and (not isinstance(correlation["task_id"], str) or not correlation["task_id"]):
        raise ObservabilityInputInvalid("cost correlation task_id invalid")
    if source["source_kind"] not in {"provider_receipt", "task_receipt", "operator_projection", "unavailable"}:
        raise ObservabilityInputInvalid("cost source_kind invalid")
    if source["source_digest"] is not None and (not isinstance(source["source_digest"], str) or not DIGEST_RE.fullmatch(source["source_digest"])):
        raise ObservabilityInputInvalid("cost source_digest invalid")
    for value_field, reason_field in (("incurred_usd", "incurred_unknown_reason"), ("projected_remaining_usd", "remaining_unknown_reason")):
        value, reason = source[value_field], source[reason_field]
        if value is None:
            if not isinstance(reason, str) or not reason: raise ObservabilityInputInvalid(f"{reason_field} required for unknown cost")
        else:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or reason is not None:
                raise ObservabilityInputInvalid(f"{value_field}/{reason_field} invalid")
    body = {"schema": COST_OBSERVATION_SCHEMA, "campaign_id": campaign_id, "campaign_revision": campaign_revision, **source, "authority": dict(COST_AUTHORITY)}
    return _identity(body, "costobs_", "observation_id", "observation_digest")


def validate_cost_observation(record: Any) -> dict[str, Any]:
    value = _validate_identity(record, COST_OBSERVATION_SCHEMA, "costobs_", "observation_id", "observation_digest")
    expected_fields = {"schema", "observation_id", "observation_digest", "campaign_id", "campaign_revision", "observed_at", "correlation", "source_kind", "source_digest", "incurred_usd", "incurred_unknown_reason", "projected_remaining_usd", "remaining_unknown_reason", "authority"}
    if set(value) != expected_fields or value.get("authority") != COST_AUTHORITY:
        raise ObservabilityInputInvalid("cost observation authority invalid")
    rebuilt = build_cost_observation(value["campaign_id"], value["campaign_revision"], {key: value[key] for key in ("observed_at", "correlation", "source_kind", "source_digest", "incurred_usd", "incurred_unknown_reason", "projected_remaining_usd", "remaining_unknown_reason")})
    if rebuilt != value:
        raise ObservabilityInputInvalid("cost observation differs from canonical reconstruction")
    return value


def _records(campaign_dir: pathlib.Path, kind: str) -> list[dict[str, Any]]:
    directory = _oroot(campaign_dir) / kind
    if not directory.exists(): return []
    if directory.is_symlink() or not directory.is_dir(): raise ObservabilityProjectionInvalid(f"{kind} observation directory unsafe")
    validator = validate_campaign_observation if kind == "campaign" else validate_cost_observation
    records = []
    seen = set()
    for path in sorted(directory.glob("*.json")):
        record = validator(_read_json(path, f"{kind} observation"))
        if path.name != record["observation_id"] + ".json" or record["observation_id"] in seen:
            raise ObservabilityProjectionInvalid(f"{kind} observation storage identity invalid")
        seen.add(record["observation_id"]); records.append(record)
    return sorted(records, key=lambda item: (item["observed_at"], item["observation_id"]))


def _cost_projection(records: list[dict[str, Any]]) -> dict[str, Any]:
    known_incurred = sum(record["incurred_usd"] for record in records if record["incurred_usd"] is not None)
    known_remaining = sum(record["projected_remaining_usd"] for record in records if record["projected_remaining_usd"] is not None)
    incurred_unknown = sum(record["incurred_usd"] is None for record in records)
    remaining_unknown = sum(record["projected_remaining_usd"] is None for record in records)
    total_incurred = None if not records or incurred_unknown else known_incurred
    total_remaining = None if not records or remaining_unknown else known_remaining
    return {
        "observation_count": len(records), "known_incurred_usd": known_incurred,
        "incurred_unknown_count": incurred_unknown, "total_incurred_usd": total_incurred,
        "known_projected_remaining_usd": known_remaining, "remaining_unknown_count": remaining_unknown,
        "projected_remaining_usd": total_remaining,
        "projected_total_usd": None if total_incurred is None or total_remaining is None else total_incurred + total_remaining,
    }


def _projection(campaign_dir: pathlib.Path, campaign_id: str) -> dict[str, Any]:
    campaigns = _records(campaign_dir, "campaign")
    costs = _records(campaign_dir, "cost")
    if not campaigns: raise ObservabilityProjectionInvalid("no immutable campaign observation exists")
    if any(record["campaign_id"] != campaign_id for record in [*campaigns, *costs]):
        raise ObservabilityProjectionInvalid("observation belongs to another campaign")
    latest = campaigns[-1]
    timeline = sorted(
        [{"kind": "campaign", "observed_at": row["observed_at"], "observation_id": row["observation_id"], "observation_digest": row["observation_digest"]} for row in campaigns]
        + [{"kind": "cost", "observed_at": row["observed_at"], "observation_id": row["observation_id"], "observation_digest": row["observation_digest"]} for row in costs],
        key=lambda row: (row["observed_at"], row["kind"], row["observation_id"]),
    )
    body = {
        "schema": PROJECTION_SCHEMA, "campaign_id": campaign_id,
        "latest_campaign_observation_id": latest["observation_id"],
        "latest_campaign_revision": latest["campaign_revision"],
        "campaign_observation_count": len(campaigns), "cost_observation_count": len(costs),
        "source_index": latest["source_index"], "correlations": latest["correlations"],
        "progress": latest["progress"], "critical_path": latest["critical_path"], "blockers": latest["blockers"],
        "cost": _cost_projection(costs), "timeline": timeline, "authority": dict(PROJECTION_AUTHORITY),
    }
    return _identity(body, "obsproj_", "projection_id", "projection_digest")


def validate_projection(projection: Any) -> dict[str, Any]:
    value = _validate_identity(projection, PROJECTION_SCHEMA, "obsproj_", "projection_id", "projection_digest")
    if value.get("authority") != PROJECTION_AUTHORITY or not CAMPAIGN_ID_RE.fullmatch(str(value.get("campaign_id", ""))):
        raise ObservabilityProjectionInvalid("observability projection fields invalid")
    return value


def rebuild_projection(campaign_dir: pathlib.Path, campaign_id: str) -> dict[str, Any]:
    with intel.intelligence_lock(pathlib.Path(campaign_dir)):
        projection = _projection(campaign_dir, campaign_id)
        _write(_oroot(campaign_dir) / "projection.json", projection, replace=True)
    return projection


def record_campaign_observation(campaign_dir: pathlib.Path, campaign: dict[str, Any], observed_at: str) -> dict[str, Any]:
    with intel.intelligence_lock(pathlib.Path(campaign_dir)):
        record = build_campaign_observation(campaign_dir, campaign, observed_at)
        validate_campaign_observation(record)
        _write(_oroot(campaign_dir) / "campaign" / (record["observation_id"] + ".json"), record)
        projection = _projection(campaign_dir, campaign["id"])
        _write(_oroot(campaign_dir) / "projection.json", projection, replace=True)
    return {"status": "RECORDED", "observation": record, "projection": projection}


def record_cost_observation(campaign_dir: pathlib.Path, campaign_id: str, campaign_revision: int, source: Any) -> dict[str, Any]:
    record = build_cost_observation(campaign_id, campaign_revision, source)
    validate_cost_observation(record)
    with intel.intelligence_lock(pathlib.Path(campaign_dir)):
        _write(_oroot(campaign_dir) / "cost" / (record["observation_id"] + ".json"), record)
        projection = _projection(campaign_dir, campaign_id)
        _write(_oroot(campaign_dir) / "projection.json", projection, replace=True)
    return {"status": "RECORDED", "observation": record, "projection": projection}


def read_projection(campaign_dir: pathlib.Path, campaign_id: str) -> dict[str, Any]:
    projection = validate_projection(_read_json(_oroot(campaign_dir) / "projection.json", "observability projection"))
    if projection["campaign_id"] != campaign_id: raise ObservabilityProjectionInvalid("projection belongs to another campaign")
    return projection


def consistency_audit(campaign_dir: pathlib.Path, campaign: dict[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    try:
        stored = read_projection(campaign_dir, campaign["id"])
        expected = _projection(campaign_dir, campaign["id"])
        checks.append({"check": "projection_matches_records", "status": "PASS" if stored == expected else "FAIL"})
    except ObservabilityError:
        return {"schema": "myrmex.observability-consistency/v1", "campaign_id": campaign["id"], "status": "FAIL", "checks": [{"check": "projection_valid", "status": "FAIL"}]}
    latest = _records(campaign_dir, "campaign")[-1]
    revision_status = "PASS" if latest["campaign_revision"] == campaign["revision"] and latest["source_index"]["campaign_state_digest"] == _sha(campaign) else ("STALE" if latest["campaign_revision"] < campaign["revision"] else "FAIL")
    checks.append({"check": "campaign_state", "status": revision_status})
    current_events, _ = _events(campaign_dir, campaign["id"])
    observed_events = latest["source_index"]["events"]
    event_status = "FAIL" if current_events[:len(observed_events)] != observed_events else ("STALE" if len(current_events) > len(observed_events) else "PASS")
    checks.append({"check": "event_digest_index", "status": event_status})
    current_artifacts, _, _ = _artifacts(campaign_dir, campaign["id"])
    observed_artifacts = latest["source_index"]["artifacts"]
    observed_map = {row["artifact_id"]: row for row in observed_artifacts}; current_map = {row["artifact_id"]: row for row in current_artifacts}
    artifact_status = "FAIL" if any(current_map.get(key) != value for key, value in observed_map.items()) else ("STALE" if current_map != observed_map else "PASS")
    checks.append({"check": "artifact_digest_index", "status": artifact_status})
    statuses = {row["status"] for row in checks}
    status = "FAIL" if "FAIL" in statuses else "STALE" if "STALE" in statuses else "PASS"
    return {"schema": "myrmex.observability-consistency/v1", "campaign_id": campaign["id"], "status": status, "checks": checks}


def status(campaign_dir: pathlib.Path, campaign: dict[str, Any]) -> dict[str, Any]:
    return {"projection": read_projection(campaign_dir, campaign["id"]), "consistency": consistency_audit(campaign_dir, campaign)}


def timeline(campaign_dir: pathlib.Path, campaign_id: str) -> list[dict[str, Any]]:
    return read_projection(campaign_dir, campaign_id)["timeline"]
