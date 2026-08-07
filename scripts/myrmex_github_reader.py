#!/usr/bin/env python3
"""State-first read-only GitHub issue and milestone adapter for Myrmex P1-005.

Transforms an injected transport's GitHub issues/milestones snapshot into a
safe neutral representation (``myrmex.github-source-neutral/v1``) and observes
that representation through the P1-003 state-first import lifecycle.

The module is stdlib-only and performs NO HTTP/GitHub/network access. The
external transport is an injected callable whose implementation (auth,
pagination, HTTP) lives outside this module. Tests use only an in-process
fake transport and deterministic local fixtures.

Safety boundary:
  * issue bodies, milestone descriptions, comments, users, URLs, credentials,
    headers, and raw transport fields are NEVER copied into the neutral
    representation, content digest, observation, receipt, or any persisted
    artifact;
  * ``content_digest`` covers only safe issue/milestone metadata (number,
    title, state, labels, milestone association, due date);
  * ``observed_version`` additionally includes safe ``updated_at`` metadata;
  * the P1-002 secret/raw-content policy is applied to the selected safe
    metadata before hashing; ignored raw bodies are never scanned or
    persisted.

P1-003 remains the sole operation/idempotency/recovery authority: the reader
is invoked only after the durable import intent exists.
"""
from __future__ import annotations

import copy
import hashlib
import json
import pathlib
import re
import sys
from typing import Any, Callable


class GitHubSourceError(Exception):
    """Base error for the GitHub source adapter."""


try:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import myrmex_campaign_intelligence as intel  # type: ignore
except Exception as exc:  # pragma: no cover - import-time only
    raise GitHubSourceError(
        "P1-002 safety backend is unavailable; GitHub adapters fail closed"
    ) from exc


class GitHubRepositoryInvalid(GitHubSourceError):
    """Repository identity is not a valid owner/repository lexical form."""


class GitHubTransportInvalid(GitHubSourceError):
    """Transport response violates the bounded protocol."""


class GitHubSnapshotMalformed(GitHubSourceError):
    """Transport success payload contains structurally invalid selected fields."""


class GitHubSourcePolicyError(GitHubSourceError):
    """Selected safe metadata violates the P1-002 secret/raw-content policy."""


NEUTRAL_SCHEMA = "myrmex.github-source-neutral/v1"
ADAPTER_ID = "github-issues-milestones/v1"
SOURCE_KIND = "github-issues-milestones"
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

ISSUE_STATES = ("open", "closed")
MILESTONE_STATES = ("open", "closed")

AMBIGUITY_CODES = {
    "duplicate_issue_number",
    "duplicate_milestone_number",
    "unresolved_milestone_reference",
}

SAFE_ISSUE_FIELDS = ("issue_id", "number", "title", "state", "labels", "milestone_number", "updated_at")
SAFE_MILESTONE_FIELDS = ("milestone_id", "number", "title", "state", "due_on", "updated_at")
NEUTRAL_TOP_FIELDS = ("schema", "repository", "issues", "milestones", "ambiguities")

ISSUE_SELECTED_RAW = ("number", "title", "state", "labels", "milestone", "updated_at", "pull_request")
MILESTONE_SELECTED_RAW = ("number", "title", "state", "due_on", "updated_at")

TRANSPORT_REQUEST_FIELDS = ("operation", "repository", "issue_state", "milestone_state", "include_pull_requests")
TRANSPORT_OK_FIELDS = ("status", "repository", "issues", "milestones")
TRANSPORT_UNAVAILABLE_FIELDS = ("status", "reason_code")


def canonical_json_bytes(value: Any) -> bytes:
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
        raise GitHubSourcePolicyError(
            f"github safe metadata rejected by secret/raw-content policy at {where}"
        ) from exc


def canonical_repository(repository: str) -> str:
    """Lexically validate and canonicalize an owner/repository identity."""
    if not isinstance(repository, str) or not repository:
        raise GitHubRepositoryInvalid("repository must be a non-empty owner/repository string")
    if not REPOSITORY_RE.fullmatch(repository):
        raise GitHubRepositoryInvalid("repository must match ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    return repository.lower()


# ---------------------------------------------------------------------------
# Validation helpers


def _require_int_positive(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise GitHubSnapshotMalformed(f"{label} must be a positive integer")
    return value


def _require_nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise GitHubSnapshotMalformed(f"{label} must be a non-empty string")
    return value


def _require_state(value: Any, allowed: tuple[str, ...], label: str) -> str:
    if value not in allowed:
        raise GitHubSnapshotMalformed(f"{label} must be one of {', '.join(allowed)}")
    return value


def _require_optional_string(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise GitHubSnapshotMalformed(f"{label} must be a string, null, or omitted")
    return value


def _normalize_labels(raw_labels: Any) -> list[str]:
    """Normalize labels to a sorted unique name list (non-semantic ordering)."""
    if raw_labels is None:
        return []
    if not isinstance(raw_labels, list):
        raise GitHubSnapshotMalformed("issue labels must be an array")
    names: list[str] = []
    for entry in raw_labels:
        if isinstance(entry, str):
            name = entry
        elif isinstance(entry, dict) and isinstance(entry.get("name"), str):
            name = entry["name"]
        else:
            raise GitHubSnapshotMalformed("issue label must be a string or {name: string}")
        if not name:
            raise GitHubSnapshotMalformed("issue label name must be non-empty")
        names.append(name)
    return sorted(set(names))


def _milestone_number(raw_milestone: Any) -> int | None:
    """Extract a safe milestone number from a raw milestone association."""
    if raw_milestone is None:
        return None
    if not isinstance(raw_milestone, dict):
        raise GitHubSnapshotMalformed("issue milestone must be an object or null")
    number = raw_milestone.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise GitHubSnapshotMalformed("issue milestone.number must be a positive integer")
    return number


# ---------------------------------------------------------------------------
# Semantic identity + digest


def _issue_id(repository: str, number: int) -> str:
    core = {"repository": repository, "number": number}
    return "ghissue_" + sha256_hex(canonical_json_bytes(core))


def _milestone_id(repository: str, number: int) -> str:
    core = {"repository": repository, "number": number}
    return "ghmilestone_" + sha256_hex(canonical_json_bytes(core))


def github_content_digest(neutral: dict[str, Any]) -> str:
    """SHA-256 of the safe semantic projection (excludes bodies/descriptions/updated_at)."""
    issues = []
    for issue in neutral["issues"]:
        issues.append({
            "issue_id": issue["issue_id"],
            "number": issue["number"],
            "title": issue["title"],
            "state": issue["state"],
            "labels": issue["labels"],
            "milestone_number": issue["milestone_number"],
        })
    issues.sort(key=lambda i: i["number"])
    milestones = []
    for ms in neutral["milestones"]:
        milestones.append({
            "milestone_id": ms["milestone_id"],
            "number": ms["number"],
            "title": ms["title"],
            "state": ms["state"],
            "due_on": ms["due_on"],
        })
    milestones.sort(key=lambda m: m["number"])
    projection = {
        "schema": NEUTRAL_SCHEMA,
        "repository": neutral["repository"],
        "issues": issues,
        "milestones": milestones,
    }
    return sha256_hex(canonical_json_bytes(projection))


def github_observed_version(neutral: dict[str, Any]) -> str:
    """Safe version projection: semantic fields plus safe updated_at metadata."""
    issues = []
    for issue in neutral["issues"]:
        issues.append({
            "issue_id": issue["issue_id"],
            "number": issue["number"],
            "title": issue["title"],
            "state": issue["state"],
            "labels": issue["labels"],
            "milestone_number": issue["milestone_number"],
            "updated_at": issue.get("updated_at"),
        })
    issues.sort(key=lambda i: i["number"])
    milestones = []
    for ms in neutral["milestones"]:
        milestones.append({
            "milestone_id": ms["milestone_id"],
            "number": ms["number"],
            "title": ms["title"],
            "state": ms["state"],
            "due_on": ms["due_on"],
            "updated_at": ms.get("updated_at"),
        })
    milestones.sort(key=lambda m: m["number"])
    projection = {
        "schema": NEUTRAL_SCHEMA,
        "repository": neutral["repository"],
        "issues": issues,
        "milestones": milestones,
    }
    return "sha256:" + sha256_hex(canonical_json_bytes(projection))


def validate_github_neutral_snapshot(neutral: Any) -> None:
    """Validate a neutral snapshot; raise GitHubSnapshotMalformed on violation."""
    if not isinstance(neutral, dict):
        raise GitHubSnapshotMalformed("neutral snapshot must be an object")
    unknown = sorted(set(neutral) - set(NEUTRAL_TOP_FIELDS))
    if unknown:
        raise GitHubSnapshotMalformed(f"neutral snapshot has unknown fields: {', '.join(unknown)}")
    missing = [f for f in NEUTRAL_TOP_FIELDS if f not in neutral]
    if missing:
        raise GitHubSnapshotMalformed(f"neutral snapshot is missing: {', '.join(missing)}")
    if neutral["schema"] != NEUTRAL_SCHEMA:
        raise GitHubSnapshotMalformed("neutral snapshot schema mismatch")
    canonical_repository(neutral["repository"])
    if not isinstance(neutral.get("issues"), list) or not isinstance(neutral.get("milestones"), list):
        raise GitHubSnapshotMalformed("issues/milestones must be arrays")
    if not isinstance(neutral.get("ambiguities"), list):
        raise GitHubSnapshotMalformed("ambiguities must be an array")

    issue_numbers: set[int] = set()
    for issue in neutral["issues"]:
        unknown_i = sorted(set(issue) - set(SAFE_ISSUE_FIELDS))
        if unknown_i:
            raise GitHubSnapshotMalformed(f"issue has unknown fields: {', '.join(unknown_i)}")
        missing_i = [f for f in SAFE_ISSUE_FIELDS if f not in issue]
        if missing_i:
            raise GitHubSnapshotMalformed(f"issue is missing: {', '.join(missing_i)}")
        number = _require_int_positive(issue["number"], "issue.number")
        if issue["issue_id"] != _issue_id(neutral["repository"], number):
            raise GitHubSnapshotMalformed("issue.issue_id does not derive from repository+number")
        _require_nonempty_string(issue["title"], "issue.title")
        _require_state(issue["state"], ISSUE_STATES, "issue.state")
        if issue["labels"] != sorted(set(issue["labels"])):
            raise GitHubSnapshotMalformed("issue.labels must be sorted unique")
        if issue.get("milestone_number") is not None and (
            isinstance(issue["milestone_number"], bool)
            or not isinstance(issue["milestone_number"], int)
            or issue["milestone_number"] < 1
        ):
            raise GitHubSnapshotMalformed("issue.milestone_number must be a positive integer or null")
        _require_optional_string(issue.get("updated_at"), "issue.updated_at")
        issue_numbers.add(number)

    milestone_numbers: set[int] = set()
    for ms in neutral["milestones"]:
        unknown_m = sorted(set(ms) - set(SAFE_MILESTONE_FIELDS))
        if unknown_m:
            raise GitHubSnapshotMalformed(f"milestone has unknown fields: {', '.join(unknown_m)}")
        missing_m = [f for f in SAFE_MILESTONE_FIELDS if f not in ms]
        if missing_m:
            raise GitHubSnapshotMalformed(f"milestone is missing: {', '.join(missing_m)}")
        number = _require_int_positive(ms["number"], "milestone.number")
        if ms["milestone_id"] != _milestone_id(neutral["repository"], number):
            raise GitHubSnapshotMalformed("milestone.milestone_id does not derive from repository+number")
        _require_nonempty_string(ms["title"], "milestone.title")
        _require_state(ms["state"], MILESTONE_STATES, "milestone.state")
        _require_optional_string(ms.get("due_on"), "milestone.due_on")
        _require_optional_string(ms.get("updated_at"), "milestone.updated_at")
        milestone_numbers.add(number)

    for amb in neutral["ambiguities"]:
        if not isinstance(amb, dict):
            raise GitHubSnapshotMalformed("ambiguity must be an object")
        if amb.get("code") not in AMBIGUITY_CODES:
            raise GitHubSnapshotMalformed(f"ambiguity has unknown code {amb.get('code')!r}")
        if amb.get("entity_id") is not None and not isinstance(amb["entity_id"], str):
            raise GitHubSnapshotMalformed("ambiguity.entity_id must be string or null")


# ---------------------------------------------------------------------------
# Normalization


def normalize_github_snapshot(snapshot: Any, expected_repository: str) -> dict[str, Any]:
    """Normalize a transport success snapshot into a safe neutral representation.

    ``snapshot`` must be the exact transport success payload
    ({status:"ok", repository, issues, milestones}).
    """
    if not isinstance(snapshot, dict):
        raise GitHubTransportInvalid("transport success payload must be an object")
    unknown = sorted(set(snapshot) - set(TRANSPORT_OK_FIELDS))
    if unknown:
        raise GitHubTransportInvalid(f"transport payload has unknown fields: {', '.join(unknown)}")
    missing = [f for f in TRANSPORT_OK_FIELDS if f not in snapshot]
    if missing:
        raise GitHubTransportInvalid(f"transport payload is missing: {', '.join(missing)}")
    if snapshot.get("status") != "ok":
        raise GitHubTransportInvalid("transport status must be 'ok'")
    try:
        repo = canonical_repository(snapshot["repository"])
    except GitHubRepositoryInvalid as exc:
        raise GitHubTransportInvalid(str(exc)) from exc
    if repo != expected_repository:
        raise GitHubTransportInvalid(
            "transport repository does not match the requested repository"
        )
    if not isinstance(snapshot.get("issues"), list):
        raise GitHubTransportInvalid("transport issues must be an array")
    if not isinstance(snapshot.get("milestones"), list):
        raise GitHubTransportInvalid("transport milestones must be an array")

    ambiguities: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    milestones: list[dict[str, Any]] = []
    issue_numbers: set[int] = set()
    milestone_numbers: set[int] = set()

    for raw_ms in snapshot["milestones"]:
        if not isinstance(raw_ms, dict):
            raise GitHubSnapshotMalformed("milestone entry must be an object")
        number = _require_int_positive(raw_ms.get("number"), "milestone.number")
        title = _require_nonempty_string(raw_ms.get("title"), "milestone.title")
        state = _require_state(raw_ms.get("state"), MILESTONE_STATES, "milestone.state")
        due_on = _require_optional_string(raw_ms.get("due_on"), "milestone.due_on")
        updated_at = _require_optional_string(raw_ms.get("updated_at"), "milestone.updated_at")
        if number in milestone_numbers:
            ambiguities.append({
                "code": "duplicate_milestone_number",
                "entity_id": _milestone_id(repo, number),
            })
            continue
        milestone_numbers.add(number)
        milestones.append({
            "milestone_id": _milestone_id(repo, number),
            "number": number,
            "title": title,
            "state": state,
            "due_on": due_on,
            "updated_at": updated_at,
        })

    for idx, raw_issue in enumerate(snapshot["issues"]):
        if not isinstance(raw_issue, dict):
            raise GitHubSnapshotMalformed(f"issue entry {idx} must be an object")
        if raw_issue.get("pull_request") is not None:
            # PR-like records are excluded entirely.
            continue
        number = _require_int_positive(raw_issue.get("number"), f"issue[{idx}].number")
        title = _require_nonempty_string(raw_issue.get("title"), f"issue[{idx}].title")
        state = _require_state(raw_issue.get("state"), ISSUE_STATES, f"issue[{idx}].state")
        labels = _normalize_labels(raw_issue.get("labels"))
        milestone_number = _milestone_number(raw_issue.get("milestone"))
        updated_at = _require_optional_string(raw_issue.get("updated_at"), f"issue[{idx}].updated_at")
        if number in issue_numbers:
            ambiguities.append({
                "code": "duplicate_issue_number",
                "entity_id": _issue_id(repo, number),
            })
            continue
        issue_numbers.add(number)
        issues.append({
            "issue_id": _issue_id(repo, number),
            "number": number,
            "title": title,
            "state": state,
            "labels": labels,
            "milestone_number": milestone_number,
            "updated_at": updated_at,
        })

    # Validate milestone references: each issue's milestone must resolve to exactly one milestone.
    valid_milestone_numbers = {ms["number"] for ms in milestones}
    for issue in issues:
        if issue["milestone_number"] is not None and issue["milestone_number"] not in valid_milestone_numbers:
            ambiguities.append({
                "code": "unresolved_milestone_reference",
                "entity_id": issue["issue_id"],
            })

    issues.sort(key=lambda i: i["number"])
    milestones.sort(key=lambda m: m["number"])
    ambiguities.sort(key=lambda a: (a["code"], a["entity_id"] or ""))

    neutral = {
        "schema": NEUTRAL_SCHEMA,
        "repository": repo,
        "issues": issues,
        "milestones": milestones,
        "ambiguities": ambiguities,
    }
    validate_github_neutral_snapshot(neutral)
    # P1-002 policy over the selected safe metadata (never over ignored bodies).
    _policy_reject(neutral, "$")
    return neutral


# ---------------------------------------------------------------------------
# Reader factory + P1-003 wrapper


def make_github_source_reader(transport: Callable[[dict[str, Any]], Any]) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Return a P1-003-compatible reader using the injected transport.

    Construction validates the callable and captures it WITHOUT invoking it.
    All transport calls happen inside the returned reader, which P1-003
    invokes only after durable intent.
    """
    if not callable(transport):
        raise GitHubTransportInvalid("transport must be callable")

    def reader(context: dict[str, Any]) -> dict[str, Any]:
        request = copy.deepcopy(context["request"])
        transport_request = {
            "operation": "issues_milestones_snapshot",
            "repository": request["repository"],
            "issue_state": request["issue_state"],
            "milestone_state": request["milestone_state"],
            "include_pull_requests": request["include_pull_requests"],
        }
        result = transport(copy.deepcopy(transport_request))
        if not isinstance(result, dict):
            raise GitHubTransportInvalid("transport result must be an object")
        status = result.get("status")
        if status == "unavailable":
            unknown = sorted(set(result) - set(TRANSPORT_UNAVAILABLE_FIELDS))
            if unknown:
                raise GitHubTransportInvalid(
                    f"transport unavailable payload has unknown fields: {', '.join(unknown)}"
                )
            reason = result.get("reason_code")
            if not isinstance(reason, str) or not reason:
                raise GitHubTransportInvalid("unavailable reason_code must be a non-empty string")
            return {"status": "unavailable", "reason_code": reason}
        if status != "ok":
            raise GitHubTransportInvalid("transport status must be 'ok' or 'unavailable'")
        neutral = normalize_github_snapshot(result, request["repository"])
        if neutral["ambiguities"]:
            return {
                "status": "ambiguous",
                "reason_code": "github_snapshot_ambiguous",
                "observed_version": github_observed_version(neutral),
            }
        return {
            "status": "observed",
            "observed_version": github_observed_version(neutral),
            "content_digest": github_content_digest(neutral),
        }

    return reader


def execute_github_import(
    campaign_dir: str | pathlib.Path,
    campaign_id: str,
    campaign_revision: int,
    idempotency_key: str,
    repository: str,
    transport: Callable[[dict[str, Any]], Any],
    previous_content_digest: str | None = None,
) -> dict[str, Any]:
    """Execute one state-first GitHub import through the P1-003 lifecycle.

    Performs only lexical repository validation, source identity/request/
    reader construction, and delegation to execute_import_operation. No
    network operation occurs before P1-003 persists the durable intent.
    """
    from myrmex_backlog_import import execute_import_operation  # type: ignore

    repo = canonical_repository(repository)
    source_identity = {
        "kind": SOURCE_KIND,
        "canonical_id": repo,
    }
    request = {
        "repository": repo,
        "resource": "issues-and-milestones",
        "issue_state": "all",
        "milestone_state": "all",
        "include_pull_requests": False,
    }
    reader = make_github_source_reader(transport)
    return execute_import_operation(
        campaign_dir=campaign_dir,
        campaign_id=campaign_id,
        campaign_revision=campaign_revision,
        idempotency_key=idempotency_key,
        source_identity=source_identity,
        adapter=ADAPTER_ID,
        request=request,
        previous_content_digest=previous_content_digest,
        reader=reader,
    )
