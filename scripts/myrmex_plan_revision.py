#!/usr/bin/env python3
"""Governed, idempotent, crash-safe plan activation for P1-012."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
from contextlib import contextmanager
from typing import Any, Iterator

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import myrmex_campaign_intelligence as intel  # noqa: E402
import myrmex_dag_validate as dag_validate  # noqa: E402
import myrmex_plan_store as plan_store  # noqa: E402
import myrmex_work_unit_compiler as compiler  # noqa: E402

RECEIPT_SCHEMA = "myrmex.plan-activation/v1"
INTENT_SCHEMA = "myrmex.plan-activation-intent/v1"
PRECONDITION_SCHEMA = "myrmex.plan-activation-precondition/v1"
PROJECTION_SCHEMA = "myrmex.active-plan-projection/v1"
AUTHORITY_SCHEMA = "myrmex.activation-authority/v1"
DECISION_SCHEMA = "myrmex.human-decision/v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PLAN_RE = re.compile(r"^plan_[0-9a-f]{64}$")
RECORD_RE = re.compile(r"^planrec_[0-9a-f]{64}$")
REQUEST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,255}$")
AUTHORIZED_ROLES = {"primary_orchestrator", "human_operator"}


class PlanActivationError(Exception): pass
class PlanActivationInputInvalid(PlanActivationError): pass
class PlanActivationUnauthorized(PlanActivationError): pass
class PlanActivationStale(PlanActivationError): pass
class PlanActivationConflict(PlanActivationError): pass
class PlanActivationBackendUnavailable(PlanActivationError): pass


def _canon(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canon(value)).hexdigest()


def _time(value: Any, label: str) -> dt.datetime:
    if not isinstance(value, str) or "T" not in value or value.endswith("Z") is False and not re.search(r"[+-][0-9]{2}:[0-9]{2}$", value):
        raise PlanActivationInputInvalid(f"{label} must be strict timezone-aware RFC3339")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise PlanActivationInputInvalid(f"{label} must be strict timezone-aware RFC3339") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PlanActivationInputInvalid(f"{label} must include a timezone")
    return parsed


def _validate_authority(authority: Any, plan_revision_id: str, activated_at: str) -> str:
    fields = {"schema", "authority_id", "authority_digest", "role", "subject", "plan_revision_id", "scope", "granted_at", "expires_at"}
    if not isinstance(authority, dict) or set(authority) != fields or authority.get("schema") != AUTHORITY_SCHEMA:
        raise PlanActivationUnauthorized("activation authority fields/schema invalid")
    body = {key: value for key, value in authority.items() if key not in {"authority_id", "authority_digest"}}
    digest = _sha(body)
    if authority.get("authority_digest") != digest or authority.get("authority_id") != "auth_" + digest:
        raise PlanActivationUnauthorized("activation authority digest identity invalid")
    if authority.get("role") not in AUTHORIZED_ROLES:
        raise PlanActivationUnauthorized("planner, critic, and other non-governing roles cannot activate plans")
    if not isinstance(authority.get("subject"), str) or not authority["subject"] or authority.get("scope") != "plan_activation_only":
        raise PlanActivationUnauthorized("activation authority subject/scope invalid")
    if authority.get("plan_revision_id") != plan_revision_id:
        raise PlanActivationUnauthorized("activation authority is bound to a different plan revision")
    granted = _time(authority.get("granted_at"), "authority.granted_at")
    expires = _time(authority.get("expires_at"), "authority.expires_at")
    moment = _time(activated_at, "activated_at")
    if granted > moment or expires < moment or expires <= granted:
        raise PlanActivationUnauthorized("activation authority is not valid at activation time")
    return digest


def _validate_decisions(decisions: Any, reviewed: dict[str, Any], activated_at: str) -> str:
    if not isinstance(decisions, list):
        raise PlanActivationInputInvalid("human decisions must be an array")
    required = {}
    known = {}
    for wu in reviewed["work_units"]:
        for gate in wu["human_gates"]:
            gate_id = gate["gate_id"]
            if gate_id in known:
                raise PlanActivationInputInvalid("human gate IDs must be globally unique within a plan")
            known[gate_id] = gate
            if gate["required_before"] == "plan_activation":
                required[gate_id] = gate
    accepted = {}
    fields = {"schema", "decision_id", "decision_digest", "gate_id", "plan_revision_id", "outcome", "decided_by", "decided_at", "expires_at"}
    moment = _time(activated_at, "activated_at")
    for decision in decisions:
        if not isinstance(decision, dict) or set(decision) != fields or decision.get("schema") != DECISION_SCHEMA:
            raise PlanActivationInputInvalid("human decision fields/schema invalid")
        body = {key: value for key, value in decision.items() if key not in {"decision_id", "decision_digest"}}
        digest = _sha(body)
        if decision.get("decision_digest") != digest or decision.get("decision_id") != "hdec_" + digest:
            raise PlanActivationInputInvalid("human decision digest identity invalid")
        gate_id = decision.get("gate_id")
        if gate_id not in known or gate_id in accepted or decision.get("plan_revision_id") != reviewed["plan_revision_id"]:
            raise PlanActivationInputInvalid("human decision is duplicate or bound to an unknown gate/plan")
        if decision.get("outcome") != "APPROVED" or not isinstance(decision.get("decided_by"), str) or not decision["decided_by"]:
            raise PlanActivationUnauthorized("required human decision is not an explicit approval")
        decided = _time(decision.get("decided_at"), "decision.decided_at")
        expires = _time(decision.get("expires_at"), "decision.expires_at")
        if decided > moment or expires < moment or expires <= decided:
            raise PlanActivationUnauthorized("human approval is expired or not yet valid")
        accepted[gate_id] = decision
    missing = sorted(set(required) - set(accepted))
    if missing:
        raise PlanActivationUnauthorized("required plan-activation human decisions are missing: " + ", ".join(missing))
    return _sha([[decision["gate_id"], decision["decision_digest"]] for decision in sorted(decisions, key=lambda item: item["gate_id"])])


@contextmanager
def _activation_lock(campaign_dir: pathlib.Path) -> Iterator[None]:
    try:
        import fcntl
    except ImportError as error:
        raise PlanActivationBackendUnavailable("fcntl unavailable; activation fails closed") from error
    intelligence = pathlib.Path(campaign_dir) / "intelligence"
    if intelligence.is_symlink() or not intelligence.is_dir():
        raise PlanActivationBackendUnavailable("campaign intelligence directory unavailable or unsafe")
    path = intelligence / "plan-activation.lock"
    if path.is_symlink():
        raise PlanActivationBackendUnavailable("plan activation lock must not be a symlink")
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as error:
        raise PlanActivationBackendUnavailable("cannot open plan activation lock") from error
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise PlanActivationBackendUnavailable("plan activation lock must be regular")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def _campaign_snapshot(campaign_dir: pathlib.Path, campaign_id: str, expected_revision: int) -> tuple[dict[str, Any], str]:
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 1:
        raise PlanActivationInputInvalid("expected campaign revision must be a positive integer")
    path = pathlib.Path(campaign_dir) / "campaign.json"
    try:
        if path.is_symlink() or not path.is_file():
            raise PlanActivationBackendUnavailable("campaign state path unavailable or unsafe")
        campaign = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise PlanActivationBackendUnavailable("campaign state is unreadable") from error
    if not isinstance(campaign, dict) or campaign.get("id") != campaign_id or campaign.get("revision") != expected_revision or campaign.get("status") != "active":
        raise PlanActivationStale("campaign identity, status, or revision is stale")
    if not isinstance(campaign.get("repository_root"), str) or not isinstance(campaign.get("work_units"), list) or not isinstance(campaign.get("dag"), dict):
        raise PlanActivationInputInvalid("campaign state required fields invalid")
    return campaign, _sha(campaign)


def _load_payload(campaign_dir, campaign_id, artifact_id, kind=None) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        artifact = intel.get_artifact(pathlib.Path(campaign_dir), campaign_id, artifact_id)["artifact"]
    except Exception as error:
        raise PlanActivationInputInvalid(f"required artifact unavailable or invalid: {artifact_id}") from error
    if artifact.get("artifact_id") != artifact_id or (kind is not None and artifact.get("kind") != kind) or not isinstance(artifact.get("payload"), dict):
        raise PlanActivationInputInvalid(f"required artifact identity/kind invalid: {artifact_id}")
    return artifact["payload"], artifact


def _put_exact(campaign_dir, campaign_id, revision, kind, artifact_id, payload):
    try:
        existing = intel.get_artifact(pathlib.Path(campaign_dir), campaign_id, artifact_id)["artifact"]
    except intel.IntelligenceArtifactInvalid as error:
        if not str(error).startswith("artifact not found"):
            raise PlanActivationBackendUnavailable(f"activation artifact is corrupt: {artifact_id}") from error
    except Exception as error:
        raise PlanActivationBackendUnavailable(f"activation artifact lookup failed: {artifact_id}") from error
    else:
        if existing.get("kind") != kind or _canon(existing.get("payload")) != _canon(payload):
            raise PlanActivationConflict(f"immutable activation artifact conflict: {artifact_id}")
        return existing["payload"]
    try:
        intel.put_artifact(pathlib.Path(campaign_dir), campaign_id, revision, kind, artifact_id, payload)
        stored, _ = _load_payload(campaign_dir, campaign_id, artifact_id, kind)
    except PlanActivationError:
        raise
    except intel.IntelligenceArtifactConflict as error:
        raise PlanActivationConflict(f"immutable activation artifact conflict: {artifact_id}") from error
    except Exception as error:
        raise PlanActivationBackendUnavailable(f"activation artifact persistence failed: {artifact_id}") from error
    if _canon(stored) != _canon(payload):
        raise PlanActivationConflict(f"activation artifact replay mismatch: {artifact_id}")
    return stored


def _optional_payload(campaign_dir, campaign_id, artifact_id, kind):
    try:
        artifact = intel.get_artifact(pathlib.Path(campaign_dir), campaign_id, artifact_id)["artifact"]
    except intel.IntelligenceArtifactInvalid as error:
        if str(error).startswith("artifact not found"):
            return None
        raise PlanActivationBackendUnavailable(f"activation artifact is corrupt: {artifact_id}") from error
    except Exception as error:
        raise PlanActivationBackendUnavailable(f"activation artifact lookup failed: {artifact_id}") from error
    if artifact.get("kind") != kind or not isinstance(artifact.get("payload"), dict):
        raise PlanActivationConflict(f"activation artifact kind/payload invalid: {artifact_id}")
    return artifact["payload"]


def _git_head(repository_root: str) -> str:
    root = pathlib.Path(repository_root)
    if root.is_symlink() or not root.is_dir():
        raise PlanActivationStale("repository root unavailable or unsafe")
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_OPTIONAL_LOCKS="0")
    proc = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-C", str(root), "rev-parse", "--verify", "HEAD"],
        capture_output=True, text=True, env=env, timeout=15,
    )
    head = proc.stdout.strip()
    if proc.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", head):
        raise PlanActivationStale("repository HEAD cannot be verified")
    return head


def _verify_current_inputs(campaign_dir, campaign_id, campaign, reviewed) -> str:
    bound = []
    normalized_identity = None
    for entry in reviewed["input_digests"]:
        _, artifact = _load_payload(campaign_dir, campaign_id, entry["identity"])
        if artifact["payload_digest"] != entry["sha256"]:
            raise PlanActivationStale(f"planning input digest is stale: {entry['identity']}")
        bound.append([entry["kind"], entry["identity"], entry["sha256"]])
        if entry["kind"] == "normalized-backlog":
            normalized_identity = entry["identity"]
        if entry["kind"] == "repository-context":
            context = artifact["payload"]
            if context.get("base_sha") != reviewed["base_sha"] or context.get("repository_root") != campaign["repository_root"]:
                raise PlanActivationStale("repository context no longer matches plan/campaign")
    if normalized_identity is None:
        raise PlanActivationInputInvalid("reviewed plan lacks normalized backlog input")
    listing = intel.list_artifacts(pathlib.Path(campaign_dir), campaign_id, kind="backlog")
    if listing.get("status") != "healthy":
        raise PlanActivationBackendUnavailable("backlog projection unavailable")
    descriptors = listing.get("artifacts", {}).get("backlog", [])
    snapshots = [item for item in descriptors if isinstance(item, dict) and item.get("artifact_id", "").startswith("normalized-backlog/snapshot/")]
    selected = next((item for item in snapshots if item["artifact_id"] == normalized_identity), None)
    if selected is None:
        raise PlanActivationStale("bound normalized backlog snapshot is not projected")
    selected_at = _time(selected.get("created_at"), "backlog snapshot created_at")
    if any(item["artifact_id"] != normalized_identity and _time(item.get("created_at"), "backlog snapshot created_at") > selected_at for item in snapshots):
        raise PlanActivationStale("a newer normalized backlog snapshot supersedes the reviewed plan input")
    head = _git_head(campaign["repository_root"])
    if head != reviewed["base_sha"]:
        raise PlanActivationStale("repository HEAD differs from reviewed plan base SHA")
    return _sha(sorted(bound))


def _work_order_set_digest(campaign: dict[str, Any], plan_revision_id: str | None = None) -> str:
    rows = []
    for wu in campaign["work_units"]:
        order = wu.get("work_order") if isinstance(wu, dict) else None
        if plan_revision_id is not None and (not isinstance(order, dict) or order.get("plan_provenance", {}).get("plan_revision_id") != plan_revision_id):
            continue
        try:
            compiler.validate_work_order(order)
        except Exception as error:
            raise PlanActivationInputInvalid("campaign contains a non-compiled or invalid work order") from error
        rows.append([wu["id"], order["work_order_id"], order["work_order_digest"]])
    if not rows:
        raise PlanActivationInputInvalid("activation requires compiled work orders")
    return _sha(sorted(rows))


def _active_heads(campaign_dir, campaign_id) -> list[dict[str, Any]]:
    listing = plan_store.list_plan_records(campaign_dir, campaign_id)
    return [item for item in listing["records"] if item["is_head"] and item["lifecycle_status"] == "active"]


def _intent(
    request_id, campaign_id, expected_revision, reviewed_record_id, review_digest,
    plan_revision_id, dag_receipt, authority, decisions, activated_at,
):
    if not isinstance(request_id, str) or not REQUEST_RE.fullmatch(request_id):
        raise PlanActivationInputInvalid("request_id format invalid")
    if not RECORD_RE.fullmatch(reviewed_record_id or "") or not SHA256_RE.fullmatch(review_digest or "") or not PLAN_RE.fullmatch(plan_revision_id or ""):
        raise PlanActivationInputInvalid("expected plan/review identities invalid")
    body = {
        "schema": INTENT_SCHEMA, "request_id": request_id, "campaign_id": campaign_id,
        "expected_campaign_revision": expected_revision, "plan_revision_id": plan_revision_id,
        "expected_reviewed_record_id": reviewed_record_id, "expected_review_digest": review_digest,
        "expected_dag_validation_id": dag_receipt.get("validation_id") if isinstance(dag_receipt, dict) else None,
        "expected_dag_validation_digest": dag_receipt.get("validation_digest") if isinstance(dag_receipt, dict) else None,
        "expected_graph_digest": dag_receipt.get("graph_digest") if isinstance(dag_receipt, dict) else None,
        "activation_authority": authority, "human_decisions": decisions, "activated_at": activated_at,
        "authority": {"scope": "activation_intent_only", "repository_write": False, "commit": False, "push": False},
    }
    digest = _sha(body)
    return {**body, "activation_id": "activation_" + digest, "intent_digest": digest}


def _activation_store_authority(intent, precondition, validated_record):
    body = {
        "schema": "myrmex.plan-activation-authority/v1", "status": "PRECONDITIONS_PASSED",
        "activation_id": intent["activation_id"], "precondition_digest": precondition["precondition_digest"],
        "campaign_id": intent["campaign_id"], "plan_revision_id": intent["plan_revision_id"],
        "validated_record_id": validated_record["record_id"], "activate_plan": True,
        "repository_write": False, "commit": False, "push": False,
    }
    return {**body, "authority_digest": _sha(body)}


def validate_activation_receipt(receipt: Any) -> None:
    fields = {
        "schema", "status", "activation_id", "request_id", "campaign_id", "campaign_revision",
        "plan_revision_id", "reviewed_record_id", "validated_record_id", "active_record_id",
        "plan_digest", "review_digest", "dag_validation_id", "dag_validation_digest", "graph_digest",
        "work_order_set_digest", "input_set_digest", "human_decisions_digest", "activation_authority_digest",
        "intent_artifact_id", "intent_digest", "precondition_artifact_id", "precondition_digest",
        "active_projection_artifact_id", "active_projection_digest", "repository_base_sha", "activated_at",
        "authority", "receipt_digest",
    }
    if not isinstance(receipt, dict) or set(receipt) != fields or receipt.get("schema") != RECEIPT_SCHEMA or receipt.get("status") != "ACTIVE":
        raise PlanActivationInputInvalid("activation receipt fields/schema/status invalid")
    digest = _sha({key: value for key, value in receipt.items() if key != "receipt_digest"})
    if receipt.get("receipt_digest") != digest:
        raise PlanActivationInputInvalid("activation receipt digest invalid")
    if not re.fullmatch(r"activation_[0-9a-f]{64}", receipt.get("activation_id", "")) or not REQUEST_RE.fullmatch(receipt.get("request_id", "")):
        raise PlanActivationInputInvalid("activation receipt activation/request identity invalid")
    if not isinstance(receipt.get("campaign_id"), str) or isinstance(receipt.get("campaign_revision"), bool) or not isinstance(receipt.get("campaign_revision"), int) or receipt["campaign_revision"] < 1:
        raise PlanActivationInputInvalid("activation receipt campaign identity invalid")
    for field in ("plan_revision_id",):
        if not PLAN_RE.fullmatch(receipt.get(field, "")):
            raise PlanActivationInputInvalid(f"activation receipt {field} invalid")
    for field in ("reviewed_record_id", "validated_record_id", "active_record_id"):
        if not RECORD_RE.fullmatch(receipt.get(field, "")):
            raise PlanActivationInputInvalid(f"activation receipt {field} invalid")
    for field in ("plan_digest", "review_digest", "dag_validation_digest", "graph_digest", "work_order_set_digest", "input_set_digest", "human_decisions_digest", "activation_authority_digest", "intent_digest", "precondition_digest", "active_projection_digest", "receipt_digest"):
        if not SHA256_RE.fullmatch(receipt.get(field, "")):
            raise PlanActivationInputInvalid(f"activation receipt {field} invalid")
    if not re.fullmatch(r"dagval_[0-9a-f]{64}", receipt.get("dag_validation_id", "")) or not re.fullmatch(r"[0-9a-f]{40}", receipt.get("repository_base_sha", "")):
        raise PlanActivationInputInvalid("activation receipt DAG/repository identity invalid")
    if receipt.get("intent_artifact_id") != "plan-activation/intent/" + hashlib.sha256(receipt["request_id"].encode("utf-8")).hexdigest():
        raise PlanActivationInputInvalid("activation receipt intent artifact identity invalid")
    if receipt.get("precondition_artifact_id") != "plan-activation/precondition/" + receipt["activation_id"] or receipt.get("active_projection_artifact_id") != "plan-activation/projection/" + receipt["activation_id"]:
        raise PlanActivationInputInvalid("activation receipt precondition/projection artifact identity invalid")
    if len({receipt["reviewed_record_id"], receipt["validated_record_id"], receipt["active_record_id"]}) != 3:
        raise PlanActivationInputInvalid("activation receipt lifecycle identities must be distinct")
    if receipt.get("authority") != {"scope": "plan_activation_only", "repository_write": False, "commit": False, "push": False, "merge": False, "release": False, "deploy": False}:
        raise PlanActivationInputInvalid("activation receipt authority invalid")
    _time(receipt.get("activated_at"), "receipt.activated_at")


def _validate_confirmed_chain(campaign_dir, campaign_id, receipt, intent) -> None:
    precondition, _ = _load_payload(campaign_dir, campaign_id, receipt["precondition_artifact_id"], "decision")
    precondition_digest = _sha({key: value for key, value in precondition.items() if key != "precondition_digest"})
    if (
        precondition.get("schema") != PRECONDITION_SCHEMA
        or precondition.get("status") != "PASS"
        or precondition.get("activation_id") != receipt["activation_id"]
        or precondition.get("intent_digest") != intent["intent_digest"]
        or precondition.get("precondition_digest") != precondition_digest
        or precondition_digest != receipt["precondition_digest"]
    ):
        raise PlanActivationConflict("confirmed activation precondition chain is corrupt")
    projection, _ = _load_payload(campaign_dir, campaign_id, receipt["active_projection_artifact_id"], "decision")
    projection_digest = _sha({key: value for key, value in projection.items() if key != "projection_digest"})
    if (
        projection.get("schema") != PROJECTION_SCHEMA
        or projection.get("activation_id") != receipt["activation_id"]
        or projection.get("active_record_id") != receipt["active_record_id"]
        or projection.get("plan_revision_id") != receipt["plan_revision_id"]
        or projection.get("projection_digest") != projection_digest
        or projection_digest != receipt["active_projection_digest"]
    ):
        raise PlanActivationConflict("confirmed active-plan projection chain is corrupt")
    try:
        active_record = plan_store.get_plan_record(campaign_dir, campaign_id, receipt["active_record_id"])
    except Exception as error:
        raise PlanActivationConflict("confirmed active lifecycle record is unavailable") from error
    if active_record["lifecycle_status"] != "active" or active_record["plan_revision_id"] != receipt["plan_revision_id"] or active_record["previous_record_id"] != receipt["validated_record_id"]:
        raise PlanActivationConflict("confirmed active lifecycle chain is corrupt")


def activate_plan(
    campaign_dir: pathlib.Path,
    campaign_id: str,
    expected_campaign_revision: int,
    request_id: str,
    plan_revision_id: str,
    expected_reviewed_record_id: str,
    expected_review_digest: str,
    dag_receipt: dict[str, Any],
    activation_authority: dict[str, Any],
    human_decisions: list[dict[str, Any]],
    activated_at: str,
) -> dict[str, Any]:
    """Activate one exact plan once; every write is replayable and digest-bound."""
    campaign_dir = pathlib.Path(campaign_dir)
    with _activation_lock(campaign_dir):
        intent = _intent(
            request_id, campaign_id, expected_campaign_revision, expected_reviewed_record_id,
            expected_review_digest, plan_revision_id, dag_receipt, activation_authority,
            human_decisions, activated_at,
        )
        intent_artifact_id = "plan-activation/intent/" + hashlib.sha256(request_id.encode("utf-8")).hexdigest()
        _put_exact(campaign_dir, campaign_id, expected_campaign_revision, "decision", intent_artifact_id, intent)

        receipt_artifact_id = "plan-activation/receipt/" + intent["activation_id"]
        confirmed = _optional_payload(campaign_dir, campaign_id, receipt_artifact_id, "decision")
        if confirmed is not None:
            validate_activation_receipt(confirmed)
            if (
                confirmed["activation_id"] != intent["activation_id"]
                or confirmed["request_id"] != request_id
                or confirmed["campaign_id"] != campaign_id
                or confirmed["campaign_revision"] != expected_campaign_revision
                or confirmed["plan_revision_id"] != plan_revision_id
                or confirmed["reviewed_record_id"] != expected_reviewed_record_id
                or confirmed["review_digest"] != expected_review_digest
                or confirmed["dag_validation_id"] != intent["expected_dag_validation_id"]
                or confirmed["dag_validation_digest"] != intent["expected_dag_validation_digest"]
                or confirmed["graph_digest"] != intent["expected_graph_digest"]
                or confirmed["intent_digest"] != intent["intent_digest"]
            ):
                raise PlanActivationConflict("confirmed activation receipt does not match exact replay intent")
            _validate_confirmed_chain(campaign_dir, campaign_id, confirmed, intent)
            return confirmed

        try:
            reviewed = plan_store.get_plan_record(campaign_dir, campaign_id, expected_reviewed_record_id)
        except Exception as error:
            raise PlanActivationInputInvalid("expected reviewed plan record unavailable or invalid") from error
        if reviewed["plan_revision_id"] != plan_revision_id or reviewed["lifecycle_status"] != "reviewed":
            raise PlanActivationStale("expected reviewed plan identity/status is stale")

        precondition_artifact_id = "plan-activation/precondition/" + intent["activation_id"]
        precondition = _optional_payload(campaign_dir, campaign_id, precondition_artifact_id, "decision")
        campaign = None
        campaign_digest = None
        if precondition is not None:
            if precondition.get("schema") != PRECONDITION_SCHEMA or precondition.get("activation_id") != intent["activation_id"]:
                raise PlanActivationConflict("persisted activation precondition identity invalid")
            precondition_digest = _sha({key: value for key, value in precondition.items() if key != "precondition_digest"})
            if precondition.get("precondition_digest") != precondition_digest:
                raise PlanActivationConflict("persisted activation precondition digest invalid")
            if precondition.get("intent_digest") != intent["intent_digest"] or precondition.get("status") != "PASS":
                raise PlanActivationConflict("persisted activation precondition is not bound to the exact intent")
        else:
            campaign, campaign_digest = _campaign_snapshot(campaign_dir, campaign_id, expected_campaign_revision)
            dag_validate.validate_result(dag_receipt)
            if dag_receipt["status"] != "PASS" or dag_receipt["campaign_id"] != campaign_id or dag_receipt["campaign_revision"] != expected_campaign_revision or dag_receipt["plan_revision_id"] != plan_revision_id or dag_receipt["reviewed_plan_record_id"] != expected_reviewed_record_id or dag_receipt["review_digest"] != expected_review_digest:
                raise PlanActivationInputInvalid("supplied DAG PASS is not bound to exact activation inputs")
            recomputed_dag = dag_validate.validate_semantic_dag(campaign_dir, campaign, expected_campaign_revision, plan_revision_id)
            if recomputed_dag != dag_receipt:
                raise PlanActivationStale("DAG validation receipt does not match current campaign/plan/work orders")
            critic_receipt = compiler._review_receipt(campaign_dir, campaign_id, reviewed)
            if critic_receipt["review_digest"] != expected_review_digest or critic_receipt.get("verdict") != "PASS":
                raise PlanActivationInputInvalid("exact critic PASS is missing or stale")
            authority_digest = _validate_authority(activation_authority, plan_revision_id, activated_at)
            decisions_digest = _validate_decisions(human_decisions, reviewed, activated_at)
            input_set_digest = _verify_current_inputs(campaign_dir, campaign_id, campaign, reviewed)
            work_order_set_digest = _work_order_set_digest(campaign, plan_revision_id)
            active_heads = _active_heads(campaign_dir, campaign_id)
            if active_heads:
                raise PlanActivationConflict("another plan is already active")
            report_body = {
                "schema": PRECONDITION_SCHEMA, "status": "PASS", "activation_id": intent["activation_id"],
                "intent_digest": intent["intent_digest"], "campaign_id": campaign_id,
                "campaign_revision": expected_campaign_revision, "campaign_digest": campaign_digest,
                "plan_revision_id": plan_revision_id, "reviewed_record_id": expected_reviewed_record_id,
                "plan_digest": reviewed["plan_digest"], "review_digest": expected_review_digest,
                "dag_validation_id": dag_receipt["validation_id"], "dag_validation_digest": dag_receipt["validation_digest"],
                "graph_digest": dag_receipt["graph_digest"], "work_order_set_digest": work_order_set_digest,
                "input_set_digest": input_set_digest, "human_decisions_digest": decisions_digest,
                "activation_authority_digest": authority_digest,
                "checks": {"campaign_revision": "PASS", "critic": "PASS", "dag": "PASS", "work_orders": "PASS", "inputs": "PASS", "repository_head": "PASS", "human_decisions": "PASS", "activation_authority": "PASS", "single_active_plan": "PASS"},
                "authority": {"scope": "precondition_report_only", "activate_plan": False, "repository_write": False, "commit": False, "push": False},
            }
            precondition = {**report_body, "precondition_digest": _sha(report_body)}
            _put_exact(campaign_dir, campaign_id, expected_campaign_revision, "decision", precondition_artifact_id, precondition)

        validated_record = plan_store.build_lifecycle_record(reviewed, "validated", activated_at)
        store_authority = _activation_store_authority(intent, precondition, validated_record)
        active_record = plan_store.build_activated_plan_record(validated_record, activated_at, store_authority)
        try:
            current_head = plan_store.get_plan_head(campaign_dir, campaign_id, plan_revision_id)
        except Exception as error:
            raise PlanActivationConflict("plan lifecycle head unavailable during activation") from error
        active_already_durable = current_head["lifecycle_status"] == "active" and current_head["record_id"] == active_record["record_id"]
        if current_head["lifecycle_status"] == "active" and not active_already_durable:
            raise PlanActivationConflict("a different active lifecycle record already exists")
        if current_head["lifecycle_status"] not in {"reviewed", "validated", "active"}:
            raise PlanActivationConflict("plan lifecycle left the governed activation path")
        if current_head["lifecycle_status"] == "validated" and current_head["record_id"] != validated_record["record_id"]:
            raise PlanActivationConflict("a different validated lifecycle record already exists")

        if not active_already_durable:
            # Until the active record exists, every exact campaign, input,
            # authority, decision, and work-order precondition remains live.
            current_campaign, current_digest = _campaign_snapshot(campaign_dir, campaign_id, expected_campaign_revision)
            if current_digest != precondition["campaign_digest"] or (campaign_digest is not None and current_digest != campaign_digest):
                raise PlanActivationStale("campaign changed after activation intent/preconditions")
            if _verify_current_inputs(campaign_dir, campaign_id, current_campaign, reviewed) != precondition["input_set_digest"]:
                raise PlanActivationStale("planning inputs changed after activation preconditions")
            if _work_order_set_digest(current_campaign, plan_revision_id) != precondition["work_order_set_digest"]:
                raise PlanActivationStale("compiled work orders changed after activation preconditions")
            _validate_authority(activation_authority, plan_revision_id, activated_at)
            _validate_decisions(human_decisions, reviewed, activated_at)
            if current_head["lifecycle_status"] == "reviewed":
                try:
                    plan_store.store_plan_record(campaign_dir, campaign_id, expected_campaign_revision, validated_record)
                except (plan_store.PlanLifecycleConflict, plan_store.PlanLifecycleInvalid) as error:
                    raise PlanActivationConflict("validated lifecycle append conflicted with current plan head") from error
            active_heads = _active_heads(campaign_dir, campaign_id)
            foreign = [item for item in active_heads if item["record_id"] != active_record["record_id"]]
            if foreign:
                raise PlanActivationConflict("another plan is already active")
            try:
                plan_store.store_activated_plan_record(
                    campaign_dir, campaign_id, expected_campaign_revision, active_record, store_authority,
                )
            except (plan_store.PlanLifecycleConflict, plan_store.PlanLifecycleInvalid, plan_store.PlanActivationAuthorityRequired) as error:
                raise PlanActivationConflict("governed active lifecycle append failed") from error

        projection_artifact_id = "plan-activation/projection/" + intent["activation_id"]
        projection_body = {
            "schema": PROJECTION_SCHEMA, "activation_id": intent["activation_id"], "campaign_id": campaign_id,
            "campaign_revision": expected_campaign_revision, "plan_revision_id": plan_revision_id,
            "active_record_id": active_record["record_id"], "graph_digest": precondition["graph_digest"],
            "work_order_set_digest": precondition["work_order_set_digest"], "activated_at": activated_at,
            "authority": {"scope": "active_plan_projection", "repository_write": False, "commit": False, "push": False},
        }
        projection = {**projection_body, "projection_digest": _sha(projection_body)}
        _put_exact(campaign_dir, campaign_id, expected_campaign_revision, "decision", projection_artifact_id, projection)

        receipt_body = {
            "schema": RECEIPT_SCHEMA, "status": "ACTIVE", "activation_id": intent["activation_id"],
            "request_id": request_id, "campaign_id": campaign_id, "campaign_revision": expected_campaign_revision,
            "plan_revision_id": plan_revision_id, "reviewed_record_id": reviewed["record_id"],
            "validated_record_id": validated_record["record_id"], "active_record_id": active_record["record_id"],
            "plan_digest": reviewed["plan_digest"], "review_digest": precondition["review_digest"],
            "dag_validation_id": precondition["dag_validation_id"], "dag_validation_digest": precondition["dag_validation_digest"],
            "graph_digest": precondition["graph_digest"], "work_order_set_digest": precondition["work_order_set_digest"],
            "input_set_digest": precondition["input_set_digest"], "human_decisions_digest": precondition["human_decisions_digest"],
            "activation_authority_digest": precondition["activation_authority_digest"],
            "intent_artifact_id": intent_artifact_id, "intent_digest": intent["intent_digest"],
            "precondition_artifact_id": precondition_artifact_id, "precondition_digest": precondition["precondition_digest"],
            "active_projection_artifact_id": projection_artifact_id, "active_projection_digest": projection["projection_digest"],
            "repository_base_sha": reviewed["base_sha"], "activated_at": activated_at,
            "authority": {"scope": "plan_activation_only", "repository_write": False, "commit": False, "push": False, "merge": False, "release": False, "deploy": False},
        }
        receipt = {**receipt_body, "receipt_digest": _sha(receipt_body)}
        validate_activation_receipt(receipt)
        _put_exact(campaign_dir, campaign_id, expected_campaign_revision, "decision", receipt_artifact_id, receipt)
        return receipt
