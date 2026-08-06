#!/usr/bin/env python3
"""Atomic campaign-intelligence sidecar store for Myrmex.

Stdlib-only immutable artifact store that lives beneath each existing campaign
directory. It stores four immutable artifact kinds (backlog, plan, review,
decision) and maintains one reconstructible, non-authoritative index
(projection.json). It never mutates campaign.json, the campaign revision/CAS,
or the myrmex.campaign/v1 execution projection, and it has no repository-effect
authority: it does not activate plans or create work units.

Layout under the resolved campaign directory::

    intelligence/
      lock
      artifacts/<sha256-of-artifact-id>.json
      projection.json

Directories are mode 0700 and files mode 0600. Every managed path rejects
symbolic links and must stay beneath the campaign directory. Artifact
filenames are derived only from the SHA-256 of the validated artifact ID.

Public operations:

    put_artifact       create or reuse one immutable artifact, then rebuild the
                       projection under the same exclusive lock
    get_artifact       resolve one artifact by ID, validating the envelope
                       directly from the artifact file (never the projection)
    list_artifacts     read the projection, validate its source_digest, return
                       descriptors (never payloads); missing/stale projections
                       are reported without writing
    rebuild_projection reconstruct projection.json from every stored artifact,
                       rejecting corrupt artifacts instead of skipping them
    doctor             read-only health report: healthy, projection_missing,
                       projection_stale, artifact_corrupt, backend_unavailable

CLI stable error codes (used by bin/myrmex-campaign intelligence-* commands):

    0  success (created / reused / healthy / found / rebuilt / list status)
    1  invalid campaign or artifact id, missing campaign, input policy
       rejection (malformed, non-object, inside repository, symlink,
       non-regular, secret/raw content)
    2  artifact conflict (same artifact ID, different kind or payload)
    3  backend unavailable or internal storage error
    4  artifact not found

Argparse-level usage errors (for example an unrecognized --kind choice) exit
with code 2 in the standard argparse convention; unlike a conflict, they print
usage text and never a payload.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

ARTIFACT_SCHEMA = "myrmex.campaign-intelligence-artifact/v1"
PROJECTION_SCHEMA = "myrmex.campaign-intelligence-projection/v1"
ALLOWED_ARTIFACT_KINDS = ("backlog", "plan", "review", "decision")

CAMPAIGN_ID_RE = re.compile(r"^camp-[a-z0-9][a-z0-9-]{4,60}$")
ARTIFACT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")

INTELLIGENCE_DIR_NAME = "intelligence"
ARTIFACTS_DIR_NAME = "artifacts"
LOCK_FILE_NAME = "lock"
PROJECTION_FILE_NAME = "projection.json"

_DIR_MODE = 0o700
_FILE_MODE = 0o600

_SENSITIVE_KEY_NAMES = frozenset({
    "password", "secret", "api_key", "private_key",
    "access_token", "refresh_token", "authorization",
    "cookie", "credential",
})
_SENSITIVE_KEY_SUFFIXES = frozenset({
    "password", "secret", "api_key", "private_key",
    "access_token", "refresh_token", "authorization",
})
_RAW_CONTENT_KEYS = frozenset({
    "source_code", "source_text", "file_content", "file_contents",
    "raw_file", "raw_files", "repository_snapshot",
    "raw_diff", "patch", "raw_log", "raw_prompt",
})

# The exact, closed set of artifact-envelope fields. Envelopes carrying any
# additional field (or missing any of these) are invalid; unknown field values
# are never echoed in validation errors.
_ARTIFACT_ENVELOPE_FIELDS = frozenset({
    "schema", "campaign_id", "artifact_id", "kind",
    "artifact_digest", "payload_digest", "observed_campaign_revision",
    "created_at", "payload",
})


def _compact_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


_SENSITIVE_COMPACT = frozenset(_compact_key(k) for k in _SENSITIVE_KEY_NAMES)
_SENSITIVE_SUFFIX_COMPACT = frozenset(_compact_key(k) for k in _SENSITIVE_KEY_SUFFIXES)
_RAW_COMPACT = frozenset(_compact_key(k) for k in _RAW_CONTENT_KEYS)

# Value-scanning patterns. They identify the rejected category without ever
# echoing the matched value in an error message.
_PEM_RE = re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----", re.I)
_TOKEN_RE = re.compile(r"\b(?:sk|gh[opsu])-[A-Za-z0-9_-]{12,}\b", re.I)
_GH_PAT_RE = re.compile(r"(?<![A-Za-z0-9_])gh[opsur]_[A-Za-z0-9]{36}(?![A-Za-z0-9_])")
_GITHUB_PAT_RE = re.compile(r"(?<![A-Za-z0-9_])github_pat_[A-Za-z0-9_]{82}(?![A-Za-z0-9_])")
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~-]{12,}\b", re.I)
_ASSIGNMENT_RE = re.compile(
    r"\b(?:api[_-]?key|password|secret|authorization|cookie|token|credential|"
    r"access_token|refresh_token|private_key)\s*[:=]\s*[^\s]{6,}",
    re.I,
)

METADATA_KEYS_ALLOWED = frozenset({
    "source_identity", "source_path", "repository_path",
    "diff_digest", "file_digest", "credential_required",
})


class IntelligenceStoreError(Exception):
    """Base class for campaign-intelligence store failures."""


class IntelligenceBackendUnavailable(IntelligenceStoreError):
    """The locking backend (fcntl) is unavailable; writes fail closed."""


class IntelligenceArtifactConflict(IntelligenceStoreError):
    """An artifact with the same ID already exists with different content."""


class IntelligenceArtifactInvalid(IntelligenceStoreError):
    """An artifact path, envelope, or identifier is invalid."""


class IntelligencePayloadRejected(IntelligenceStoreError):
    """Input payload violates the JSON-object-only, secret/raw-content policy."""


class IntelligenceProjectionInvalid(IntelligenceStoreError):
    """The projection is corrupt, stale, or cannot be rebuilt."""


class IntelligenceCampaignMismatch(IntelligenceStoreError):
    """An artifact or projection belongs to a different campaign."""


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def canonical_json_bytes(value: Any) -> bytes:
    """Deterministic canonical JSON: UTF-8, sorted keys, compact separators."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def artifact_storage_key(artifact_id: str) -> str:
    """Storage filename stem: lowercase SHA-256 of the validated artifact ID."""
    return sha256_hex(artifact_id.encode("utf-8"))


def compute_payload_digest(payload: dict[str, Any]) -> str:
    return sha256_hex(canonical_json_bytes(payload))


def compute_artifact_digest(
    campaign_id: str, kind: str, artifact_id: str, payload: dict[str, Any]
) -> str:
    core = {
        "campaign_id": campaign_id,
        "kind": kind,
        "artifact_id": artifact_id,
        "payload": payload,
    }
    return sha256_hex(canonical_json_bytes(core))


def _validate_campaign_id(campaign_id: str) -> None:
    if not CAMPAIGN_ID_RE.match(campaign_id):
        raise IntelligenceArtifactInvalid(
            "invalid campaign id; must match ^camp-[a-z0-9][a-z0-9-]{4,60}$"
        )


def _validate_kind(kind: str) -> None:
    if kind not in ALLOWED_ARTIFACT_KINDS:
        raise IntelligenceArtifactInvalid(f"invalid artifact kind {kind!r}")


def _validate_artifact_id(artifact_id: str) -> None:
    if not isinstance(artifact_id, str) or not ARTIFACT_ID_RE.match(artifact_id):
        raise IntelligenceArtifactInvalid(
            "invalid artifact id; must match ^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$"
        )


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _iroot(campaign_dir: Path) -> Path:
    return campaign_dir / INTELLIGENCE_DIR_NAME


def _ensure_dir(path: Path, mode: int = _DIR_MODE) -> None:
    if path.is_symlink():
        raise IntelligenceStoreError(f"{path.name} path is a symbolic link")
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise IntelligenceStoreError(f"cannot create {path.name} directory: {exc}") from exc
    if not path.is_dir():
        raise IntelligenceStoreError(f"{path.name} is not a directory")
    try:
        os.chmod(path, mode)
    except OSError as exc:
        raise IntelligenceStoreError(f"cannot set mode on {path.name}: {exc}") from exc


def _require_regular_non_symlink(path: Path, label: str) -> None:
    try:
        st = path.lstat()
    except FileNotFoundError:
        raise IntelligenceArtifactInvalid(f"{label} is missing: {path.name}") from None
    if stat.S_ISLNK(st.st_mode):
        raise IntelligenceStoreError(f"{label} is a symbolic link")
    if not stat.S_ISREG(st.st_mode):
        raise IntelligenceStoreError(f"{label} is not a regular file")


def _fsync_dir(path: Path) -> None:
    """Best-effort directory fsync; strict file fsync is handled at write time."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------

@contextmanager
def intelligence_lock(campaign_dir: Path) -> Iterator[None]:
    """Exclusive per-campaign flock; fails closed when fcntl is unavailable."""
    campaign_dir = Path(campaign_dir)
    try:
        import fcntl  # type: ignore
    except ImportError:
        raise IntelligenceBackendUnavailable(
            "fcntl is unavailable; cannot acquire the campaign intelligence lock"
        ) from None

    iroot = _iroot(campaign_dir)
    _ensure_dir(iroot)
    lock_path = iroot / LOCK_FILE_NAME
    if lock_path.exists():
        _require_regular_non_symlink(lock_path, "intelligence lock")
    flags = os.O_CREAT | os.O_RDWR
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(lock_path, flags, _FILE_MODE)
    except OSError as exc:
        raise IntelligenceBackendUnavailable(
            f"cannot open campaign intelligence lock: {exc}"
        ) from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise IntelligenceStoreError("intelligence lock is not a regular file")
        os.chmod(lock_path, _FILE_MODE)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError as exc:
            raise IntelligenceBackendUnavailable(
                f"cannot lock campaign intelligence: {exc}"
            ) from exc
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception:
            pass
        os.close(fd)


# ---------------------------------------------------------------------------
# Artifact persistence
# ---------------------------------------------------------------------------

def _write_durable(final_path: Path, data: bytes, prefix: str) -> None:
    """Write bytes to a unique O_EXCL temp file, fsync, then atomically replace."""
    fd, tmp_name = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=str(final_path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, _FILE_MODE)
        os.replace(tmp_name, final_path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _write_artifact_durable(final_path: Path, envelope: dict[str, Any]) -> None:
    data = canonical_json_bytes(envelope) + b"\n"
    _write_durable(final_path, data, prefix=".artifact-")
    _fsync_dir(final_path.parent)


def _write_projection_durable(campaign_dir: Path, projection: dict[str, Any]) -> None:
    iroot = _iroot(campaign_dir)
    _ensure_dir(iroot)
    target = iroot / PROJECTION_FILE_NAME
    if target.is_symlink():
        raise IntelligenceStoreError("projection path is a symbolic link")
    data = canonical_json_bytes(projection) + b"\n"
    _write_durable(target, data, prefix=".projection-")
    _fsync_dir(iroot)


def _validate_artifact_envelope(
    envelope: Any, campaign_id: str, storage_key: str | None = None
) -> dict[str, Any]:
    if not isinstance(envelope, dict):
        raise IntelligenceArtifactInvalid("artifact envelope is not a JSON object")
    actual_fields = set(envelope.keys())
    if actual_fields != _ARTIFACT_ENVELOPE_FIELDS:
        extra = sorted(actual_fields - _ARTIFACT_ENVELOPE_FIELDS)
        missing = sorted(_ARTIFACT_ENVELOPE_FIELDS - actual_fields)
        raise IntelligenceArtifactInvalid(
            "artifact envelope must contain exactly the fields "
            "schema, campaign_id, artifact_id, kind, artifact_digest, "
            "payload_digest, observed_campaign_revision, created_at, payload"
            + (f"; unexpected field(s): {extra!r}" if extra else "")
            + (f"; missing field(s): {missing!r}" if missing else "")
        )
    if envelope.get("schema") != ARTIFACT_SCHEMA:
        raise IntelligenceArtifactInvalid(
            f"unsupported artifact schema {envelope.get('schema')!r}"
        )
    cid = envelope.get("campaign_id")
    if cid != campaign_id:
        raise IntelligenceCampaignMismatch(
            f"artifact belongs to campaign {cid!r}, expected {campaign_id!r}"
        )
    aid = envelope.get("artifact_id")
    if not isinstance(aid, str) or not ARTIFACT_ID_RE.match(aid):
        raise IntelligenceArtifactInvalid("artifact envelope has an invalid artifact_id")
    kind = envelope.get("kind")
    if kind not in ALLOWED_ARTIFACT_KINDS:
        raise IntelligenceArtifactInvalid(
            f"artifact envelope has invalid kind {kind!r}"
        )
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        raise IntelligenceArtifactInvalid("artifact envelope payload is not a JSON object")
    # Re-apply the content-safety invariant on persisted artifacts. A tampered
    # artifact whose payload carries secret/raw content is treated as an
    # artifact-invalid failure (read/doctor/rebuild semantics); the converted
    # message never carries secret material.
    try:
        reject_secret_or_raw(payload)
    except IntelligencePayloadRejected as exc:
        raise IntelligenceArtifactInvalid(
            "artifact payload violates the content-safety policy"
        ) from exc
    if compute_payload_digest(payload) != envelope.get("payload_digest"):
        raise IntelligenceArtifactInvalid(
            "artifact payload_digest does not match the payload"
        )
    if (
        compute_artifact_digest(cid, kind, aid, payload)
        != envelope.get("artifact_digest")
    ):
        raise IntelligenceArtifactInvalid(
            "artifact artifact_digest does not match the payload"
        )
    if not isinstance(envelope.get("created_at"), str) or not envelope.get("created_at"):
        raise IntelligenceArtifactInvalid("artifact envelope is missing created_at")
    if not isinstance(envelope.get("observed_campaign_revision"), int):
        raise IntelligenceArtifactInvalid(
            "artifact envelope is missing observed_campaign_revision"
        )
    if storage_key is not None and artifact_storage_key(aid) != storage_key:
        raise IntelligenceArtifactInvalid(
            "artifact filename does not match the artifact id digest"
        )
    return envelope


def _read_artifact_file(path: Path, campaign_id: str) -> dict[str, Any]:
    _require_regular_non_symlink(path, "artifact file")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise IntelligenceArtifactInvalid(
            f"cannot read artifact file {path.name}: {exc}"
        ) from exc
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise IntelligenceArtifactInvalid(
            f"artifact file {path.name} is not valid JSON"
        ) from exc
    storage_key = path.name[: -len(".json")]
    return _validate_artifact_envelope(envelope, campaign_id, storage_key=storage_key)


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------

def _compute_projection_source_digest(
    campaign_id: str, artifact_count: int, artifacts: dict[str, Any]
) -> str:
    core = {
        "campaign_id": campaign_id,
        "artifact_count": artifact_count,
        "artifacts": artifacts,
    }
    return sha256_hex(canonical_json_bytes(core))


def _read_projection_file(campaign_dir: Path, campaign_id: str) -> dict[str, Any]:
    """Read and fully validate the stored projection (schema, owner, digest)."""
    proj_path = _iroot(campaign_dir) / PROJECTION_FILE_NAME
    _require_regular_non_symlink(proj_path, "projection file")
    try:
        raw = proj_path.read_bytes()
    except OSError as exc:
        raise IntelligenceProjectionInvalid(
            f"cannot read projection file: {exc}"
        ) from exc
    try:
        projection = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise IntelligenceProjectionInvalid("projection file is not valid JSON") from exc
    if not isinstance(projection, dict):
        raise IntelligenceProjectionInvalid("projection is not a JSON object")
    if projection.get("schema") != PROJECTION_SCHEMA:
        raise IntelligenceProjectionInvalid(
            f"unsupported projection schema {projection.get('schema')!r}"
        )
    if projection.get("campaign_id") != campaign_id:
        raise IntelligenceCampaignMismatch(
            f"projection belongs to campaign {projection.get('campaign_id')!r}, "
            f"expected {campaign_id!r}"
        )
    expected = _compute_projection_source_digest(
        campaign_id,
        projection.get("artifact_count", -1),
        projection.get("artifacts", None),
    )
    if projection.get("source_digest") != expected:
        raise IntelligenceProjectionInvalid(
            "projection source_digest is stale or inconsistent"
        )
    return projection


def _rebuild_projection_locked(campaign_dir: Path, campaign_id: str) -> dict[str, Any]:
    """Reconstruct projection.json from every stored artifact (no skips)."""
    iroot = _iroot(campaign_dir)
    _ensure_dir(iroot)
    arts_dir = iroot / ARTIFACTS_DIR_NAME
    _ensure_dir(arts_dir)

    by_kind: dict[str, list[dict[str, Any]]] = {kind: [] for kind in ALLOWED_ARTIFACT_KINDS}
    seen_ids: set[str] = set()
    for path in sorted(arts_dir.glob("*.json")):
        envelope = _read_artifact_file(path, campaign_id)
        aid = envelope["artifact_id"]
        if aid in seen_ids:
            raise IntelligenceProjectionInvalid(
                f"duplicate logical artifact id {aid!r} in the artifact store"
            )
        seen_ids.add(aid)
        by_kind[envelope["kind"]].append({
            "artifact_id": aid,
            "artifact_digest": envelope["artifact_digest"],
            "payload_digest": envelope["payload_digest"],
            "created_at": envelope["created_at"],
            "storage_key": path.stem,
        })

    artifacts: dict[str, list[dict[str, Any]]] = {}
    for kind in ALLOWED_ARTIFACT_KINDS:
        descriptors = sorted(
            by_kind[kind], key=lambda d: (d["artifact_id"], d["artifact_digest"])
        )
        artifacts[kind] = descriptors
    artifact_count = sum(len(descriptors) for descriptors in artifacts.values())

    source_digest = _compute_projection_source_digest(campaign_id, artifact_count, artifacts)
    projection = {
        "schema": PROJECTION_SCHEMA,
        "campaign_id": campaign_id,
        "source_digest": source_digest,
        "artifact_count": artifact_count,
        "artifacts": artifacts,
        "rebuilt_at": utcnow(),
    }
    _write_projection_durable(campaign_dir, projection)
    return projection


# ---------------------------------------------------------------------------
# Structured-input safety policy
# ---------------------------------------------------------------------------

def _reject_constant(name: str) -> None:
    raise ValueError(f"non-standard JSON constant {name}")


def _check_key_name(key: str) -> None:
    if not isinstance(key, str):
        return
    norm = key.lower()
    compact = _compact_key(norm)
    if norm in _SENSITIVE_KEY_NAMES or compact in _SENSITIVE_COMPACT:
        raise IntelligencePayloadRejected(
            f"rejected field {key!r}: sensitive key name"
        )
    if any(compact.endswith(suffix) for suffix in _SENSITIVE_SUFFIX_COMPACT):
        raise IntelligencePayloadRejected(
            f"rejected field {key!r}: sensitive key name"
        )
    if norm in _RAW_CONTENT_KEYS or compact in _RAW_COMPACT:
        raise IntelligencePayloadRejected(
            f"rejected field {key!r}: raw repository content key"
        )


def _check_string_value(value: str, where: str) -> None:
    if "\x00" in value:
        raise IntelligencePayloadRejected(
            f"rejected value at {where}: NUL character"
        )
    if _PEM_RE.search(value):
        raise IntelligencePayloadRejected(
            f"rejected value at {where}: private key material"
        )
    if _TOKEN_RE.search(value) or _GH_PAT_RE.search(value) or _GITHUB_PAT_RE.search(value):
        raise IntelligencePayloadRejected(
            f"rejected value at {where}: API token"
        )
    if _BEARER_RE.search(value):
        raise IntelligencePayloadRejected(
            f"rejected value at {where}: bearer authorization"
        )
    if _ASSIGNMENT_RE.search(value):
        raise IntelligencePayloadRejected(
            f"rejected value at {where}: secret assignment"
        )


def reject_secret_or_raw(value: Any, where: str = "$") -> None:
    """Recursively reject secret material and raw repository content.

    Metadata keys (source_identity, source_path, repository_path, diff_digest,
    file_digest, credential_required) are permitted when their values do not
    themselves contain secrets or raw content.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            _check_key_name(key)
            child = f"{where}.{key}" if where else key
            reject_secret_or_raw(item, child)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            child = f"{where}[{index}]"
            reject_secret_or_raw(item, child)
    elif isinstance(value, str):
        _check_string_value(value, where)


def read_input_json(
    input_spec: str, repository_root: str, campaign_dir: Path
) -> dict[str, Any]:
    """Read and validate a JSON-object input from a file or stdin.

    Permits ``-`` for stdin and a regular non-symlink file outside the
    campaign repository root. Rejects symbolic links, non-regular files,
    repository-root paths, malformed JSON, non-object JSON, NUL bytes, and
    secret/raw-content material -- all before any artifact is created.
    """
    campaign_dir = Path(campaign_dir)
    if input_spec == "-":
        raw_bytes = sys.stdin.buffer.read()
    else:
        in_path = Path(input_spec)
        if in_path.is_symlink():
            raise IntelligencePayloadRejected("input path is a symbolic link")
        try:
            st = in_path.lstat()
        except OSError as exc:
            raise IntelligencePayloadRejected(
                f"cannot access input path: {exc}"
            ) from exc
        if not stat.S_ISREG(st.st_mode):
            raise IntelligencePayloadRejected("input path is not a regular file")
        try:
            resolved = in_path.resolve()
            resolved.relative_to(campaign_dir.resolve())
        except ValueError:
            pass
        else:
            raise IntelligencePayloadRejected(
                "input path resolves inside the campaign directory"
            )
        try:
            repo = Path(repository_root).resolve()
        except OSError as exc:
            raise IntelligencePayloadRejected(
                f"cannot resolve repository root: {exc}"
            ) from exc
        try:
            resolved = in_path.resolve()
            resolved.relative_to(repo)
        except ValueError:
            pass
        else:
            raise IntelligencePayloadRejected(
                "input path resolves inside the campaign repository root"
            )
        try:
            raw_bytes = in_path.read_bytes()
        except OSError as exc:
            raise IntelligencePayloadRejected(
                f"cannot read input path: {exc}"
            ) from exc

    if b"\x00" in raw_bytes:
        raise IntelligencePayloadRejected("input contains NUL characters")
    try:
        value = json.loads(raw_bytes.decode("utf-8"), parse_constant=_reject_constant)
    except ValueError as exc:
        raise IntelligencePayloadRejected(f"input is not valid JSON: {exc}") from exc
    except Exception as exc:
        raise IntelligencePayloadRejected("input is not valid JSON") from exc
    if not isinstance(value, dict):
        raise IntelligencePayloadRejected("input JSON must be a top-level object")
    reject_secret_or_raw(value)
    return value


# ---------------------------------------------------------------------------
# Public operations
# ---------------------------------------------------------------------------

def put_artifact(
    campaign_dir: Path,
    campaign_id: str,
    campaign_revision: int,
    kind: str,
    artifact_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Create or reuse one immutable artifact, then rebuild the projection."""
    campaign_dir = Path(campaign_dir)
    _validate_campaign_id(campaign_id)
    _validate_kind(kind)
    _validate_artifact_id(artifact_id)
    if not isinstance(payload, dict):
        raise IntelligencePayloadRejected("payload must be a JSON object")
    reject_secret_or_raw(payload)
    if not isinstance(campaign_revision, int) or campaign_revision < 0:
        raise IntelligenceArtifactInvalid(
            "campaign revision must be a non-negative integer"
        )

    payload_digest = compute_payload_digest(payload)
    artifact_digest = compute_artifact_digest(campaign_id, kind, artifact_id, payload)

    with intelligence_lock(campaign_dir):
        iroot = _iroot(campaign_dir)
        _ensure_dir(iroot)
        arts_dir = iroot / ARTIFACTS_DIR_NAME
        _ensure_dir(arts_dir)
        storage_key = artifact_storage_key(artifact_id)
        final_path = arts_dir / (storage_key + ".json")

        if final_path.is_symlink():
            raise IntelligenceStoreError("artifact path is a symbolic link")
        if final_path.exists():
            existing = _read_artifact_file(final_path, campaign_id)
            if existing["kind"] != kind or existing["payload"] != payload:
                raise IntelligenceArtifactConflict(
                    f"artifact {artifact_id!r} already exists for campaign "
                    f"{campaign_id!r} with a different kind or payload"
                )
            status = "reused"
        else:
            envelope = {
                "schema": ARTIFACT_SCHEMA,
                "campaign_id": campaign_id,
                "artifact_id": artifact_id,
                "kind": kind,
                "artifact_digest": artifact_digest,
                "payload_digest": payload_digest,
                "observed_campaign_revision": campaign_revision,
                "created_at": utcnow(),
                "payload": payload,
            }
            _write_artifact_durable(final_path, envelope)
            status = "created"

        projection = _rebuild_projection_locked(campaign_dir, campaign_id)

    return {
        "ok": True,
        "status": status,
        "campaign_id": campaign_id,
        "observed_campaign_revision": campaign_revision,
        "artifact_id": artifact_id,
        "kind": kind,
        "artifact_digest": artifact_digest,
        "payload_digest": payload_digest,
        "projection_source_digest": projection["source_digest"],
    }


def get_artifact(campaign_dir: Path, campaign_id: str, artifact_id: str) -> dict[str, Any]:
    """Resolve one artifact by ID, validating the envelope directly."""
    campaign_dir = Path(campaign_dir)
    _validate_campaign_id(campaign_id)
    _validate_artifact_id(artifact_id)
    storage_key = artifact_storage_key(artifact_id)
    path = _iroot(campaign_dir) / ARTIFACTS_DIR_NAME / (storage_key + ".json")
    if not path.exists():
        raise IntelligenceArtifactInvalid(
            f"artifact not found for campaign {campaign_id!r}: {artifact_id!r}"
        )
    envelope = _read_artifact_file(path, campaign_id)
    return {
        "ok": True,
        "status": "found",
        "campaign_id": campaign_id,
        "artifact": envelope,
    }


def list_artifacts(
    campaign_dir: Path, campaign_id: str, kind: str | None = None
) -> dict[str, Any]:
    """Return projection descriptors (never payloads) or a typed status."""
    campaign_dir = Path(campaign_dir)
    _validate_campaign_id(campaign_id)
    if kind is not None:
        _validate_kind(kind)

    proj_path = _iroot(campaign_dir) / PROJECTION_FILE_NAME
    if not proj_path.exists():
        return {
            "ok": True,
            "status": "projection_missing",
            "campaign_id": campaign_id,
        }
    if proj_path.is_symlink():
        raise IntelligenceProjectionInvalid("projection path is a symbolic link")
    try:
        projection = _read_projection_file(campaign_dir, campaign_id)
    except (IntelligenceProjectionInvalid, IntelligenceCampaignMismatch) as exc:
        return {
            "ok": True,
            "status": "projection_stale",
            "campaign_id": campaign_id,
            "reason": str(exc),
        }

    artifacts = projection["artifacts"]
    if kind is not None:
        artifacts = {kind: artifacts.get(kind, [])}
    return {
        "ok": True,
        "status": "healthy",
        "campaign_id": campaign_id,
        "artifact_count": projection["artifact_count"],
        "artifacts": artifacts,
        "source_digest": projection["source_digest"],
        "rebuilt_at": projection["rebuilt_at"],
    }


def rebuild_projection(campaign_dir: Path, campaign_id: str) -> dict[str, Any]:
    """Rebuild projection.json from every stored artifact under the lock."""
    campaign_dir = Path(campaign_dir)
    _validate_campaign_id(campaign_id)
    with intelligence_lock(campaign_dir):
        projection = _rebuild_projection_locked(campaign_dir, campaign_id)
    return {
        "ok": True,
        "status": "rebuilt",
        "campaign_id": campaign_id,
        "artifact_count": projection["artifact_count"],
        "source_digest": projection["source_digest"],
    }


def doctor(campaign_dir: Path, campaign_id: str) -> dict[str, Any]:
    """Read-only health report; performs no repair and writes nothing."""
    campaign_dir = Path(campaign_dir)
    _validate_campaign_id(campaign_id)
    try:
        import fcntl  # type: ignore  # noqa: F401
    except ImportError:
        return {
            "ok": False,
            "status": "backend_unavailable",
            "campaign_id": campaign_id,
            "reason": "fcntl is unavailable",
        }

    details: list[str] = []
    mode_issues: list[str] = []
    iroot = _iroot(campaign_dir)
    if not iroot.exists():
        return {
            "ok": False,
            "status": "projection_missing",
            "campaign_id": campaign_id,
            "artifact_count": 0,
            "details": ["intelligence sidecar does not exist yet"],
        }
    if iroot.is_symlink():
        return {
            "ok": False,
            "status": "artifact_corrupt",
            "campaign_id": campaign_id,
            "details": ["intelligence root is a symbolic link"],
        }

    arts_dir = iroot / ARTIFACTS_DIR_NAME
    lock_path = iroot / LOCK_FILE_NAME
    proj_path = iroot / PROJECTION_FILE_NAME
    for label, path, expected_mode, is_dir in (
        ("intelligence root", iroot, _DIR_MODE, True),
        ("artifacts directory", arts_dir, _DIR_MODE, True),
        ("lock file", lock_path, _FILE_MODE, False),
        ("projection file", proj_path, _FILE_MODE, False),
    ):
        if not path.exists():
            continue
        if path.is_symlink():
            return {
                "ok": False,
                "status": "artifact_corrupt",
                "campaign_id": campaign_id,
                "details": [f"{label} is a symbolic link"],
            }
        try:
            st = path.lstat()
        except OSError as exc:
            return {
                "ok": False,
                "status": "artifact_corrupt",
                "campaign_id": campaign_id,
                "details": [f"cannot stat {label}: {exc}"],
            }
        if is_dir:
            if not stat.S_ISDIR(st.st_mode):
                return {
                    "ok": False,
                    "status": "artifact_corrupt",
                    "campaign_id": campaign_id,
                    "details": [f"{label} is not a directory"],
                }
        elif not stat.S_ISREG(st.st_mode):
            return {
                "ok": False,
                "status": "artifact_corrupt",
                "campaign_id": campaign_id,
                "details": [f"{label} is not a regular file"],
            }
        if (st.st_mode & 0o777) != expected_mode:
            mode_issues.append(f"{label} mode is {oct(st.st_mode & 0o777)}")

    # Private-mode violations make doctor non-healthy: the required modes are
    # intelligence/ 0700, intelligence/artifacts/ 0700, intelligence/lock 0600,
    # projection.json 0600, and every artifact file 0600. Details only carry
    # non-sensitive mode information.
    if mode_issues:
        return {
            "ok": False,
            "status": "artifact_corrupt",
            "campaign_id": campaign_id,
            "details": mode_issues,
        }

    # Validate every stored artifact; corrupt artifacts are reported, never
    # omitted. A stored artifact with a non-0600 mode is itself a health failure.
    artifacts_dir_exists = arts_dir.exists()
    stored = []
    if artifacts_dir_exists:
        for path in sorted(arts_dir.glob("*.json")):
            try:
                st = path.lstat()
            except OSError as exc:
                return {
                    "ok": False,
                    "status": "artifact_corrupt",
                    "campaign_id": campaign_id,
                    "details": [f"cannot stat artifact {path.name}: {exc}"],
                }
            if not stat.S_ISREG(st.st_mode):
                return {
                    "ok": False,
                    "status": "artifact_corrupt",
                    "campaign_id": campaign_id,
                    "details": [f"artifact {path.name} is not a regular file"],
                }
            if (st.st_mode & 0o777) != _FILE_MODE:
                return {
                    "ok": False,
                    "status": "artifact_corrupt",
                    "campaign_id": campaign_id,
                    "details": [f"artifact {path.name} mode is {oct(st.st_mode & 0o777)}"],
                }
            try:
                envelope = _read_artifact_file(path, campaign_id)
            except IntelligenceStoreError as exc:
                return {
                    "ok": False,
                    "status": "artifact_corrupt",
                    "campaign_id": campaign_id,
                    "details": [f"artifact {path.name} is corrupt: {exc}"],
                }
            stored.append(envelope)

    if not proj_path.exists():
        return {
            "ok": False,
            "status": "projection_missing",
            "campaign_id": campaign_id,
            "artifact_count": len(stored),
            "details": details,
        }

    try:
        projection = _read_projection_file(campaign_dir, campaign_id)
    except (IntelligenceProjectionInvalid, IntelligenceCampaignMismatch) as exc:
        return {
            "ok": False,
            "status": "projection_stale",
            "campaign_id": campaign_id,
            "artifact_count": len(stored),
            "details": details + [f"projection invalid: {exc}"],
        }

    fresh_descriptors = []
    for envelope in stored:
        fresh_descriptors.append((envelope["artifact_id"], envelope["artifact_digest"]))
    projected_pairs = set()
    for kind in ALLOWED_ARTIFACT_KINDS:
        for descriptor in projection["artifacts"].get(kind, []):
            projected_pairs.add((descriptor["artifact_id"], descriptor["artifact_digest"]))
    if projected_pairs != set(fresh_descriptors) or len(stored) != projection.get("artifact_count", -1):
        return {
            "ok": False,
            "status": "projection_stale",
            "campaign_id": campaign_id,
            "artifact_count": len(stored),
            "details": details + ["projection descriptors do not match the artifact store"],
        }

    return {
        "ok": True,
        "status": "healthy",
        "campaign_id": campaign_id,
        "artifact_count": len(stored),
        "source_digest": projection["source_digest"],
        "details": details,
    }
