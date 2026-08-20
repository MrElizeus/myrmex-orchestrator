#!/usr/bin/env python3
"""Read-only deterministic coordination of eligible WorkUnits across campaigns.

The P1 priority policy produces one immutable preview per campaign.  This
module is the P2 portfolio boundary: it consumes those previews, applies one
explicit global concurrency ceiling, and returns a digest-addressed candidate
set.  It has no dispatch, campaign-write, repository, or delivery authority.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import pathlib
import re
from collections.abc import Iterable, Mapping
from typing import Any

import myrmex_priority_policy as priority_policy


DECISION_SCHEMA = "myrmex.portfolio-scheduling-decision/v1"
CAMPAIGN_ID_RE = re.compile(r"^camp-[a-z0-9][a-z0-9-]{4,60}$")
WU_ID_RE = re.compile(r"^WU-[A-Z0-9][A-Z0-9-]{0,30}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
DEFAULT_MAX_CONCURRENT_WU = 1
AUTHORITY = {
    "scope": "portfolio_schedule_preview_only",
    "dispatch": False,
    "start_run": False,
    "repository_write": False,
    "commit": False,
    "push": False,
}


class PortfolioSchedulerError(Exception):
    """Base error for a fail-closed portfolio preview."""


class PortfolioInputInvalid(PortfolioSchedulerError):
    """The portfolio input or an immutable campaign snapshot is invalid."""


class PortfolioBackendUnavailable(PortfolioSchedulerError):
    """A required read-only campaign source cannot be safely read."""


def _canon(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise PortfolioInputInvalid("portfolio value is not canonical JSON") from error


def _sha(value: Any) -> str:
    return hashlib.sha256(_canon(value)).hexdigest()


def _digest(value: Any, label: str) -> None:
    if not isinstance(value, str) or not DIGEST_RE.fullmatch(value):
        raise PortfolioInputInvalid(f"{label} must be a lowercase SHA-256 digest")


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PortfolioInputInvalid(f"{label} must be a positive integer")
    return value


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PortfolioInputInvalid(f"{label} must be a non-negative integer")
    return value


def _rfc3339(value: Any, label: str) -> None:
    if not isinstance(value, str) or "T" not in value or not (value.endswith("Z") or re.search(r"[+-][0-9]{2}:[0-9]{2}$", value)):
        raise PortfolioInputInvalid(f"{label} must be timezone-aware RFC3339")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise PortfolioInputInvalid(f"{label} must be timezone-aware RFC3339") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PortfolioInputInvalid(f"{label} must be timezone-aware RFC3339")


def _candidate_id(campaign_id: str, work_unit_id: str) -> str:
    return f"{campaign_id}/{work_unit_id}"


def _read_campaign(campaign_dir: pathlib.Path, campaign_id: str) -> dict[str, Any]:
    if not CAMPAIGN_ID_RE.fullmatch(campaign_id):
        raise PortfolioInputInvalid(f"invalid campaign ID: {campaign_id}")
    if campaign_dir.is_symlink() or not campaign_dir.is_dir():
        raise PortfolioBackendUnavailable(f"campaign directory is unavailable or unsafe: {campaign_id}")
    campaign_file = campaign_dir / "campaign.json"
    if campaign_file.is_symlink() or not campaign_file.is_file():
        raise PortfolioBackendUnavailable(f"campaign state is unavailable or unsafe: {campaign_id}")
    try:
        payload = json.loads(campaign_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise PortfolioBackendUnavailable(f"campaign state is unreadable: {campaign_id}") from error
    if not isinstance(payload, dict) or payload.get("id") != campaign_id:
        raise PortfolioInputInvalid(f"campaign identity does not match its path: {campaign_id}")
    _positive_int(payload.get("revision"), f"{campaign_id}.revision")
    return payload


def _campaign_paths(
    campaigns_root: pathlib.Path | Mapping[str, pathlib.Path],
    campaign_ids: Iterable[str] | None,
) -> list[tuple[str, pathlib.Path]]:
    if isinstance(campaigns_root, Mapping):
        supplied = dict(campaigns_root)
        if campaign_ids is not None:
            requested = list(campaign_ids)
            if len(requested) != len(set(requested)):
                raise PortfolioInputInvalid("campaign IDs must be unique")
            unknown = sorted(set(requested) - set(supplied))
            if unknown:
                raise PortfolioInputInvalid(f"requested campaigns are not supplied: {unknown}")
            supplied = {key: supplied[key] for key in requested}
        pairs = [(str(cid), pathlib.Path(path)) for cid, path in supplied.items()]
    else:
        root = pathlib.Path(campaigns_root)
        if root.is_symlink() or not root.is_dir():
            raise PortfolioBackendUnavailable("campaign root is unavailable or unsafe")
        if campaign_ids is None:
            try:
                names = sorted(
                    entry.name for entry in root.iterdir()
                    if entry.is_dir() and not entry.is_symlink() and CAMPAIGN_ID_RE.fullmatch(entry.name)
                )
            except OSError as error:
                raise PortfolioBackendUnavailable("campaign root cannot be listed") from error
        else:
            names = list(campaign_ids)
            if len(names) != len(set(names)):
                raise PortfolioInputInvalid("campaign IDs must be unique")
            names.sort()
        pairs = [(name, root / name) for name in names]

    if not pairs:
        raise PortfolioInputInvalid("portfolio contains no campaigns")
    seen: set[str] = set()
    result: list[tuple[str, pathlib.Path]] = []
    for campaign_id, path in pairs:
        if campaign_id in seen:
            raise PortfolioInputInvalid(f"duplicate campaign ID: {campaign_id}")
        seen.add(campaign_id)
        if not CAMPAIGN_ID_RE.fullmatch(campaign_id):
            raise PortfolioInputInvalid(f"invalid campaign ID: {campaign_id}")
        result.append((campaign_id, path))
    return sorted(result, key=lambda item: item[0])


def _campaign_descriptor(campaign: dict[str, Any], local_decision: dict[str, Any]) -> dict[str, Any]:
    active_count = sum(
        1 for wu in campaign.get("work_units", [])
        if isinstance(wu, dict) and wu.get("status") in priority_policy.ACTIVE
    )
    return {
        "campaign_id": campaign["id"],
        "campaign_revision": campaign["revision"],
        "campaign_state_digest": local_decision["campaign_state_digest"],
        "observed_campaign_updated_at": local_decision["observed_campaign_updated_at"],
        "local_decision_digest": local_decision["decision_digest"],
        "campaign_max_concurrent_wu": campaign["budgets"]["max_concurrent_wu"],
        "active_work_unit_count": active_count,
    }


def _portfolio_state_digest(
    max_concurrent_wu: int,
    policy: dict[str, Any],
    campaigns: list[dict[str, Any]],
) -> str:
    return _sha({
        "max_concurrent_wu": max_concurrent_wu,
        "policy_digest": policy["policy_digest"],
        "campaigns": campaigns,
    })


def _candidate_rows(
    campaigns: list[dict[str, Any]],
    local_decisions: Mapping[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for descriptor in campaigns:
        campaign_id = descriptor["campaign_id"]
        local = local_decisions[campaign_id]
        for local_row in local["considered_candidates"]:
            work_unit_id = local_row["work_unit_id"]
            rows.append({
                "candidate_id": _candidate_id(campaign_id, work_unit_id),
                "campaign_id": campaign_id,
                "campaign_revision": descriptor["campaign_revision"],
                "campaign_state_digest": descriptor["campaign_state_digest"],
                "work_unit_id": work_unit_id,
                "status": local_row["status"],
                "source_index": local_row["source_index"],
                "eligible": local_row["eligible"],
                "exclusion_reasons": list(local_row["exclusion_reasons"]),
                "features": copy.deepcopy(local_row["features"]),
                "score": local_row["score"],
                "local_rank": local_row["rank"],
                "portfolio_rank": None,
            })
    return sorted(rows, key=lambda row: row["candidate_id"])


def _build_decision(
    campaigns: list[dict[str, Any]],
    local_decisions: Mapping[str, dict[str, Any]],
    max_concurrent_wu: int,
    policy: dict[str, Any],
) -> dict[str, Any]:
    rows = _candidate_rows(campaigns, local_decisions)
    eligible_rows = sorted(
        (row for row in rows if row["eligible"]),
        key=lambda row: (-row["score"], row["candidate_id"]),
    )
    for rank, row in enumerate(eligible_rows, 1):
        row["portfolio_rank"] = rank
    eligible_ids = [row["candidate_id"] for row in eligible_rows]
    active_count = sum(item["active_work_unit_count"] for item in campaigns)
    available_slots = max(0, max_concurrent_wu - active_count)
    campaign_slots = {
        item["campaign_id"]: max(0, item["campaign_max_concurrent_wu"] - item["active_work_unit_count"])
        for item in campaigns
    }
    selected_ids: list[str] = []
    for row in eligible_rows:
        if available_slots <= 0:
            break
        campaign_id = row["campaign_id"]
        if campaign_slots[campaign_id] <= 0:
            continue
        selected_ids.append(row["candidate_id"])
        available_slots -= 1
        campaign_slots[campaign_id] -= 1
    if selected_ids:
        selection_reason = "highest_score_then_stable_id"
    elif active_count >= max_concurrent_wu:
        selection_reason = "portfolio_concurrency_exhausted"
    else:
        selection_reason = "no_available_candidate"
    body = {
        "schema": DECISION_SCHEMA,
        "portfolio_state_digest": _portfolio_state_digest(max_concurrent_wu, policy, campaigns),
        "max_concurrent_wu": max_concurrent_wu,
        "active_work_unit_count": active_count,
        "policy": copy.deepcopy(policy),
        "campaigns": campaigns,
        "considered_candidates": rows,
        "eligible_candidate_ids": eligible_ids,
        "selected_candidate_ids": selected_ids,
        "selection_reason": selection_reason,
        "authority": dict(AUTHORITY),
    }
    digest = _sha(body)
    decision = {**body, "decision_id": "portfolio_schedule_" + digest, "decision_digest": digest}
    validate_portfolio_decision(decision)
    return decision


def preview_portfolio(
    campaigns_root: pathlib.Path | Mapping[str, pathlib.Path],
    campaign_ids: Iterable[str] | None = None,
    max_concurrent_wu: int = DEFAULT_MAX_CONCURRENT_WU,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return one deterministic portfolio candidate preview without writes."""
    _positive_int(max_concurrent_wu, "max_concurrent_wu")
    selected_policy = priority_policy.default_policy() if policy is None else copy.deepcopy(policy)
    priority_policy.validate_policy(selected_policy)
    pairs = _campaign_paths(campaigns_root, campaign_ids)
    local_decisions: dict[str, dict[str, Any]] = {}
    descriptors: list[dict[str, Any]] = []
    for campaign_id, path in pairs:
        campaign = _read_campaign(path, campaign_id)
        revision = campaign["revision"]
        local = priority_policy.preview_schedule_payload(campaign, campaign_id, revision, selected_policy)
        local_decisions[campaign_id] = local
        descriptors.append(_campaign_descriptor(campaign, local))
    return _build_decision(descriptors, local_decisions, max_concurrent_wu, selected_policy)


def validate_portfolio_decision(decision: Any) -> None:
    """Validate shape, identities, ranking, capacity, and no-effect authority."""
    fields = {
        "schema", "decision_id", "decision_digest", "portfolio_state_digest",
        "max_concurrent_wu", "active_work_unit_count", "policy", "campaigns",
        "considered_candidates", "eligible_candidate_ids", "selected_candidate_ids",
        "selection_reason", "authority",
    }
    if not isinstance(decision, dict) or set(decision) != fields or decision.get("schema") != DECISION_SCHEMA:
        raise PortfolioInputInvalid("portfolio decision fields/schema invalid")
    digest = _sha({key: value for key, value in decision.items() if key not in {"decision_id", "decision_digest"}})
    if decision.get("decision_digest") != digest or decision.get("decision_id") != "portfolio_schedule_" + digest:
        raise PortfolioInputInvalid("portfolio decision digest identity invalid")
    _digest(decision.get("portfolio_state_digest"), "portfolio_state_digest")
    max_concurrent = _positive_int(decision.get("max_concurrent_wu"), "max_concurrent_wu")
    active_count = _nonnegative_int(decision.get("active_work_unit_count"), "active_work_unit_count")
    priority_policy.validate_policy(decision.get("policy"))
    campaigns = decision.get("campaigns")
    campaign_fields = {
        "campaign_id", "campaign_revision", "campaign_state_digest",
        "observed_campaign_updated_at", "local_decision_digest", "campaign_max_concurrent_wu",
        "active_work_unit_count",
    }
    if not isinstance(campaigns, list) or any(not isinstance(item, dict) or set(item) != campaign_fields for item in campaigns):
        raise PortfolioInputInvalid("portfolio campaign descriptors invalid")
    campaign_ids: list[str] = []
    descriptor_by_id: dict[str, dict[str, Any]] = {}
    for item in campaigns:
        cid = item.get("campaign_id")
        if not isinstance(cid, str) or not CAMPAIGN_ID_RE.fullmatch(cid) or cid in descriptor_by_id:
            raise PortfolioInputInvalid("portfolio campaign identity invalid or duplicated")
        _positive_int(item.get("campaign_revision"), f"{cid}.campaign_revision")
        _digest(item.get("campaign_state_digest"), f"{cid}.campaign_state_digest")
        _digest(item.get("local_decision_digest"), f"{cid}.local_decision_digest")
        _rfc3339(item.get("observed_campaign_updated_at"), f"{cid}.observed_campaign_updated_at")
        _positive_int(item.get("campaign_max_concurrent_wu"), f"{cid}.campaign_max_concurrent_wu")
        _nonnegative_int(item.get("active_work_unit_count"), f"{cid}.active_work_unit_count")
        campaign_ids.append(cid)
        descriptor_by_id[cid] = item
    if campaign_ids != sorted(campaign_ids):
        raise PortfolioInputInvalid("portfolio campaigns must be stable-ID sorted")
    if not campaigns:
        raise PortfolioInputInvalid("portfolio contains no campaigns")
    expected_state_digest = _portfolio_state_digest(max_concurrent, decision["policy"], campaigns)
    if decision["portfolio_state_digest"] != expected_state_digest:
        raise PortfolioInputInvalid("portfolio state digest invalid")
    if active_count != sum(item["active_work_unit_count"] for item in campaigns):
        raise PortfolioInputInvalid("portfolio active work-unit count invalid")

    candidate_fields = {
        "candidate_id", "campaign_id", "campaign_revision", "campaign_state_digest", "work_unit_id",
        "status", "source_index", "eligible", "exclusion_reasons", "features", "score",
        "local_rank", "portfolio_rank",
    }
    feature_fields = {"age_minutes", "critical_path_length", "downstream_unlock_count", "dependency_count", "completed_dependency_count"}
    rows = decision.get("considered_candidates")
    if not isinstance(rows, list) or any(not isinstance(row, dict) or set(row) != candidate_fields for row in rows):
        raise PortfolioInputInvalid("portfolio candidate rows invalid")
    seen: set[str] = set()
    eligible_rows: list[dict[str, Any]] = []
    rows_by_campaign: dict[str, list[dict[str, Any]]] = {cid: [] for cid in descriptor_by_id}
    for row in rows:
        cid = row.get("campaign_id")
        wuid = row.get("work_unit_id")
        if not isinstance(cid, str) or cid not in descriptor_by_id or not isinstance(wuid, str) or not WU_ID_RE.fullmatch(wuid):
            raise PortfolioInputInvalid("portfolio candidate identity invalid")
        expected_id = _candidate_id(cid, wuid)
        if row.get("candidate_id") != expected_id or expected_id in seen:
            raise PortfolioInputInvalid("portfolio candidate ID invalid or duplicated")
        seen.add(expected_id)
        descriptor = descriptor_by_id[cid]
        if row.get("campaign_revision") != descriptor["campaign_revision"] or row.get("campaign_state_digest") != descriptor["campaign_state_digest"]:
            raise PortfolioInputInvalid("portfolio candidate campaign binding invalid")
        if row.get("status") not in priority_policy.KNOWN_STATUSES:
            raise PortfolioInputInvalid("portfolio candidate status invalid")
        _nonnegative_int(row.get("source_index"), f"{expected_id}.source_index")
        if not isinstance(row.get("eligible"), bool):
            raise PortfolioInputInvalid("portfolio candidate eligibility invalid")
        reasons = row.get("exclusion_reasons")
        if not isinstance(reasons, list) or any(not isinstance(reason, str) or not reason for reason in reasons) or len(reasons) != len(set(reasons)):
            raise PortfolioInputInvalid("portfolio candidate exclusion reasons invalid")
        features = row.get("features")
        if (
            not isinstance(features, dict)
            or set(features) != feature_fields
            or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in features.values())
            or features.get("critical_path_length", 0) < 1
        ):
            raise PortfolioInputInvalid("portfolio candidate features invalid")
        if row["eligible"]:
            expected_score = sum(features[name] * decision["policy"]["weights"][name] for name in priority_policy.WEIGHTS)
            if reasons or isinstance(row.get("score"), bool) or not isinstance(row.get("score"), int) or row["score"] != expected_score:
                raise PortfolioInputInvalid("eligible portfolio candidate score/reasons invalid")
            _positive_int(row.get("local_rank"), f"{expected_id}.local_rank")
            _positive_int(row.get("portfolio_rank"), f"{expected_id}.portfolio_rank")
            eligible_rows.append(row)
        else:
            if not reasons or row.get("score") is not None or row.get("local_rank") is not None or row.get("portfolio_rank") is not None:
                raise PortfolioInputInvalid("excluded portfolio candidate fields invalid")
        _nonnegative_int(row.get("source_index"), f"{expected_id}.source_index")
        rows_by_campaign[cid].append(row)
    if [row["candidate_id"] for row in rows] != sorted(seen):
        raise PortfolioInputInvalid("portfolio candidates must be stable-ID sorted")
    for cid, campaign_rows in rows_by_campaign.items():
        source_indexes = sorted(row["source_index"] for row in campaign_rows)
        if source_indexes != list(range(len(campaign_rows))):
            raise PortfolioInputInvalid(f"{cid} candidate source indexes are not a complete stable range")
        local_eligible = sorted(
            (row for row in campaign_rows if row["eligible"]),
            key=lambda row: (-row["score"], row["work_unit_id"]),
        )
        if [row["local_rank"] for row in local_eligible] != list(range(1, len(local_eligible) + 1)):
            raise PortfolioInputInvalid(f"{cid} local candidate ranking invalid")
    expected_eligible = sorted(eligible_rows, key=lambda row: (-row["score"], row["candidate_id"]))
    if [row["portfolio_rank"] for row in expected_eligible] != list(range(1, len(expected_eligible) + 1)):
        raise PortfolioInputInvalid("portfolio candidate ranking invalid")
    eligible_ids = decision.get("eligible_candidate_ids")
    if not isinstance(eligible_ids, list) or eligible_ids != [row["candidate_id"] for row in expected_eligible]:
        raise PortfolioInputInvalid("portfolio eligible candidate list invalid")
    selected_ids = decision.get("selected_candidate_ids")
    available_slots = max(0, max_concurrent - active_count)
    campaign_slots = {
        cid: max(0, descriptor["campaign_max_concurrent_wu"] - descriptor["active_work_unit_count"])
        for cid, descriptor in descriptor_by_id.items()
    }
    expected_selected: list[str] = []
    for row in expected_eligible:
        if available_slots <= 0:
            break
        cid = row["campaign_id"]
        if campaign_slots[cid] <= 0:
            continue
        expected_selected.append(row["candidate_id"])
        available_slots -= 1
        campaign_slots[cid] -= 1
    if not isinstance(selected_ids, list) or selected_ids != expected_selected:
        raise PortfolioInputInvalid("portfolio selected candidate list violates capacity/ranking")
    if decision.get("selection_reason") not in {"highest_score_then_stable_id", "no_available_candidate", "portfolio_concurrency_exhausted"}:
        raise PortfolioInputInvalid("portfolio selection reason invalid")
    expected_reason = (
        "highest_score_then_stable_id" if selected_ids
        else "portfolio_concurrency_exhausted" if active_count >= max_concurrent
        else "no_available_candidate"
    )
    if decision["selection_reason"] != expected_reason or decision.get("authority") != AUTHORITY:
        raise PortfolioInputInvalid("portfolio selection or authority invalid")


__all__ = [
    "AUTHORITY", "DECISION_SCHEMA", "DEFAULT_MAX_CONCURRENT_WU", "PortfolioBackendUnavailable",
    "PortfolioInputInvalid", "PortfolioSchedulerError", "preview_portfolio", "validate_portfolio_decision",
]
