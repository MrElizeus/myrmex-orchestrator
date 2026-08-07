#!/usr/bin/env python3
"""State-first read-only backlog import operations (WU-P1-003).

Stdlib-only import engine that executes one idempotent, state-first import
operation against the P1-002 campaign-intelligence sidecar store. The intent
record for the operation is durably persisted BEFORE the source reader is
invoked; every later stage (source observation, effect-observed,
receipt-recorded, confirmed) is persisted as an immutable artifact and reused
on replay. The operation is strictly read-only with respect to the campaign:
it never mutates campaign.json, the campaign revision, work units, the DAG,
leases, or evidence, and its persisted authority blocks pin every effect flag
(including create_work_units) to false.

The reader boundary is deliberately narrow: the reader receives ONLY
``{source_identity, adapter, request, request_digest}`` and returns exactly one
of the typed protocol results documented on :func:`_parse_reader_result`. The
reader has no campaign, work-unit, plan, repository, or credential access.

Artifact layout under the campaign intelligence sidecar (all kind=decision
except the source observation which is kind=backlog)::

    import-operation/<operation_id>/intent
    import-operation/<operation_id>/effect-observed
    import-operation/<operation_id>/receipt-recorded
    import-operation/<operation_id>/confirmed
    source-observation/<operation_id>

Canonical digest rules follow the v1 contracts:
``canonical_json_bytes`` uses UTF-8, sorted keys, compact separators,
``ensure_ascii=False`` and ``allow_nan=False``. operation_id is
``importop-`` + first 24 hex of SHA256(canonical {campaign_id,
idempotency_key}) and does NOT include request content; request_digest is
SHA256(canonical request); observation_digest covers the observation
excluding observation_id/observation_digest; receipt_digest covers the
receipt excluding receipt_id/receipt_digest; record_digest covers the record
excluding record_id/record_digest.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable

# Import the sibling campaign-intelligence store normally. The import module
# reuses put_artifact/get_artifact and the P1-002 secret/raw-content rejection
# policy; it never mutates campaign state itself.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import myrmex_campaign_intelligence as intel  # noqa: E402

SOURCE_OBSERVATION_SCHEMA = "myrmex.source-observation/v1"
IMPORT_OPERATION_SCHEMA = "myrmex.import-operation/v1"

CAMPAIGN_ID_RE = re.compile(r"^camp-[a-z0-9][a-z0-9-]{4,60}$")
SOURCE_KIND_RE = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
OPERATION_ID_RE = re.compile(r"^importop-[0-9a-f]{24}$")
OBSERVATION_ID_RE = re.compile(r"^srcobs_[0-9a-f]{64}$")
RECORD_ID_RE = re.compile(r"^importrec_[0-9a-f]{64}$")
RECEIPT_ID_RE = re.compile(r"^imprcpt_[0-9a-f]{64}$")
SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")

LIFECYCLE_STATUSES = ("intent", "effect-observed", "receipt-recorded", "confirmed")
OUTCOMES = ("changed", "unchanged", "unavailable", "invalid", "ambiguous")

# Exact runtime field sets mirroring the committed 2020-12 schemas. Recovery
# and construction validation reject any unknown or missing field so that a
# locally tampered record cannot be accepted on its own internally consistent
# digests.
SOURCE_OBSERVATION_FIELDS = frozenset({
    "schema", "observation_id", "observation_digest", "campaign_id",
    "operation_id", "source_identity", "adapter", "request_digest",
    "previous_content_digest", "observed_version", "content_digest",
    "outcome", "reason_code", "observed_at", "authority",
})
IMPORT_RECORD_FIELDS = frozenset({
    "schema", "record_id", "record_digest", "operation_id", "idempotency_key",
    "campaign_id", "source_identity", "adapter", "request", "request_digest",
    "previous_content_digest", "status", "effect_stage", "observation",
    "receipt", "previous_record_id", "authority", "created_at",
})
OBSERVATION_REF_FIELDS = frozenset({"observation_id", "observation_digest", "outcome"})
RECEIPT_FIELDS = frozenset({
    "receipt_id", "receipt_digest", "operation_id", "observation_id",
    "observation_digest", "request_digest", "outcome",
})

# Bounded authority blocks. Only external_read is true; every effect flag
# (repository_write, create_work_units, activate_plan, commit, push, merge,
# release, deploy) is pinned false.
OBSERVATION_AUTHORITY = {
    "scope": "source_observation_only",
    "external_read": True,
    "repository_write": False,
    "create_work_units": False,
    "activate_plan": False,
    "commit": False,
    "push": False,
    "merge": False,
    "release": False,
    "deploy": False,
}

IMPORT_AUTHORITY = {
    "scope": "read_only_backlog_import",
    "external_read": True,
    "repository_write": False,
    "create_work_units": False,
    "activate_plan": False,
    "commit": False,
    "push": False,
    "merge": False,
    "release": False,
    "deploy": False,
}


class BacklogImportError(Exception):
    """Base class for backlog import failures."""


class ImportOperationConflict(BacklogImportError):
    """An import operation already exists with different identity or content."""


class ImportOperationInvalid(BacklogImportError):
    """An import operation input, artifact, or recovery chain is invalid.

    Error messages never echo secret or raw-content values.
    """


class SourceReaderInvalid(BacklogImportError):
    """The reader returned a malformed or out-of-protocol result."""


# ---------------------------------------------------------------------------
# Canonical digest helpers (match the v1 contract descriptions)
# ---------------------------------------------------------------------------

def canonical_json_bytes(value: Any) -> bytes:
    """Deterministic canonical serialization: UTF-8, sorted keys, compact separators, no trailing whitespace."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def compute_request_digest(request: dict[str, Any]) -> str:
    """SHA256(canonical request)."""
    return sha256_hex(canonical_json_bytes(request))


def derive_operation_id(campaign_id: str, idempotency_key: str) -> str:
    """'importop-' + first 24 hex of SHA256(canonical {campaign_id, idempotency_key}).

    The operation identity deliberately does NOT include request content.
    """
    core = {"campaign_id": campaign_id, "idempotency_key": idempotency_key}
    return "importop-" + sha256_hex(canonical_json_bytes(core))[:24]


def compute_observation_digest(observation: dict[str, Any]) -> str:
    """SHA256(canonical observation excluding observation_id and observation_digest)."""
    core = {
        k: v for k, v in observation.items()
        if k not in ("observation_id", "observation_digest")
    }
    return sha256_hex(canonical_json_bytes(core))


def derive_observation_id(observation_digest: str) -> str:
    return "srcobs_" + observation_digest


def compute_receipt_digest(receipt: dict[str, Any]) -> str:
    """SHA256(canonical receipt excluding receipt_id and receipt_digest)."""
    core = {
        k: v for k, v in receipt.items()
        if k not in ("receipt_id", "receipt_digest")
    }
    return sha256_hex(canonical_json_bytes(core))


def derive_receipt_id(receipt_digest: str) -> str:
    return "imprcpt_" + receipt_digest


def compute_record_digest(record: dict[str, Any]) -> str:
    """SHA256(canonical record excluding record_id and record_digest)."""
    core = {
        k: v for k, v in record.items()
        if k not in ("record_id", "record_digest")
    }
    return sha256_hex(canonical_json_bytes(core))


def derive_record_id(record_digest: str) -> str:
    return "importrec_" + record_digest


# ---------------------------------------------------------------------------
# Input validation (never echoes secret values)
# ---------------------------------------------------------------------------

def _validate_campaign_id(campaign_id: Any) -> None:
    if not isinstance(campaign_id, str) or not CAMPAIGN_ID_RE.match(campaign_id):
        raise ImportOperationInvalid(
            "invalid campaign_id; must match ^camp-[a-z0-9][a-z0-9-]{4,60}$"
        )


def _source_identity_errors(source_identity: Any) -> list[str]:
    """Return exact-shape violations for a source identity block."""
    if not isinstance(source_identity, dict) or set(source_identity) != {"kind", "canonical_id"}:
        return ["source_identity must contain exactly the fields kind and canonical_id"]
    errors: list[str] = []
    kind = source_identity.get("kind")
    if not isinstance(kind, str) or not SOURCE_KIND_RE.match(kind):
        errors.append("source_identity.kind must match ^[a-z][a-z0-9._-]{0,63}$")
    canonical_id = source_identity.get("canonical_id")
    if not isinstance(canonical_id, str) or not (1 <= len(canonical_id) <= 512):
        errors.append("source_identity.canonical_id must be a string of 1..512 characters")
    return errors


def _validate_source_identity(source_identity: Any) -> None:
    errors = _source_identity_errors(source_identity)
    if errors:
        raise ImportOperationInvalid("; ".join(errors))


def _authority_errors(authority: Any, expected: dict[str, Any]) -> list[str]:
    """Return a violation when an authority block is not the exact bounded block.

    Dict equality is exact: unknown fields, missing fields, repository
    authority, or any raised effect flag (create_work_units, activate_plan,
    ...) all diverge and become invalid.
    """
    if not isinstance(authority, dict) or authority != expected:
        return [
            f"authority must equal the bounded {expected['scope']} block exactly "
            "(external_read only; every effect flag false)"
        ]
    return []


def _previous_content_digest_errors(value: Any) -> list[str]:
    if value is not None and (
        not isinstance(value, str) or not SHA256_HEX_RE.match(value)
    ):
        return ["previous_content_digest must be null or a 64-hex SHA-256 digest"]
    return []


def _rfc3339_datetime_errors(value: Any) -> list[str]:
    if not isinstance(value, str) or not value:
        return ["must be a non-empty RFC3339 date-time string"]
    try:
        dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return ["must be an RFC3339 date-time string"]
    return []


def _validate_idempotency_key(idempotency_key: Any) -> None:
    if not isinstance(idempotency_key, str) or not (1 <= len(idempotency_key) <= 256):
        raise ImportOperationInvalid(
            "idempotency_key must be a string of 1..256 characters"
        )


def _validate_adapter(adapter: Any) -> None:
    if not isinstance(adapter, str) or len(adapter) < 1:
        raise ImportOperationInvalid("adapter must be a non-empty string")


def _validate_previous_content_digest(value: Any) -> None:
    errors = _previous_content_digest_errors(value)
    if errors:
        raise ImportOperationInvalid("; ".join(errors))


def _validate_request(request: Any) -> None:
    if not isinstance(request, dict):
        raise ImportOperationInvalid("request must be a JSON object")


def _reject_secret_or_raw(value: Any, where: str = "$") -> None:
    """Apply the P1-002 secret/raw-content policy before any artifact is persisted."""
    try:
        intel.reject_secret_or_raw(value, where)
    except intel.IntelligencePayloadRejected as exc:
        # Generic message; offending values are never echoed.
        raise ImportOperationInvalid(
            "import payload rejected by secret/raw-content policy"
        ) from exc


# ---------------------------------------------------------------------------
# Reader protocol
# ---------------------------------------------------------------------------

_OBSERVED_KEYS = frozenset({"status", "observed_version", "content_digest"})
_REASON_KEYS = frozenset({"status", "reason_code"})
_AMBIGUOUS_KEYS = frozenset({"status", "reason_code", "observed_version"})


def _parse_reader_result(result: Any) -> dict[str, Any]:
    """Validate a reader return and normalize it.

    The reader protocol is exactly one of::

        {status: "observed", observed_version, content_digest}
        {status: "unavailable", reason_code}
        {status: "invalid", reason_code}
        {status: "ambiguous", reason_code, observed_version: null|str}

    Unknown keys, missing keys, or wrong types raise SourceReaderInvalid. The
    returned dict carries the reader status plus the normalized
    observed_version/content_digest/reason_code (null when not applicable).
    """
    if not isinstance(result, dict):
        raise SourceReaderInvalid("reader must return a JSON object")
    status = result.get("status")
    if status == "observed":
        if set(result) != _OBSERVED_KEYS:
            raise SourceReaderInvalid(
                "observed reader result must contain exactly status, observed_version, content_digest"
            )
        observed_version = result.get("observed_version")
        content_digest = result.get("content_digest")
        if not isinstance(observed_version, str) or not observed_version:
            raise SourceReaderInvalid(
                "observed reader result requires a non-empty observed_version string"
            )
        if not isinstance(content_digest, str) or not SHA256_HEX_RE.match(content_digest):
            raise SourceReaderInvalid(
                "observed reader result requires a 64-hex content_digest"
            )
        return {
            "status": status,
            "observed_version": observed_version,
            "content_digest": content_digest,
            "reason_code": None,
        }
    if status in ("unavailable", "invalid"):
        if set(result) != _REASON_KEYS:
            raise SourceReaderInvalid(
                f"{status} reader result must contain exactly status and reason_code"
            )
        reason_code = result.get("reason_code")
        if not isinstance(reason_code, str) or not reason_code:
            raise SourceReaderInvalid(
                f"{status} reader result requires a non-empty reason_code string"
            )
        return {
            "status": status,
            "observed_version": None,
            "content_digest": None,
            "reason_code": reason_code,
        }
    if status == "ambiguous":
        if set(result) != _AMBIGUOUS_KEYS:
            raise SourceReaderInvalid(
                "ambiguous reader result must contain exactly status, reason_code, observed_version"
            )
        reason_code = result.get("reason_code")
        observed_version = result.get("observed_version")
        if not isinstance(reason_code, str) or not reason_code:
            raise SourceReaderInvalid(
                "ambiguous reader result requires a non-empty reason_code string"
            )
        if observed_version is not None and not isinstance(observed_version, str):
            raise SourceReaderInvalid(
                "ambiguous reader result observed_version must be null or a string"
            )
        return {
            "status": status,
            "observed_version": observed_version,
            "content_digest": None,
            "reason_code": reason_code,
        }
    raise SourceReaderInvalid(f"unknown reader status {status!r}")


def _map_outcome(parsed: dict[str, Any], previous_content_digest: str | None) -> str:
    """Map a reader result to the source-observation outcome."""
    status = parsed["status"]
    if status == "observed":
        if previous_content_digest is None:
            return "changed"
        if parsed["content_digest"] == previous_content_digest:
            return "unchanged"
        return "changed"
    return status  # unavailable | invalid | ambiguous


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------

def _build_observation(
    *,
    campaign_id: str,
    operation_id: str,
    source_identity: dict[str, Any],
    adapter: str,
    request_digest: str,
    previous_content_digest: str | None,
    parsed: dict[str, Any],
    observed_at: str,
) -> dict[str, Any]:
    payload = {
        "schema": SOURCE_OBSERVATION_SCHEMA,
        "campaign_id": campaign_id,
        "operation_id": operation_id,
        "source_identity": source_identity,
        "adapter": adapter,
        "request_digest": request_digest,
        "previous_content_digest": previous_content_digest,
        "observed_version": parsed["observed_version"],
        "content_digest": parsed["content_digest"],
        "outcome": parsed["outcome"],
        "reason_code": parsed["reason_code"],
        "observed_at": observed_at,
        "authority": dict(OBSERVATION_AUTHORITY),
    }
    payload["observation_digest"] = compute_observation_digest(payload)
    payload["observation_id"] = derive_observation_id(payload["observation_digest"])
    return payload


def _build_import_record(
    *,
    operation_id: str,
    idempotency_key: str,
    campaign_id: str,
    source_identity: dict[str, Any],
    adapter: str,
    request: dict[str, Any],
    request_digest: str,
    previous_content_digest: str | None,
    status: str,
    effect_stage: str,
    observation: dict[str, Any] | None,
    receipt: dict[str, Any] | None,
    previous_record_id: str | None,
    authority: dict[str, Any],
    created_at: str,
) -> dict[str, Any]:
    record = {
        "schema": IMPORT_OPERATION_SCHEMA,
        "operation_id": operation_id,
        "idempotency_key": idempotency_key,
        "campaign_id": campaign_id,
        "source_identity": source_identity,
        "adapter": adapter,
        "request": request,
        "request_digest": request_digest,
        "previous_content_digest": previous_content_digest,
        "status": status,
        "effect_stage": effect_stage,
        "observation": observation,
        "receipt": receipt,
        "previous_record_id": previous_record_id,
        "authority": authority,
        "created_at": created_at,
    }
    record["record_digest"] = compute_record_digest(record)
    record["record_id"] = derive_record_id(record["record_digest"])
    return record


def _build_receipt(
    *,
    operation_id: str,
    observation_id: str,
    observation_digest: str,
    request_digest: str,
    outcome: str,
) -> dict[str, Any]:
    """Deterministic receipt for (operation, observation, request, outcome)."""
    receipt = {
        "operation_id": operation_id,
        "observation_id": observation_id,
        "observation_digest": observation_digest,
        "request_digest": request_digest,
        "outcome": outcome,
    }
    receipt["receipt_digest"] = compute_receipt_digest(receipt)
    receipt["receipt_id"] = derive_receipt_id(receipt["receipt_digest"])
    return receipt


def _observation_ref(observation: dict[str, Any]) -> dict[str, Any]:
    return {
        "observation_id": observation["observation_id"],
        "observation_digest": observation["observation_digest"],
        "outcome": observation["outcome"],
    }


# ---------------------------------------------------------------------------
# Semantic validation helpers (invariants JSON Schema cannot express)
# ---------------------------------------------------------------------------

def validate_source_observation_semantics(observation: dict[str, Any]) -> list[str]:
    """Return invariant violations for a source-observation payload.

    Enforces the exact closed field set of the committed schema plus the
    identity/authority/digest/outcome invariants the schema cannot express.
    """
    errors: list[str] = []
    if not isinstance(observation, dict):
        return ["source observation must be an object"]
    if set(observation) != SOURCE_OBSERVATION_FIELDS:
        errors.append(
            "source observation must contain exactly the fields "
            + ", ".join(sorted(SOURCE_OBSERVATION_FIELDS))
        )
    if observation.get("schema") != SOURCE_OBSERVATION_SCHEMA:
        errors.append("schema must be myrmex.source-observation/v1")
    campaign_id = observation.get("campaign_id")
    if not isinstance(campaign_id, str) or not CAMPAIGN_ID_RE.match(campaign_id):
        errors.append("invalid campaign_id; must match ^camp-[a-z0-9][a-z0-9-]{4,60}$")
    operation_id = observation.get("operation_id")
    if not isinstance(operation_id, str) or not OPERATION_ID_RE.match(operation_id):
        errors.append("invalid operation_id; must match ^importop-[0-9a-f]{24}$")
    errors.extend(_source_identity_errors(observation.get("source_identity")))
    adapter = observation.get("adapter")
    if not isinstance(adapter, str) or len(adapter) < 1:
        errors.append("adapter must be a non-empty string")
    request_digest = observation.get("request_digest")
    if not isinstance(request_digest, str) or not SHA256_HEX_RE.match(request_digest):
        errors.append("request_digest must be a 64-hex SHA-256 digest")
    errors.extend(_previous_content_digest_errors(observation.get("previous_content_digest")))
    if observation.get("outcome") in OUTCOMES:
        observed_at = observation.get("observed_at")
        if _rfc3339_datetime_errors(observed_at):
            errors.append("observed_at must be an RFC3339 date-time string")
    errors.extend(_authority_errors(observation.get("authority"), OBSERVATION_AUTHORITY))
    if observation.get("observation_digest") != compute_observation_digest(observation):
        errors.append("observation_digest does not match canonical digest of the observation")
    if observation.get("observation_id") != "srcobs_" + str(observation.get("observation_digest", "")):
        errors.append("observation_id is not derived from observation_digest ('srcobs_' + observation_digest)")
    outcome = observation.get("outcome")
    if outcome in ("changed", "unchanged"):
        observed_version = observation.get("observed_version")
        if not isinstance(observed_version, str) or not observed_version:
            errors.append("changed/unchanged requires a non-empty observed_version string")
        content_digest = observation.get("content_digest")
        if not isinstance(content_digest, str) or not SHA256_HEX_RE.match(content_digest):
            errors.append("changed/unchanged requires a 64-hex content_digest")
        if observation.get("reason_code") is not None:
            errors.append("changed/unchanged requires a null reason_code")
        previous = observation.get("previous_content_digest")
        if outcome == "unchanged":
            if previous is None:
                errors.append("unchanged requires a non-null previous_content_digest")
            elif content_digest != previous:
                errors.append("unchanged requires content_digest == previous_content_digest")
        else:
            if previous is not None and content_digest == previous:
                errors.append("changed requires previous_content_digest null or a different content_digest")
    elif outcome in ("unavailable", "invalid"):
        if observation.get("observed_version") is not None:
            errors.append("unavailable/invalid requires a null observed_version")
        if observation.get("content_digest") is not None:
            errors.append("unavailable/invalid requires a null content_digest")
        reason_code = observation.get("reason_code")
        if not isinstance(reason_code, str) or not reason_code:
            errors.append("unavailable/invalid requires a non-empty reason_code")
    elif outcome == "ambiguous":
        if observation.get("content_digest") is not None:
            errors.append("ambiguous requires a null content_digest")
        reason_code = observation.get("reason_code")
        if not isinstance(reason_code, str) or not reason_code:
            errors.append("ambiguous requires a non-empty reason_code")
        observed_version = observation.get("observed_version")
        if observed_version is not None and not isinstance(observed_version, str):
            errors.append("ambiguous observed_version must be null or a string")
    else:
        errors.append(f"unknown outcome {outcome!r}")
    return errors


def validate_receipt_semantics(receipt: dict[str, Any]) -> list[str]:
    """Return invariant violations for a receipt object.

    Enforces the exact closed field set of the import-operation schema's
    receipt member plus digest/identity derivations.
    """
    errors: list[str] = []
    if not isinstance(receipt, dict):
        return ["receipt must be an object"]
    if set(receipt) != RECEIPT_FIELDS:
        errors.append(
            "receipt must contain exactly the fields receipt_id, receipt_digest, "
            "operation_id, observation_id, observation_digest, request_digest, outcome"
        )
    if not isinstance(receipt.get("receipt_id"), str) or not RECEIPT_ID_RE.match(
        receipt.get("receipt_id", "")
    ):
        errors.append("invalid receipt_id; must match ^imprcpt_[0-9a-f]{64}$")
    if not isinstance(receipt.get("receipt_digest"), str) or not SHA256_HEX_RE.match(
        receipt.get("receipt_digest", "")
    ):
        errors.append("invalid receipt_digest; must be a 64-hex SHA-256 digest")
    if not isinstance(receipt.get("operation_id"), str) or not OPERATION_ID_RE.match(
        receipt.get("operation_id", "")
    ):
        errors.append("invalid receipt operation_id; must match ^importop-[0-9a-f]{24}$")
    if not isinstance(receipt.get("observation_id"), str) or not OBSERVATION_ID_RE.match(
        receipt.get("observation_id", "")
    ):
        errors.append("invalid receipt observation_id; must match ^srcobs_[0-9a-f]{64}$")
    if not isinstance(receipt.get("observation_digest"), str) or not SHA256_HEX_RE.match(
        receipt.get("observation_digest", "")
    ):
        errors.append("invalid receipt observation_digest; must be a 64-hex SHA-256 digest")
    if not isinstance(receipt.get("request_digest"), str) or not SHA256_HEX_RE.match(
        receipt.get("request_digest", "")
    ):
        errors.append("invalid receipt request_digest; must be a 64-hex SHA-256 digest")
    if receipt.get("outcome") not in OUTCOMES:
        errors.append("invalid receipt outcome")
    if receipt.get("receipt_digest") != compute_receipt_digest(receipt):
        errors.append("receipt_digest does not match canonical digest of the receipt")
    if receipt.get("receipt_id") != "imprcpt_" + str(receipt.get("receipt_digest", "")):
        errors.append("receipt_id is not derived from receipt_digest ('imprcpt_' + receipt_digest)")
    return errors


def validate_import_record_semantics(record: dict[str, Any]) -> list[str]:
    """Return invariant violations for an import-operation record payload.

    Enforces the exact closed field set of the committed schema plus the
    identity/authority/digest/status/reference/receipt invariants the schema
    cannot express.
    """
    errors: list[str] = []
    if not isinstance(record, dict):
        return ["import operation record must be an object"]
    if set(record) != IMPORT_RECORD_FIELDS:
        errors.append(
            "import operation record must contain exactly the fields "
            + ", ".join(sorted(IMPORT_RECORD_FIELDS))
        )
    if record.get("schema") != IMPORT_OPERATION_SCHEMA:
        errors.append("schema must be myrmex.import-operation/v1")
    campaign_id = record.get("campaign_id")
    if not isinstance(campaign_id, str) or not CAMPAIGN_ID_RE.match(campaign_id):
        errors.append("invalid campaign_id; must match ^camp-[a-z0-9][a-z0-9-]{4,60}$")
    operation_id = record.get("operation_id")
    if not isinstance(operation_id, str) or not OPERATION_ID_RE.match(operation_id):
        errors.append("invalid operation_id; must match ^importop-[0-9a-f]{24}$")
    idempotency_key = record.get("idempotency_key")
    if not isinstance(idempotency_key, str) or not (1 <= len(idempotency_key) <= 256):
        errors.append("idempotency_key must be a string of 1..256 characters")
    errors.extend(_source_identity_errors(record.get("source_identity")))
    adapter = record.get("adapter")
    if not isinstance(adapter, str) or len(adapter) < 1:
        errors.append("adapter must be a non-empty string")
    errors.extend(_previous_content_digest_errors(record.get("previous_content_digest")))
    if record.get("status") in LIFECYCLE_STATUSES:
        created_at = record.get("created_at")
        if _rfc3339_datetime_errors(created_at):
            errors.append("created_at must be an RFC3339 date-time string")
    errors.extend(_authority_errors(record.get("authority"), IMPORT_AUTHORITY))
    if record.get("record_digest") != compute_record_digest(record):
        errors.append("record_digest does not match canonical digest of the record")
    if record.get("record_id") != "importrec_" + str(record.get("record_digest", "")):
        errors.append("record_id is not derived from record_digest ('importrec_' + record_digest)")
    expected_operation_id = derive_operation_id(
        str(record.get("campaign_id", "")), str(record.get("idempotency_key", ""))
    )
    if record.get("operation_id") != expected_operation_id:
        errors.append("operation_id is not derived from {campaign_id, idempotency_key}")
    request = record.get("request")
    if not isinstance(request, dict):
        errors.append("request must be a JSON object")
    else:
        expected_request_digest = compute_request_digest(request)
        if record.get("request_digest") != expected_request_digest:
            errors.append("request_digest does not match canonical digest of the request")
    # Observation reference: exact shape when present.
    obs_ref = record.get("observation")
    if obs_ref is not None:
        if not isinstance(obs_ref, dict) or set(obs_ref) != OBSERVATION_REF_FIELDS:
            errors.append(
                "observation reference must contain exactly observation_id, "
                "observation_digest, outcome"
            )
        else:
            if not isinstance(obs_ref.get("observation_id"), str) or not OBSERVATION_ID_RE.match(
                obs_ref.get("observation_id", "")
            ):
                errors.append("observation reference observation_id must match ^srcobs_[0-9a-f]{64}$")
            if not isinstance(obs_ref.get("observation_digest"), str) or not SHA256_HEX_RE.match(
                obs_ref.get("observation_digest", "")
            ):
                errors.append(
                    "observation reference observation_digest must be a 64-hex SHA-256 digest"
                )
            if obs_ref.get("outcome") not in OUTCOMES:
                errors.append(
                    "observation reference outcome must be one of " + ", ".join(OUTCOMES)
                )
            if obs_ref.get("observation_id") != "srcobs_" + str(obs_ref.get("observation_digest", "")):
                errors.append(
                    "observation reference observation_id is not derived from observation_digest"
                )
    # Receipt: exact shape + deterministic semantics when present.
    receipt = record.get("receipt")
    if receipt is not None:
        errors.extend(validate_receipt_semantics(receipt))
        if isinstance(obs_ref, dict):
            expected_receipt = _build_receipt(
                operation_id=str(record.get("operation_id", "")),
                observation_id=str(obs_ref.get("observation_id", "")),
                observation_digest=str(obs_ref.get("observation_digest", "")),
                request_digest=str(record.get("request_digest", "")),
                outcome=str(obs_ref.get("outcome", "")),
            )
            if receipt != expected_receipt:
                errors.append("receipt is not deterministic for this operation")
    status = record.get("status")
    if status == "intent":
        if record.get("effect_stage") != "none":
            errors.append("intent requires effect_stage none")
        if record.get("observation") is not None:
            errors.append("intent must have a null observation")
        if record.get("receipt") is not None:
            errors.append("intent must have a null receipt")
        if record.get("previous_record_id") is not None:
            errors.append("intent must have a null previous_record_id")
    elif status in ("effect-observed", "receipt-recorded", "confirmed"):
        if record.get("effect_stage") != "response_observed":
            errors.append(f"{status} requires effect_stage response_observed")
        if record.get("observation") is None:
            errors.append(f"{status} requires a non-null observation")
        if status == "effect-observed" and record.get("receipt") is not None:
            errors.append("effect-observed must have a null receipt")
        if status in ("receipt-recorded", "confirmed") and record.get("receipt") is None:
            errors.append(f"{status} requires a non-null receipt")
        previous_record_id = record.get("previous_record_id")
        if not isinstance(previous_record_id, str) or not RECORD_ID_RE.match(previous_record_id):
            errors.append(f"{status} requires a previous_record_id linking the previous record")
    else:
        errors.append(f"unknown status {status!r}")
    return errors


def validate_lifecycle_chain(records: list[dict[str, Any]]) -> list[str]:
    """Return chain-continuity violations for the four lifecycle records.

    The chain must be exactly [intent, effect-observed, receipt-recorded,
    confirmed] in that order; each record's previous_record_id must link the
    previous record's record_id; and every record must carry identical
    operation_id/campaign_id/source_identity/adapter/request_digest/
    previous_content_digest/authority.
    """
    errors: list[str] = []
    if len(records) != 4:
        return ["lifecycle chain must contain exactly 4 records"]
    statuses = [record.get("status") for record in records]
    if statuses != list(LIFECYCLE_STATUSES):
        errors.append(
            f"lifecycle chain statuses must be {list(LIFECYCLE_STATUSES)}, found {statuses}"
        )
    for index, record in enumerate(records):
        if index == 0:
            if record.get("previous_record_id") is not None:
                errors.append("intent must have a null previous_record_id")
        else:
            if record.get("previous_record_id") != records[index - 1].get("record_id"):
                errors.append(
                    f"{record.get('status')} previous_record_id does not link "
                    f"{records[index - 1].get('status')} record_id"
                )
        for field in (
            "operation_id", "campaign_id", "source_identity", "adapter",
            "request_digest", "previous_content_digest", "authority",
        ):
            if record.get(field) != records[0].get(field):
                errors.append(f"{record.get('status')} {field} differs from intent")
    return errors


# ---------------------------------------------------------------------------
# Immutable-purpose intent matching (recovery guards)
# ---------------------------------------------------------------------------

def _intent_mismatch_errors(record: dict[str, Any], intent: dict[str, Any]) -> list[str]:
    """Return violations when a record's immutable purpose diverges from intent.

    Compares exactly operation_id, idempotency_key, campaign_id,
    source_identity, adapter, request, request_digest, previous_content_digest
    and authority. A locally tampered record that recomputed its own digests is
    still rejected here because the purpose fields diverge from the durable
    intent.
    """
    errors: list[str] = []
    for field in (
        "operation_id", "idempotency_key", "campaign_id", "source_identity",
        "adapter", "request", "request_digest", "previous_content_digest",
        "authority",
    ):
        if record.get(field) != intent.get(field):
            errors.append(f"{record.get('status')!r} {field} differs from the durable intent")
    return errors


def _validate_record_matches_intent(record: dict[str, Any], intent: dict[str, Any]) -> None:
    """Raise ImportOperationInvalid when a recovered record diverges from intent."""
    errors = _intent_mismatch_errors(record, intent)
    if errors:
        raise ImportOperationInvalid(
            "recovered import record does not match the durable intent: "
            + "; ".join(errors)
        )


def _observation_mismatch_errors(
    observation: dict[str, Any], stored_intent: dict[str, Any]
) -> list[str]:
    """Return violations when an observation diverges from the durable intent."""
    errors: list[str] = []
    for field in (
        "campaign_id", "operation_id", "source_identity", "adapter",
        "request_digest", "previous_content_digest",
    ):
        if observation.get(field) != stored_intent.get(field):
            errors.append(f"observation {field} does not match the intent")
    if observation.get("authority") != OBSERVATION_AUTHORITY:
        errors.append(
            "observation authority must equal the bounded source-observation block exactly"
        )
    return errors


# ---------------------------------------------------------------------------
# Sidecar access
# ---------------------------------------------------------------------------

def _artifact_path(campaign_dir: Path, artifact_id: str) -> Path:
    """Documented sidecar layout: intelligence/artifacts/<sha256(artifact_id)>.json."""
    return (
        Path(campaign_dir)
        / "intelligence"
        / "artifacts"
        / (intel.artifact_storage_key(artifact_id) + ".json")
    )


def _try_get_payload(
    campaign_dir: Path, campaign_id: str, artifact_id: str
) -> dict[str, Any] | None:
    """Return the stored artifact payload, or None when the artifact does not exist."""
    path = _artifact_path(campaign_dir, artifact_id)
    if not path.exists():
        return None
    envelope = intel.get_artifact(campaign_dir, campaign_id, artifact_id)["artifact"]
    return envelope["payload"]


def _put_artifact(
    campaign_dir: Path,
    campaign_id: str,
    campaign_revision: int,
    kind: str,
    artifact_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    try:
        return intel.put_artifact(
            campaign_dir, campaign_id, campaign_revision, kind, artifact_id, payload
        )
    except intel.IntelligenceArtifactConflict as exc:
        raise ImportOperationConflict(str(exc)) from exc


# ---------------------------------------------------------------------------
# Recovery-chain validation
# ---------------------------------------------------------------------------

def _validate_intent_matches(stored: dict[str, Any], expected: dict[str, Any]) -> None:
    """Replay guard: the stored intent must match every identity-bearing field."""
    for field in (
        "campaign_id", "operation_id", "idempotency_key", "source_identity",
        "adapter", "request_digest", "request", "previous_content_digest", "authority",
    ):
        if stored.get(field) != expected.get(field):
            raise ImportOperationConflict(
                f"import operation {expected.get('operation_id')!r} already exists "
                f"with a different {field}"
            )
    sem_errors = validate_import_record_semantics(stored)
    if sem_errors:
        raise ImportOperationInvalid(
            "stored intent record is inconsistent: " + "; ".join(sem_errors)
        )
    if stored.get("status") != "intent":
        raise ImportOperationInvalid("stored intent record has a status other than intent")


def _validate_stored_observation(
    observation: dict[str, Any], stored_intent: dict[str, Any]
) -> None:
    """Replay guard: the stored observation must satisfy the full contract and
    match the durable intent for every identity-bearing field."""
    errors = validate_source_observation_semantics(observation)
    errors.extend(_observation_mismatch_errors(observation, stored_intent))
    if errors:
        raise ImportOperationInvalid(
            "stored source observation is inconsistent: " + "; ".join(errors)
        )


def _validate_recovered_chain(
    stored_intent: dict[str, Any],
    effect: dict[str, Any] | None,
    receipt_recorded: dict[str, Any] | None,
    observation: dict[str, Any],
) -> None:
    """Validate a partially recovered chain before extending it.

    Every recovered record is validated locally AND against the full
    immutable-purpose equality with the durable intent; exact previous-record
    causality and exact observation-reference continuity are enforced. A record
    from another operation/source is rejected even when its own digests are
    internally consistent.
    """
    errors: list[str] = []
    errors.extend(validate_import_record_semantics(stored_intent))
    errors.extend(validate_source_observation_semantics(observation))
    errors.extend(_observation_mismatch_errors(observation, stored_intent))
    if effect is not None:
        errors.extend(validate_import_record_semantics(effect))
        errors.extend(_intent_mismatch_errors(effect, stored_intent))
        if effect.get("status") != "effect-observed":
            errors.append("stored record has a status other than effect-observed")
        if effect.get("previous_record_id") != stored_intent.get("record_id"):
            errors.append("effect-observed previous_record_id does not link the intent")
        if effect.get("observation") != _observation_ref(observation):
            errors.append("effect-observed observation reference does not match the stored observation")
    if receipt_recorded is not None:
        errors.extend(validate_import_record_semantics(receipt_recorded))
        errors.extend(_intent_mismatch_errors(receipt_recorded, stored_intent))
        if receipt_recorded.get("status") != "receipt-recorded":
            errors.append("stored record has a status other than receipt-recorded")
        if effect is not None and receipt_recorded.get("previous_record_id") != effect.get("record_id"):
            errors.append("receipt-recorded previous_record_id does not link effect-observed")
        if receipt_recorded.get("observation") != _observation_ref(observation):
            errors.append("receipt-recorded observation reference does not match the stored observation")
        receipt = receipt_recorded.get("receipt")
        if not isinstance(receipt, dict):
            errors.append("receipt-recorded has a null receipt")
        else:
            errors.extend(validate_receipt_semantics(receipt))
            expected_receipt = _build_receipt(
                operation_id=stored_intent["operation_id"],
                observation_id=observation["observation_id"],
                observation_digest=observation["observation_digest"],
                request_digest=stored_intent["request_digest"],
                outcome=observation["outcome"],
            )
            if receipt != expected_receipt:
                errors.append("receipt-recorded receipt is not deterministic for this operation")
    if errors:
        raise ImportOperationInvalid(
            "recovered import chain is inconsistent: " + "; ".join(errors)
        )


def _validate_confirmed_chain(
    campaign_dir: Path,
    campaign_id: str,
    stored_intent: dict[str, Any],
    confirmed: dict[str, Any],
    effect_artifact_id: str,
    receipt_artifact_id: str,
    observation_artifact_id: str,
) -> None:
    effect = _try_get_payload(campaign_dir, campaign_id, effect_artifact_id)
    receipt_recorded = _try_get_payload(campaign_dir, campaign_id, receipt_artifact_id)
    observation = _try_get_payload(campaign_dir, campaign_id, observation_artifact_id)
    if effect is None or receipt_recorded is None or observation is None:
        raise ImportOperationInvalid(
            "confirmed exists without the full lifecycle chain"
        )
    _validate_recovered_chain(stored_intent, effect, receipt_recorded, observation)
    _validate_record_matches_intent(confirmed, stored_intent)
    errors = validate_import_record_semantics(confirmed)
    # Complete ordered chain intent -> effect-observed -> receipt-recorded ->
    # confirmed with exact causal links and immutable-purpose field equality.
    errors.extend(validate_lifecycle_chain([stored_intent, effect, receipt_recorded, confirmed]))
    if confirmed.get("status") != "confirmed":
        errors.append("stored record has a status other than confirmed")
    if confirmed.get("previous_record_id") != receipt_recorded.get("record_id"):
        errors.append("confirmed previous_record_id does not link receipt-recorded")
    if confirmed.get("observation") != _observation_ref(observation):
        errors.append("confirmed observation reference does not match the stored observation")
    if confirmed.get("receipt") != receipt_recorded.get("receipt"):
        errors.append("confirmed receipt does not match receipt-recorded")
    if errors:
        raise ImportOperationInvalid(
            "stored confirmed record is inconsistent: " + "; ".join(errors)
        )


def _result_from_confirmed(confirmed: dict[str, Any]) -> dict[str, Any]:
    observation = confirmed["observation"]
    receipt = confirmed["receipt"]
    return {
        "ok": True,
        "status": "confirmed",
        "operation_id": confirmed["operation_id"],
        "request_digest": confirmed["request_digest"],
        "observation_id": observation["observation_id"],
        "observation_digest": observation["observation_digest"],
        "outcome": observation["outcome"],
        "receipt_id": receipt["receipt_id"],
        "receipt_digest": receipt["receipt_digest"],
        "confirmed_record_id": confirmed["record_id"],
    }


# ---------------------------------------------------------------------------
# Construction validation (internal bugs must fail before persistence)
# ---------------------------------------------------------------------------

def _validate_constructed_intent(record: dict[str, Any]) -> None:
    """Requirement: every newly built intent record is validated before persist."""
    errors = validate_import_record_semantics(record)
    if record.get("status") != "intent":
        errors.append("constructed intent record has a status other than intent")
    if errors:
        raise ImportOperationInvalid(
            "constructed intent record is invalid: " + "; ".join(errors)
        )


def _validate_constructed_observation(
    observation: dict[str, Any], stored_intent: dict[str, Any]
) -> None:
    """Requirement: every newly built observation is validated before persist."""
    errors = validate_source_observation_semantics(observation)
    errors.extend(_observation_mismatch_errors(observation, stored_intent))
    if errors:
        raise ImportOperationInvalid(
            "constructed source observation is invalid: " + "; ".join(errors)
        )


def _validate_constructed_record(
    record: dict[str, Any],
    stored_intent: dict[str, Any],
    previous_record: dict[str, Any] | None,
) -> None:
    """Requirement: every newly built lifecycle record is validated before
    persist: local semantics, immutable-purpose equality with intent, and exact
    causality linking the previous record."""
    errors = validate_import_record_semantics(record)
    errors.extend(_intent_mismatch_errors(record, stored_intent))
    expected_previous = None if previous_record is None else previous_record.get("record_id")
    if record.get("previous_record_id") != expected_previous:
        previous_label = (
            "previous" if previous_record is None else previous_record.get("status")
        )
        errors.append(f"{record.get('status')} previous_record_id does not link the {previous_label} record")
    if errors:
        raise ImportOperationInvalid(
            "constructed import record is invalid: " + "; ".join(errors)
        )


# ---------------------------------------------------------------------------
# Public operation
# ---------------------------------------------------------------------------

def execute_import_operation(
    campaign_dir: str | Path,
    campaign_id: str,
    campaign_revision: int,
    idempotency_key: str,
    source_identity: dict[str, Any],
    adapter: str,
    request: dict[str, Any],
    previous_content_digest: str | None,
    reader: Callable[[dict[str, Any]], Any],
) -> dict[str, Any]:
    """Execute one state-first, idempotent read-only import operation.

    State ordering: the intent record is persisted BEFORE the reader is
    invoked. Replay/recovery order: confirmed -> receipt-recorded ->
    effect-observed -> source-observation -> intent. If the confirmed record
    exists the result is returned without invoking the reader; if the source
    observation exists the chain is completed without rereading; if only the
    intent exists the reader is invoked once.

    The reader is called with exactly ``{source_identity, adapter, request,
    request_digest}`` and must follow the typed return protocol. Only
    TimeoutError is translated (to an unavailable outcome with reason_code
    "timeout"); unexpected exceptions escape with the intent already
    durably persisted.
    """
    campaign_dir = Path(campaign_dir)
    _validate_campaign_id(campaign_id)
    _validate_idempotency_key(idempotency_key)
    _validate_source_identity(source_identity)
    _validate_adapter(adapter)
    _validate_request(request)
    _validate_previous_content_digest(previous_content_digest)
    if not isinstance(campaign_revision, int) or campaign_revision < 0:
        raise ImportOperationInvalid("campaign_revision must be a non-negative integer")

    # Finding 2: authoritative internal snapshots. All operation IDs, digests,
    # intent records, observations, and lifecycle records use ONLY these
    # snapshots; the reader receives separate defensive copies that share no
    # mutable object with stored intent / internal operation identity.
    authoritative_source_identity = copy.deepcopy(source_identity)
    authoritative_request = copy.deepcopy(request)

    # P1-002 policy: reject secret/raw content BEFORE any artifact is persisted.
    _reject_secret_or_raw(authoritative_request, "request")

    request_digest = compute_request_digest(authoritative_request)
    operation_id = derive_operation_id(campaign_id, idempotency_key)

    intent_artifact_id = f"import-operation/{operation_id}/intent"
    observation_artifact_id = f"source-observation/{operation_id}"
    effect_artifact_id = f"import-operation/{operation_id}/effect-observed"
    receipt_artifact_id = f"import-operation/{operation_id}/receipt-recorded"
    confirmed_artifact_id = f"import-operation/{operation_id}/confirmed"

    authority = dict(IMPORT_AUTHORITY)

    # Build the intent record (state-first: persisted before the reader runs).
    intent_record = _build_import_record(
        operation_id=operation_id,
        idempotency_key=idempotency_key,
        campaign_id=campaign_id,
        source_identity=authoritative_source_identity,
        adapter=adapter,
        request=authoritative_request,
        request_digest=request_digest,
        previous_content_digest=previous_content_digest,
        status="intent",
        effect_stage="none",
        observation=None,
        receipt=None,
        previous_record_id=None,
        authority=authority,
        created_at=intel.utcnow(),
    )
    _reject_secret_or_raw(intent_record, "import operation")
    _validate_constructed_intent(intent_record)

    stored_intent = _try_get_payload(campaign_dir, campaign_id, intent_artifact_id)
    if stored_intent is None:
        _put_artifact(
            campaign_dir, campaign_id, campaign_revision,
            "decision", intent_artifact_id, intent_record,
        )
        stored_intent = intent_record
    else:
        # Replay guard: mismatched identity fields conflict BEFORE the reader.
        _validate_intent_matches(stored_intent, intent_record)

    # Recovery chain: confirmed -> receipt-recorded -> effect-observed ->
    # source-observation -> intent.
    confirmed = _try_get_payload(campaign_dir, campaign_id, confirmed_artifact_id)
    if confirmed is not None:
        _validate_confirmed_chain(
            campaign_dir, campaign_id, stored_intent, confirmed,
            effect_artifact_id, receipt_artifact_id, observation_artifact_id,
        )
        return _result_from_confirmed(confirmed)

    receipt_recorded = _try_get_payload(campaign_dir, campaign_id, receipt_artifact_id)
    if receipt_recorded is not None:
        effect = _try_get_payload(campaign_dir, campaign_id, effect_artifact_id)
        observation = _try_get_payload(campaign_dir, campaign_id, observation_artifact_id)
        if effect is None or observation is None:
            raise ImportOperationInvalid(
                "receipt-recorded exists without effect-observed or source observation"
            )
        _validate_recovered_chain(stored_intent, effect, receipt_recorded, observation)
        confirmed_record = _build_import_record(
            operation_id=operation_id,
            idempotency_key=idempotency_key,
            campaign_id=campaign_id,
            source_identity=authoritative_source_identity,
            adapter=adapter,
            request=authoritative_request,
            request_digest=request_digest,
            previous_content_digest=previous_content_digest,
            status="confirmed",
            effect_stage="response_observed",
            observation=_observation_ref(observation),
            receipt=receipt_recorded["receipt"],
            previous_record_id=receipt_recorded["record_id"],
            authority=authority,
            created_at=intel.utcnow(),
        )
        _validate_constructed_record(confirmed_record, stored_intent, receipt_recorded)
        _put_artifact(
            campaign_dir, campaign_id, campaign_revision,
            "decision", confirmed_artifact_id, confirmed_record,
        )
        return _result_from_confirmed(confirmed_record)

    effect = _try_get_payload(campaign_dir, campaign_id, effect_artifact_id)
    if effect is not None:
        observation = _try_get_payload(campaign_dir, campaign_id, observation_artifact_id)
        if observation is None:
            raise ImportOperationInvalid("effect-observed exists without source observation")
        _validate_recovered_chain(stored_intent, effect, None, observation)
        receipt = _build_receipt(
            operation_id=operation_id,
            observation_id=observation["observation_id"],
            observation_digest=observation["observation_digest"],
            request_digest=request_digest,
            outcome=observation["outcome"],
        )
        receipt_recorded = _build_import_record(
            operation_id=operation_id,
            idempotency_key=idempotency_key,
            campaign_id=campaign_id,
            source_identity=authoritative_source_identity,
            adapter=adapter,
            request=authoritative_request,
            request_digest=request_digest,
            previous_content_digest=previous_content_digest,
            status="receipt-recorded",
            effect_stage="response_observed",
            observation=_observation_ref(observation),
            receipt=receipt,
            previous_record_id=effect["record_id"],
            authority=authority,
            created_at=intel.utcnow(),
        )
        _validate_constructed_record(receipt_recorded, stored_intent, effect)
        _put_artifact(
            campaign_dir, campaign_id, campaign_revision,
            "decision", receipt_artifact_id, receipt_recorded,
        )
        confirmed_record = _build_import_record(
            operation_id=operation_id,
            idempotency_key=idempotency_key,
            campaign_id=campaign_id,
            source_identity=authoritative_source_identity,
            adapter=adapter,
            request=authoritative_request,
            request_digest=request_digest,
            previous_content_digest=previous_content_digest,
            status="confirmed",
            effect_stage="response_observed",
            observation=_observation_ref(observation),
            receipt=receipt,
            previous_record_id=receipt_recorded["record_id"],
            authority=authority,
            created_at=intel.utcnow(),
        )
        _validate_constructed_record(confirmed_record, stored_intent, receipt_recorded)
        _put_artifact(
            campaign_dir, campaign_id, campaign_revision,
            "decision", confirmed_artifact_id, confirmed_record,
        )
        return _result_from_confirmed(confirmed_record)

    observation = _try_get_payload(campaign_dir, campaign_id, observation_artifact_id)
    if observation is not None:
        # Observation exists: complete the chain WITHOUT rereading the source.
        _validate_stored_observation(observation, stored_intent)
        effect_record = _build_import_record(
            operation_id=operation_id,
            idempotency_key=idempotency_key,
            campaign_id=campaign_id,
            source_identity=authoritative_source_identity,
            adapter=adapter,
            request=authoritative_request,
            request_digest=request_digest,
            previous_content_digest=previous_content_digest,
            status="effect-observed",
            effect_stage="response_observed",
            observation=_observation_ref(observation),
            receipt=None,
            previous_record_id=stored_intent["record_id"],
            authority=authority,
            created_at=intel.utcnow(),
        )
        _validate_constructed_record(effect_record, stored_intent, stored_intent)
        _put_artifact(
            campaign_dir, campaign_id, campaign_revision,
            "decision", effect_artifact_id, effect_record,
        )
        receipt = _build_receipt(
            operation_id=operation_id,
            observation_id=observation["observation_id"],
            observation_digest=observation["observation_digest"],
            request_digest=request_digest,
            outcome=observation["outcome"],
        )
        receipt_recorded = _build_import_record(
            operation_id=operation_id,
            idempotency_key=idempotency_key,
            campaign_id=campaign_id,
            source_identity=authoritative_source_identity,
            adapter=adapter,
            request=authoritative_request,
            request_digest=request_digest,
            previous_content_digest=previous_content_digest,
            status="receipt-recorded",
            effect_stage="response_observed",
            observation=_observation_ref(observation),
            receipt=receipt,
            previous_record_id=effect_record["record_id"],
            authority=authority,
            created_at=intel.utcnow(),
        )
        _validate_constructed_record(receipt_recorded, stored_intent, effect_record)
        _put_artifact(
            campaign_dir, campaign_id, campaign_revision,
            "decision", receipt_artifact_id, receipt_recorded,
        )
        confirmed_record = _build_import_record(
            operation_id=operation_id,
            idempotency_key=idempotency_key,
            campaign_id=campaign_id,
            source_identity=authoritative_source_identity,
            adapter=adapter,
            request=authoritative_request,
            request_digest=request_digest,
            previous_content_digest=previous_content_digest,
            status="confirmed",
            effect_stage="response_observed",
            observation=_observation_ref(observation),
            receipt=receipt,
            previous_record_id=receipt_recorded["record_id"],
            authority=authority,
            created_at=intel.utcnow(),
        )
        _validate_constructed_record(confirmed_record, stored_intent, receipt_recorded)
        _put_artifact(
            campaign_dir, campaign_id, campaign_revision,
            "decision", confirmed_artifact_id, confirmed_record,
        )
        return _result_from_confirmed(confirmed_record)

    # Only the intent exists: invoke the reader once. Only TimeoutError is
    # translated; unexpected exceptions escape with the intent intact.
    #
    # Finding 2: the reader receives defensive copies of the authoritative
    # snapshots that share NO mutable object with stored intent / internal
    # operation identity / later lifecycle builders. A buggy or adversarial
    # reader cannot mutate the durable operation.
    reader_context = {
        "source_identity": copy.deepcopy(authoritative_source_identity),
        "adapter": adapter,
        "request": copy.deepcopy(authoritative_request),
        "request_digest": request_digest,
    }
    try:
        raw_result = reader(reader_context)
    except TimeoutError:
        raw_result = {"status": "unavailable", "reason_code": "timeout"}
    parsed = _parse_reader_result(raw_result)
    parsed["outcome"] = _map_outcome(parsed, previous_content_digest)

    # Finding 2 invariant guard: after the reader returns, the internal request
    # digest must still match the authoritative snapshot.
    if compute_request_digest(authoritative_request) != request_digest:
        raise ImportOperationInvalid(
            "internal request digest invariant violated after reader execution"
        )

    observation = _build_observation(
        campaign_id=campaign_id,
        operation_id=operation_id,
        source_identity=authoritative_source_identity,
        adapter=adapter,
        request_digest=request_digest,
        previous_content_digest=previous_content_digest,
        parsed=parsed,
        observed_at=intel.utcnow(),
    )
    _validate_constructed_observation(observation, stored_intent)
    _put_artifact(
        campaign_dir, campaign_id, campaign_revision,
        "backlog", observation_artifact_id, observation,
    )

    effect_record = _build_import_record(
        operation_id=operation_id,
        idempotency_key=idempotency_key,
        campaign_id=campaign_id,
        source_identity=authoritative_source_identity,
        adapter=adapter,
        request=authoritative_request,
        request_digest=request_digest,
        previous_content_digest=previous_content_digest,
        status="effect-observed",
        effect_stage="response_observed",
        observation=_observation_ref(observation),
        receipt=None,
        previous_record_id=stored_intent["record_id"],
        authority=authority,
        created_at=intel.utcnow(),
    )
    _validate_constructed_record(effect_record, stored_intent, stored_intent)
    _put_artifact(
        campaign_dir, campaign_id, campaign_revision,
        "decision", effect_artifact_id, effect_record,
    )

    receipt = _build_receipt(
        operation_id=operation_id,
        observation_id=observation["observation_id"],
        observation_digest=observation["observation_digest"],
        request_digest=request_digest,
        outcome=observation["outcome"],
    )
    receipt_recorded = _build_import_record(
        operation_id=operation_id,
        idempotency_key=idempotency_key,
        campaign_id=campaign_id,
        source_identity=authoritative_source_identity,
        adapter=adapter,
        request=authoritative_request,
        request_digest=request_digest,
        previous_content_digest=previous_content_digest,
        status="receipt-recorded",
        effect_stage="response_observed",
        observation=_observation_ref(observation),
        receipt=receipt,
        previous_record_id=effect_record["record_id"],
        authority=authority,
        created_at=intel.utcnow(),
    )
    _validate_constructed_record(receipt_recorded, stored_intent, effect_record)
    _put_artifact(
        campaign_dir, campaign_id, campaign_revision,
        "decision", receipt_artifact_id, receipt_recorded,
    )

    confirmed_record = _build_import_record(
        operation_id=operation_id,
        idempotency_key=idempotency_key,
        campaign_id=campaign_id,
        source_identity=authoritative_source_identity,
        adapter=adapter,
        request=authoritative_request,
        request_digest=request_digest,
        previous_content_digest=previous_content_digest,
        status="confirmed",
        effect_stage="response_observed",
        observation=_observation_ref(observation),
        receipt=receipt,
        previous_record_id=receipt_recorded["record_id"],
        authority=authority,
        created_at=intel.utcnow(),
    )
    _validate_constructed_record(confirmed_record, stored_intent, receipt_recorded)
    _put_artifact(
        campaign_dir, campaign_id, campaign_revision,
        "decision", confirmed_artifact_id, confirmed_record,
    )

    return _result_from_confirmed(confirmed_record)


if __name__ == "__main__":
    raise SystemExit(
        "myrmex_backlog_import.py is a library module; call "
        "execute_import_operation() from your import adapter."
    )
