#!/usr/bin/env python3
"""Immutable replan-trigger ledger for P1-013."""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import stat
import sys
from contextlib import contextmanager
from typing import Any, Iterator

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import myrmex_campaign_intelligence as intel  # noqa: E402
import myrmex_plan_revision as activation  # noqa: E402
import myrmex_plan_store as plan_store  # noqa: E402

TRIGGER_SCHEMA = "myrmex.replan-trigger/v1"
TRIGGER_TYPES = {
    "source_change", "repository_drift", "blocker", "failure", "defect",
    "budget", "provider", "memory_refutation", "human_decision",
}
KINDS = {"backlog", "plan", "review", "decision"}
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class ReplanTriggerError(Exception): pass
class ReplanTriggerInputInvalid(ReplanTriggerError): pass
class ReplanTriggerStale(ReplanTriggerError): pass
class ReplanTriggerConflict(ReplanTriggerError): pass
class ReplanTriggerBackendUnavailable(ReplanTriggerError): pass


def _canon(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canon(value)).hexdigest()


def _time(value: Any, label: str):
    try:
        return activation._time(value, label)
    except Exception as error:
        raise ReplanTriggerInputInvalid(str(error)) from error


@contextmanager
def _trigger_lock(campaign_dir: pathlib.Path) -> Iterator[None]:
    try:
        import fcntl
    except ImportError as error:
        raise ReplanTriggerBackendUnavailable("fcntl unavailable; trigger ledger fails closed") from error
    intelligence = pathlib.Path(campaign_dir) / "intelligence"
    if intelligence.is_symlink() or not intelligence.is_dir():
        raise ReplanTriggerBackendUnavailable("campaign intelligence directory unavailable or unsafe")
    path = intelligence / "replan-trigger.lock"
    if path.is_symlink():
        raise ReplanTriggerBackendUnavailable("replan trigger lock must not be a symlink")
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as error:
        raise ReplanTriggerBackendUnavailable("cannot open replan trigger lock") from error
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ReplanTriggerBackendUnavailable("replan trigger lock must be regular")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def _campaign(campaign_dir, campaign_id, expected_revision):
    path = pathlib.Path(campaign_dir) / "campaign.json"
    try:
        if path.is_symlink() or not path.is_file():
            raise ReplanTriggerBackendUnavailable("campaign state unavailable or unsafe")
        campaign = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ReplanTriggerBackendUnavailable("campaign state unreadable") from error
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 1:
        raise ReplanTriggerInputInvalid("expected campaign revision must be positive")
    if not isinstance(campaign, dict) or campaign.get("id") != campaign_id or campaign.get("revision") != expected_revision:
        raise ReplanTriggerStale("campaign identity or revision is stale")
    return campaign


def _decision_descriptors(campaign_dir, campaign_id):
    doctor = intel.doctor(pathlib.Path(campaign_dir), campaign_id)
    if doctor.get("status") != "healthy":
        raise ReplanTriggerBackendUnavailable("intelligence projection is not cross-checked against the durable artifact store")
    listing = intel.list_artifacts(pathlib.Path(campaign_dir), campaign_id, kind="decision")
    if listing.get("status") != "healthy":
        raise ReplanTriggerBackendUnavailable("decision projection unavailable; ledger will not repair it")
    return listing.get("artifacts", {}).get("decision", [])


def _active_activation(campaign_dir, campaign_id):
    active_heads = activation._active_heads(campaign_dir, campaign_id)
    if len(active_heads) != 1:
        raise ReplanTriggerInputInvalid("trigger recording requires exactly one active plan")
    head = active_heads[0]
    matches = []
    for descriptor in _decision_descriptors(campaign_dir, campaign_id):
        artifact_id = descriptor.get("artifact_id") if isinstance(descriptor, dict) else None
        if not isinstance(artifact_id, str) or not artifact_id.startswith("plan-activation/receipt/"):
            continue
        try:
            receipt = intel.get_artifact(pathlib.Path(campaign_dir), campaign_id, artifact_id)["artifact"]["payload"]
            activation.validate_activation_receipt(receipt)
            intent = intel.get_artifact(pathlib.Path(campaign_dir), campaign_id, receipt["intent_artifact_id"])["artifact"]["payload"]
            activation._validate_confirmed_chain(campaign_dir, campaign_id, receipt, intent)
        except Exception as error:
            raise ReplanTriggerBackendUnavailable("activation receipt ledger is corrupt") from error
        if receipt["active_record_id"] == head["record_id"] and receipt["plan_revision_id"] == head["plan_revision_id"]:
            matches.append(receipt)
    if len(matches) != 1:
        raise ReplanTriggerInputInvalid("active plan lacks one exact activation receipt")
    return head, matches[0]


def evidence_reference(campaign_dir, campaign_id, artifact_id):
    try:
        artifact = intel.get_artifact(pathlib.Path(campaign_dir), campaign_id, artifact_id)["artifact"]
    except Exception as error:
        raise ReplanTriggerInputInvalid(f"evidence unavailable: {artifact_id}") from error
    return {
        "campaign_id": artifact["campaign_id"], "artifact_id": artifact["artifact_id"], "kind": artifact["kind"],
        "artifact_digest": artifact["artifact_digest"], "payload_digest": artifact["payload_digest"],
        "observed_campaign_revision": artifact["observed_campaign_revision"], "created_at": artifact["created_at"],
    }


def _verify_reference(campaign_dir, campaign_id, reference, minimum_revision):
    fields = {"campaign_id", "artifact_id", "kind", "artifact_digest", "payload_digest", "observed_campaign_revision", "created_at"}
    if not isinstance(reference, dict) or set(reference) != fields:
        raise ReplanTriggerInputInvalid("evidence reference fields invalid")
    if reference.get("campaign_id") != campaign_id or reference.get("kind") not in KINDS:
        raise ReplanTriggerInputInvalid("evidence reference belongs to the wrong campaign or kind")
    if not SHA_RE.fullmatch(reference.get("artifact_digest", "")) or not SHA_RE.fullmatch(reference.get("payload_digest", "")):
        raise ReplanTriggerInputInvalid("evidence reference digests invalid")
    actual = evidence_reference(campaign_dir, campaign_id, reference.get("artifact_id"))
    if actual != reference:
        raise ReplanTriggerConflict("evidence reference does not match immutable artifact")
    if isinstance(reference["observed_campaign_revision"], bool) or not isinstance(reference["observed_campaign_revision"], int) or reference["observed_campaign_revision"] < minimum_revision:
        raise ReplanTriggerStale("evidence predates active-plan campaign revision")
    _time(reference["created_at"], "evidence.created_at")
    return actual


def validate_trigger(trigger: Any) -> None:
    fields = {
        "schema", "trigger_id", "trigger_digest", "campaign_id", "observed_campaign_revision",
        "active_plan_revision_id", "active_record_id", "activation_id", "activation_receipt_digest",
        "trigger_type", "summary", "evidence_references", "evidence_set_digest", "detected_at", "source", "authority",
    }
    if not isinstance(trigger, dict) or set(trigger) != fields or trigger.get("schema") != TRIGGER_SCHEMA:
        raise ReplanTriggerInputInvalid("replan trigger fields/schema invalid")
    digest = _sha({key: value for key, value in trigger.items() if key not in {"trigger_id", "trigger_digest"}})
    if trigger.get("trigger_digest") != digest or trigger.get("trigger_id") != "replantrg_" + digest:
        raise ReplanTriggerInputInvalid("replan trigger digest identity invalid")
    if trigger.get("trigger_type") not in TRIGGER_TYPES or not isinstance(trigger.get("summary"), str) or not trigger["summary"] or len(trigger["summary"]) > 2048:
        raise ReplanTriggerInputInvalid("replan trigger type/summary invalid")
    if not isinstance(trigger.get("campaign_id"), str) or not re.fullmatch(r"camp-[a-z0-9][a-z0-9-]{4,60}", trigger["campaign_id"]):
        raise ReplanTriggerInputInvalid("replan trigger campaign identity invalid")
    if isinstance(trigger.get("observed_campaign_revision"), bool) or not isinstance(trigger.get("observed_campaign_revision"), int) or trigger["observed_campaign_revision"] < 1:
        raise ReplanTriggerInputInvalid("replan trigger campaign revision invalid")
    if not re.fullmatch(r"plan_[0-9a-f]{64}", trigger.get("active_plan_revision_id", "")) or not re.fullmatch(r"planrec_[0-9a-f]{64}", trigger.get("active_record_id", "")) or not re.fullmatch(r"activation_[0-9a-f]{64}", trigger.get("activation_id", "")) or not SHA_RE.fullmatch(trigger.get("activation_receipt_digest", "")):
        raise ReplanTriggerInputInvalid("replan trigger active-plan identity invalid")
    refs = trigger.get("evidence_references")
    if not isinstance(refs, list) or not refs or refs != sorted(refs, key=lambda item: _canon(item)) or len({_canon(item) for item in refs}) != len(refs):
        raise ReplanTriggerInputInvalid("evidence references must be non-empty, unique, canonical order")
    if trigger.get("evidence_set_digest") != _sha(refs):
        raise ReplanTriggerInputInvalid("evidence set digest invalid")
    if not isinstance(trigger.get("source"), dict) or set(trigger["source"]) != {"producer", "event_id"} or not all(isinstance(trigger["source"][key], str) and trigger["source"][key] for key in trigger["source"]):
        raise ReplanTriggerInputInvalid("replan trigger source invalid")
    expected_authority = {"scope": "replan_signal_only", "mutate_active_plan": False, "create_plan_revision": False, "create_work_units": False, "repository_write": False, "commit": False, "push": False}
    if trigger.get("authority") != expected_authority:
        raise ReplanTriggerInputInvalid("replan trigger authority invalid")
    _time(trigger.get("detected_at"), "trigger.detected_at")


def record_trigger(
    campaign_dir, campaign_id, expected_campaign_revision, trigger_type, summary,
    evidence_references, detected_at, source,
):
    campaign_dir = pathlib.Path(campaign_dir)
    with _trigger_lock(campaign_dir):
        _campaign(campaign_dir, campaign_id, expected_campaign_revision)
        head, receipt = _active_activation(campaign_dir, campaign_id)
        if trigger_type not in TRIGGER_TYPES:
            raise ReplanTriggerInputInvalid("unsupported replan trigger type")
        if not isinstance(evidence_references, list) or not evidence_references:
            raise ReplanTriggerInputInvalid("at least one exact evidence reference is required")
        if _time(detected_at, "trigger.detected_at") < _time(receipt["activated_at"], "activation.activated_at"):
            raise ReplanTriggerStale("trigger detection predates active-plan activation")
        references = sorted([
            _verify_reference(campaign_dir, campaign_id, reference, receipt["campaign_revision"])
            for reference in evidence_references
        ], key=lambda item: _canon(item))
        if len({_canon(item) for item in references}) != len(references):
            raise ReplanTriggerInputInvalid("duplicate evidence references are forbidden")
        body = {
            "schema": TRIGGER_SCHEMA, "campaign_id": campaign_id,
            "observed_campaign_revision": expected_campaign_revision,
            "active_plan_revision_id": head["plan_revision_id"], "active_record_id": head["record_id"],
            "activation_id": receipt["activation_id"], "activation_receipt_digest": receipt["receipt_digest"],
            "trigger_type": trigger_type, "summary": summary, "evidence_references": references,
            "evidence_set_digest": _sha(references), "detected_at": detected_at, "source": source,
            "authority": {"scope": "replan_signal_only", "mutate_active_plan": False, "create_plan_revision": False, "create_work_units": False, "repository_write": False, "commit": False, "push": False},
        }
        digest = _sha(body)
        trigger = {**body, "trigger_id": "replantrg_" + digest, "trigger_digest": digest}
        validate_trigger(trigger)
        current_head, current_receipt = _active_activation(campaign_dir, campaign_id)
        if current_head["record_id"] != head["record_id"] or current_receipt["receipt_digest"] != receipt["receipt_digest"]:
            raise ReplanTriggerStale("active plan changed while recording trigger")
        artifact_id = "replan-trigger/record/" + trigger["trigger_id"]
        try:
            existing = intel.get_artifact(campaign_dir, campaign_id, artifact_id)["artifact"]
        except intel.IntelligenceArtifactInvalid as error:
            if not str(error).startswith("artifact not found"):
                raise ReplanTriggerBackendUnavailable("trigger artifact is corrupt") from error
        except Exception as error:
            raise ReplanTriggerBackendUnavailable("trigger artifact lookup failed") from error
        else:
            if existing.get("kind") != "decision" or _canon(existing.get("payload")) != _canon(trigger):
                raise ReplanTriggerConflict("trigger identity already exists with different payload")
            return {"status": "REUSED", "artifact_id": artifact_id, "trigger": trigger}
        try:
            intel.put_artifact(campaign_dir, campaign_id, expected_campaign_revision, "decision", artifact_id, trigger)
        except intel.IntelligenceArtifactConflict as error:
            raise ReplanTriggerConflict("concurrent trigger conflict") from error
        except Exception as error:
            raise ReplanTriggerBackendUnavailable("trigger persistence failed") from error
        return {"status": "CREATED", "artifact_id": artifact_id, "trigger": trigger}


def list_triggers(campaign_dir, campaign_id):
    records = []
    for descriptor in _decision_descriptors(campaign_dir, campaign_id):
        artifact_id = descriptor.get("artifact_id") if isinstance(descriptor, dict) else None
        if not isinstance(artifact_id, str) or not artifact_id.startswith("replan-trigger/record/"):
            continue
        try:
            artifact = intel.get_artifact(pathlib.Path(campaign_dir), campaign_id, artifact_id)["artifact"]
            trigger = artifact["payload"]
            validate_trigger(trigger)
            if trigger["campaign_id"] != campaign_id or artifact.get("campaign_id") != campaign_id:
                raise ReplanTriggerConflict("trigger payload belongs to a different campaign")
            receipt_artifact_id = "plan-activation/receipt/" + trigger["activation_id"]
            receipt = intel.get_artifact(pathlib.Path(campaign_dir), campaign_id, receipt_artifact_id)["artifact"]["payload"]
            activation.validate_activation_receipt(receipt)
            if receipt["receipt_digest"] != trigger["activation_receipt_digest"] or receipt["active_record_id"] != trigger["active_record_id"]:
                raise ReplanTriggerConflict("trigger activation reference is stale or corrupt")
            for reference in trigger["evidence_references"]:
                _verify_reference(campaign_dir, campaign_id, reference, receipt["campaign_revision"])
        except ReplanTriggerError:
            raise
        except Exception as error:
            raise ReplanTriggerBackendUnavailable(f"trigger ledger corrupt: {artifact_id}") from error
        if artifact.get("kind") != "decision" or artifact_id != "replan-trigger/record/" + trigger["trigger_id"]:
            raise ReplanTriggerConflict("trigger artifact identity mismatch")
        records.append(trigger)
    records.sort(key=lambda item: (item["detected_at"], item["trigger_id"]))
    return {
        "schema": "myrmex.replan-trigger-ledger/v1", "campaign_id": campaign_id,
        "trigger_count": len(records), "triggers": records,
        "ledger_digest": _sha([[item["trigger_id"], item["trigger_digest"]] for item in records]),
        "authority": {"scope": "read_only", "mutate_active_plan": False, "repository_write": False},
    }
