#!/usr/bin/env python3
"""Immutable replan triggers and governed plan supersession for P1-013/P1-014."""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import stat
import sys
import tempfile
from contextlib import contextmanager
from typing import Any, Iterator

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import myrmex_campaign_intelligence as intel  # noqa: E402
import myrmex_plan_revision as activation  # noqa: E402
import myrmex_plan_store as plan_store  # noqa: E402
import myrmex_planner as planner  # noqa: E402
import myrmex_planner_gateway as planner_gateway  # noqa: E402
import myrmex_work_unit_compiler as compiler  # noqa: E402

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


def _matches(pattern: str, value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def _is_sha(value: Any) -> bool:
    return isinstance(value, str) and SHA_RE.fullmatch(value) is not None


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


# P1-014 governed replanning -------------------------------------------------
DECISION_SCHEMA = "myrmex.replan-decision/v1"
DISPOSITIONS = {"preserve", "replace", "supersede", "cancel", "defer"}
NEXT_GATES = ["critic", "semantic_dag_validation", "plan_activation"]
DECISION_AUTHORITY = {
    "scope": "replan_decision_only", "activate_candidate": False,
    "repository_write": False, "commit": False, "push": False,
}


def _load_payload(campaign_dir, campaign_id, artifact_id, kind):
    try:
        artifact = intel.get_artifact(pathlib.Path(campaign_dir), campaign_id, artifact_id)["artifact"]
    except Exception as error:
        raise ReplanTriggerInputInvalid(f"required replan artifact unavailable: {artifact_id}") from error
    if artifact.get("kind") != kind or not isinstance(artifact.get("payload"), dict):
        raise ReplanTriggerInputInvalid(f"required replan artifact invalid: {artifact_id}")
    return artifact["payload"], artifact


def _candidate_planner_binding(campaign_dir, campaign_id, candidate):
    request_id = candidate["planning_request_id"]
    key = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
    request, _ = _load_payload(campaign_dir, campaign_id, "planning-request/request/" + key, "plan")
    result, _ = _load_payload(campaign_dir, campaign_id, "planning-result/response/" + key, "plan")
    task_receipt, _ = _load_payload(campaign_dir, campaign_id, "planner-task/receipt/" + key, "decision")
    try:
        planner.validate_planning_result(request, result)
    except Exception as error:
        raise ReplanTriggerInputInvalid("candidate planning result is invalid") from error
    if result.get("response_type") != "plan" or result["plan_revision"]["record_id"] != candidate["record_id"]:
        raise ReplanTriggerInputInvalid("candidate does not match exact planner result")
    planner_receipt = task_receipt.get("planner_receipt")
    if (
        task_receipt.get("schema") != planner_gateway.TASK_RECEIPT_SCHEMA
        or task_receipt.get("request_id") != request_id
        or task_receipt.get("agent") != planner_gateway.AGENT_NAME
        or task_receipt.get("response_type") != "plan"
        or task_receipt.get("result_digest") != result["result_digest"]
        or task_receipt.get("authority") != planner_gateway.AUTHORITY
        or not isinstance(planner_receipt, dict)
        or planner_receipt.get("plan_revision_id") != candidate["plan_revision_id"]
        or planner_receipt.get("record_id") != candidate["record_id"]
        or planner_receipt.get("plan_digest") != candidate["plan_digest"]
    ):
        raise ReplanTriggerInputInvalid("candidate lacks exact planner task receipt")
    return request, result, _sha(task_receipt)


def _semantic_diff(active, candidate):
    old = {wu["id"]: wu for wu in active["work_units"]}; new = {wu["id"]: wu for wu in candidate["work_units"]}
    return {
        "added_work_unit_ids": sorted(set(new) - set(old)),
        "removed_work_unit_ids": sorted(set(old) - set(new)),
        "changed_work_unit_ids": sorted(wu_id for wu_id in set(old) & set(new) if _canon(old[wu_id]) != _canon(new[wu_id])),
        "added_edges": sorted([edge for edge in candidate["edges"] if edge not in active["edges"]]),
        "removed_edges": sorted([edge for edge in active["edges"] if edge not in candidate["edges"]]),
        "assumptions_changed": _canon(active["assumptions"]) != _canon(candidate["assumptions"]),
    }


def _active_campaign_wus(campaign, plan_revision_id):
    result = {}
    for wu in campaign["work_units"]:
        order = wu.get("work_order") if isinstance(wu, dict) else None
        provenance = order.get("plan_provenance") if isinstance(order, dict) else None
        if isinstance(provenance, dict) and provenance.get("plan_revision_id") == plan_revision_id:
            result[wu["id"]] = wu
    return result


def _apply_dispositions(campaign, dispositions, active_plan_id, candidate_ids, trigger_ids, decided_at):
    before = _active_campaign_wus(campaign, active_plan_id)
    if not isinstance(dispositions, list) or len(dispositions) != len(before):
        raise ReplanTriggerInputInvalid("disposition table must cover every active-plan campaign WU exactly once")
    by_id = {}
    fields = {"work_unit_id", "disposition", "replacement_work_unit_ids", "reason", "interruption"}
    active_statuses = {"active", "verifying", "remediating", "ci", "delivering"}
    for row in dispositions:
        if (
            not isinstance(row, dict) or set(row) != fields
            or not isinstance(row.get("work_unit_id"), str)
            or row["work_unit_id"] not in before or row["work_unit_id"] in by_id
        ):
            raise ReplanTriggerInputInvalid("disposition row identity/fields invalid")
        wu = before[row["work_unit_id"]]; disposition = row.get("disposition"); replacements = row.get("replacement_work_unit_ids")
        if (
            disposition not in DISPOSITIONS or not isinstance(replacements, list)
            or any(not isinstance(item, str) for item in replacements)
            or len(replacements) != len(set(replacements))
            or any(item not in candidate_ids for item in replacements)
        ):
            raise ReplanTriggerInputInvalid("disposition/replacement invalid")
        if not isinstance(row.get("reason"), str) or not row["reason"]:
            raise ReplanTriggerInputInvalid("disposition reason required")
        if wu["status"] == "completed" and (disposition != "preserve" or replacements or row["interruption"] is not None):
            raise ReplanTriggerInputInvalid("completed WUs and receipts must be preserved")
        if disposition == "replace" and not replacements:
            raise ReplanTriggerInputInvalid("replace disposition requires replacement WUs")
        if disposition != "replace" and replacements:
            raise ReplanTriggerInputInvalid("only replace may name replacement WUs")
        if wu["status"] in active_statuses:
            interruption = row.get("interruption")
            if disposition == "preserve" or not isinstance(interruption, dict) or set(interruption) != {"transition", "from_status", "from_phase", "evidence_trigger_id"} or interruption.get("transition") != "interrupted_for_replan" or interruption.get("from_status") != wu["status"] or interruption.get("from_phase") != wu["phase"] or interruption.get("evidence_trigger_id") not in trigger_ids:
                raise ReplanTriggerInputInvalid("active/verifying WU requires exact typed interruption")
        elif row.get("interruption") is not None:
            raise ReplanTriggerInputInvalid("inactive WU cannot carry interruption")
        by_id[row["work_unit_id"]] = json.loads(json.dumps(row))
    after = json.loads(json.dumps(campaign))
    for wu in after["work_units"]:
        row = by_id.get(wu["id"])
        if row is None or row["disposition"] == "preserve":
            continue
        immutable = {
            key: json.loads(json.dumps(value))
            for key, value in wu.items()
            if key not in {"status", "phase", "blocker", "next_action", "recovery_events"}
        }
        if row["disposition"] in {"replace", "supersede"}:
            wu["status"] = "superseded"; wu["phase"] = "superseded"
        elif row["disposition"] == "cancel":
            wu["status"] = "cancelled"; wu["phase"] = "cancelled"
        else:
            wu["status"] = "blocked"; wu["phase"] = "blocked"
            wu["blocker"] = {
                "type": "product_change_required", "message": row["reason"],
                "created_at": decided_at, "work_unit_id": wu["id"],
                "context": {"replan_trigger_ids": sorted(trigger_ids)},
            }
        wu["next_action"] = f"replan disposition: {row['disposition']}"
        if row["interruption"] is not None:
            wu.setdefault("recovery_events", []).append({
                **row["interruption"], "replan_trigger_ids": sorted(trigger_ids), "at": decided_at,
            })
        if any(wu.get(key) != value for key, value in immutable.items()):
            raise ReplanTriggerConflict("WU history or receipts changed during disposition")
    after["revision"] = campaign["revision"] + 1; after["updated_at"] = decided_at
    return after, [by_id[key] for key in sorted(by_id)]


def validate_replan_decision(decision):
    fields = {"schema", "decision_id", "decision_digest", "campaign_id", "expected_campaign_revision", "active_plan_revision_id", "active_record_id", "activation_id", "trigger_references", "trigger_set_digest", "candidate_plan_revision_id", "candidate_record_id", "candidate_plan_digest", "planning_request_id", "planning_input_set_digest", "planning_result_digest", "planner_task_receipt_digest", "parent_revision", "semantic_plan_diff", "wu_dispositions", "campaign_state_digest_before", "campaign_state_digest_after", "decided_at", "required_next_gates", "authority"}
    if not isinstance(decision, dict) or set(decision) != fields or decision.get("schema") != DECISION_SCHEMA:
        raise ReplanTriggerInputInvalid("replan decision fields/schema invalid")
    try:
        digest = _sha({key: value for key, value in decision.items() if key not in {"decision_id", "decision_digest"}})
    except (TypeError, ValueError) as error:
        raise ReplanTriggerInputInvalid("replan decision is not canonical JSON") from error
    if decision.get("decision_digest") != digest or decision.get("decision_id") != "replandec_" + digest:
        raise ReplanTriggerInputInvalid("replan decision digest identity invalid")
    if not isinstance(decision.get("campaign_id"), str) or not re.fullmatch(r"camp-[a-z0-9][a-z0-9-]{4,60}", decision["campaign_id"]):
        raise ReplanTriggerInputInvalid("replan decision campaign identity invalid")
    if isinstance(decision.get("expected_campaign_revision"), bool) or not isinstance(decision.get("expected_campaign_revision"), int) or decision["expected_campaign_revision"] < 1:
        raise ReplanTriggerInputInvalid("replan decision campaign revision invalid")
    if (
        not _matches(r"plan_[0-9a-f]{64}", decision.get("active_plan_revision_id"))
        or not _matches(r"planrec_[0-9a-f]{64}", decision.get("active_record_id"))
        or not _matches(r"activation_[0-9a-f]{64}", decision.get("activation_id"))
        or not _matches(r"plan_[0-9a-f]{64}", decision.get("candidate_plan_revision_id"))
        or not _matches(r"planrec_[0-9a-f]{64}", decision.get("candidate_record_id"))
        or decision["active_plan_revision_id"] == decision["candidate_plan_revision_id"]
    ):
        raise ReplanTriggerInputInvalid("replan decision plan identity invalid")
    for field in (
        "candidate_plan_digest", "planning_input_set_digest", "planning_result_digest",
        "planner_task_receipt_digest", "trigger_set_digest", "campaign_state_digest_before",
        "campaign_state_digest_after",
    ):
        if not _is_sha(decision.get(field)):
            raise ReplanTriggerInputInvalid(f"replan decision {field} invalid")
    if not isinstance(decision.get("planning_request_id"), str) or not decision["planning_request_id"]:
        raise ReplanTriggerInputInvalid("replan planning request identity invalid")
    parent = decision.get("parent_revision")
    if (
        not isinstance(parent, dict) or set(parent) != {"artifact_id", "artifact_digest"}
        or parent.get("artifact_id") != "plan-revision/record/" + decision["active_record_id"]
        or not _is_sha(parent.get("artifact_digest"))
    ):
        raise ReplanTriggerInputInvalid("replan parent revision invalid")
    references = decision.get("trigger_references")
    if (
        not isinstance(references, list) or not references
        or any(not isinstance(item, dict) or set(item) != {"trigger_id", "trigger_digest"} or not _matches(r"replantrg_[0-9a-f]{64}", item.get("trigger_id")) or not _is_sha(item.get("trigger_digest")) for item in references)
        or references != sorted(references, key=lambda item: item["trigger_id"])
        or len({item["trigger_id"] for item in references}) != len(references)
        or decision["trigger_set_digest"] != _sha(references)
    ):
        raise ReplanTriggerInputInvalid("replan trigger references invalid")
    semantic = decision.get("semantic_plan_diff")
    semantic_fields = {"added_work_unit_ids", "removed_work_unit_ids", "changed_work_unit_ids", "added_edges", "removed_edges", "assumptions_changed"}
    if not isinstance(semantic, dict) or set(semantic) != semantic_fields or not isinstance(semantic.get("assumptions_changed"), bool):
        raise ReplanTriggerInputInvalid("semantic plan diff invalid")
    for field in ("added_work_unit_ids", "removed_work_unit_ids", "changed_work_unit_ids"):
        values = semantic.get(field)
        if not isinstance(values, list) or any(not _matches(r"WU-[A-Z0-9][A-Z0-9-]{0,30}", item) for item in values) or values != sorted(set(values)):
            raise ReplanTriggerInputInvalid("semantic plan WU diff invalid")
    for field in ("added_edges", "removed_edges"):
        values = semantic.get(field)
        if not isinstance(values, list) or any(not isinstance(item, list) or len(item) != 2 or any(not isinstance(endpoint, str) for endpoint in item) for item in values):
            raise ReplanTriggerInputInvalid("semantic plan edge diff invalid")
        if values != sorted(values) or len({_canon(item) for item in values}) != len(values):
            raise ReplanTriggerInputInvalid("semantic plan edge diff invalid")
    dispositions = decision.get("wu_dispositions")
    disposition_fields = {"work_unit_id", "disposition", "replacement_work_unit_ids", "reason", "interruption"}
    if not isinstance(dispositions, list) or not dispositions:
        raise ReplanTriggerInputInvalid("replan disposition table invalid")
    seen = set()
    for row in dispositions:
        if (
            not isinstance(row, dict) or set(row) != disposition_fields
            or not _matches(r"WU-[A-Z0-9][A-Z0-9-]{0,30}", row.get("work_unit_id"))
            or (isinstance(row.get("work_unit_id"), str) and row["work_unit_id"] in seen)
            or row.get("disposition") not in DISPOSITIONS
            or not isinstance(row.get("replacement_work_unit_ids"), list)
            or any(not _matches(r"WU-[A-Z0-9][A-Z0-9-]{0,30}", item) for item in row.get("replacement_work_unit_ids", []))
            or len(row["replacement_work_unit_ids"]) != len(set(row["replacement_work_unit_ids"]))
            or not isinstance(row.get("reason"), str) or not row["reason"]
        ):
            raise ReplanTriggerInputInvalid("replan disposition row invalid")
        interruption = row.get("interruption")
        if interruption is not None and (
            not isinstance(interruption, dict)
            or set(interruption) != {"transition", "from_status", "from_phase", "evidence_trigger_id"}
            or interruption.get("transition") != "interrupted_for_replan"
            or not all(isinstance(interruption.get(key), str) and interruption[key] for key in ("from_status", "from_phase"))
            or interruption.get("evidence_trigger_id") not in {item["trigger_id"] for item in references}
        ):
            raise ReplanTriggerInputInvalid("replan interruption invalid")
        seen.add(row["work_unit_id"])
    if dispositions != sorted(dispositions, key=lambda item: item["work_unit_id"]):
        raise ReplanTriggerInputInvalid("replan disposition table must be in canonical WU order")
    if decision.get("required_next_gates") != NEXT_GATES or decision.get("authority") != DECISION_AUTHORITY:
        raise ReplanTriggerInputInvalid("replan decision authority/gates invalid")
    _time(decision.get("decided_at"), "replan.decided_at")


def _replan_intents(campaign_dir, campaign_id):
    result = []
    for descriptor in _decision_descriptors(campaign_dir, campaign_id):
        artifact_id = descriptor.get("artifact_id") if isinstance(descriptor, dict) else None
        if not isinstance(artifact_id, str) or not artifact_id.startswith("replan-decision/intent/"):
            continue
        payload, _ = _load_payload(campaign_dir, campaign_id, artifact_id, "decision")
        validate_replan_decision(payload)
        if artifact_id != "replan-decision/intent/" + payload["decision_id"]:
            raise ReplanTriggerConflict("replan intent identity mismatch")
        result.append(payload)
    return result


def _reject_competing_decision(campaign_dir, campaign_id, decision):
    for existing in _replan_intents(campaign_dir, campaign_id):
        same_source_state = (
            existing["active_record_id"] == decision["active_record_id"]
            and existing["expected_campaign_revision"] == decision["expected_campaign_revision"]
        )
        same_trigger_inputs = (
            existing["active_plan_revision_id"] == decision["active_plan_revision_id"]
            and existing["trigger_set_digest"] == decision["trigger_set_digest"]
            and existing["planning_input_set_digest"] == decision["planning_input_set_digest"]
        )
        if (same_source_state or same_trigger_inputs) and existing["decision_id"] != decision["decision_id"]:
            raise ReplanTriggerConflict("a conflicting replan decision already governs this plan/trigger/input")


def validate_replan_receipt(receipt):
    fields = {"schema", "status", "decision_id", "decision_digest", "campaign_id", "campaign_revision_before", "campaign_revision_after", "superseded_record_id", "candidate_plan_revision_id", "required_next_gates", "authority", "receipt_digest"}
    if not isinstance(receipt, dict) or set(receipt) != fields or receipt.get("schema") != "myrmex.replan-apply-receipt/v1" or receipt.get("status") != "APPLIED":
        raise ReplanTriggerInputInvalid("replan receipt fields/schema/status invalid")
    try:
        digest = _sha({key: value for key, value in receipt.items() if key != "receipt_digest"})
    except (TypeError, ValueError) as error:
        raise ReplanTriggerInputInvalid("replan receipt is not canonical JSON") from error
    if receipt.get("receipt_digest") != digest:
        raise ReplanTriggerInputInvalid("replan receipt digest invalid")
    if not _matches(r"replandec_[0-9a-f]{64}", receipt.get("decision_id")) or not _is_sha(receipt.get("decision_digest")):
        raise ReplanTriggerInputInvalid("replan receipt decision identity invalid")
    if isinstance(receipt.get("campaign_revision_before"), bool) or not isinstance(receipt.get("campaign_revision_before"), int) or receipt.get("campaign_revision_after") != receipt["campaign_revision_before"] + 1:
        raise ReplanTriggerInputInvalid("replan receipt campaign revision invalid")
    if not _matches(r"camp-[a-z0-9][a-z0-9-]{4,60}", receipt.get("campaign_id")):
        raise ReplanTriggerInputInvalid("replan receipt campaign identity invalid")
    if not _matches(r"planrec_[0-9a-f]{64}", receipt.get("superseded_record_id")) or not _matches(r"plan_[0-9a-f]{64}", receipt.get("candidate_plan_revision_id")):
        raise ReplanTriggerInputInvalid("replan receipt plan identity invalid")
    if receipt.get("required_next_gates") != NEXT_GATES or receipt.get("authority") != DECISION_AUTHORITY:
        raise ReplanTriggerInputInvalid("replan receipt authority/gates invalid")


def preview_replan(campaign_dir, campaign_id, expected_revision, candidate_plan_revision_id, trigger_ids, dispositions, decided_at):
    campaign = _campaign(campaign_dir, campaign_id, expected_revision)
    ledger = {item["trigger_id"]: item for item in list_triggers(campaign_dir, campaign_id)["triggers"]}
    active_head, active_receipt = _active_activation(campaign_dir, campaign_id)
    try:
        active = plan_store.get_plan_record(campaign_dir, campaign_id, active_head["record_id"])
        candidate = compiler._reviewed_head_read_only(campaign_dir, campaign_id, candidate_plan_revision_id)
    except Exception as error:
        raise ReplanTriggerInputInvalid("active/candidate plan unavailable") from error
    if candidate["lifecycle_status"] != "proposed" or candidate["campaign_id"] != campaign_id or candidate["objective_id"] != active["objective_id"]:
        raise ReplanTriggerInputInvalid("candidate must be a proposed plan for the active objective")
    _, active_artifact = _load_payload(campaign_dir, campaign_id, "plan-revision/record/" + active["record_id"], "plan")
    parent = {"artifact_id": active_artifact["artifact_id"], "artifact_digest": active_artifact["artifact_digest"]}
    if candidate["parent_revision"] != parent:
        raise ReplanTriggerInputInvalid("candidate parent does not reference exact active record")
    request, result, task_receipt_digest = _candidate_planner_binding(campaign_dir, campaign_id, candidate)
    if request["parent_revision"] != parent:
        raise ReplanTriggerInputInvalid("planning request parent mismatch")
    if not isinstance(trigger_ids, list) or not trigger_ids or any(not isinstance(item, str) for item in trigger_ids) or len(trigger_ids) != len(set(trigger_ids)) or any(item not in ledger for item in trigger_ids):
        raise ReplanTriggerInputInvalid("trigger set invalid")
    trigger_refs = [{"trigger_id": item, "trigger_digest": ledger[item]["trigger_digest"]} for item in sorted(trigger_ids)]
    decision_time = _time(decided_at, "replan.decided_at")
    trigger_time = max(_time(ledger[item]["detected_at"], "trigger.detected_at") for item in trigger_ids)
    request_time = _time(request["created_at"], "planning_request.created_at")
    result_time = _time(result["created_at"], "planning_result.created_at")
    candidate_time = _time(candidate["created_at"], "candidate.created_at")
    if not (trigger_time <= request_time <= candidate_time <= result_time <= decision_time):
        raise ReplanTriggerStale("candidate must be produced after its triggers and no later than the replan decision")
    active_campaign_ids = set(_active_campaign_wus(campaign, active["plan_revision_id"]))
    if active_campaign_ids != {wu["id"] for wu in active["work_units"]}:
        raise ReplanTriggerConflict("campaign does not contain the exact active-plan WU set")
    candidate_ids = {wu["id"] for wu in candidate["work_units"]}
    if candidate_ids & {wu["id"] for wu in campaign["work_units"]}:
        raise ReplanTriggerInputInvalid("candidate WU IDs must not reuse campaign history")
    after, table = _apply_dispositions(campaign, dispositions, active["plan_revision_id"], candidate_ids, set(trigger_ids), decided_at)
    body = {"schema": DECISION_SCHEMA, "campaign_id": campaign_id, "expected_campaign_revision": expected_revision, "active_plan_revision_id": active["plan_revision_id"], "active_record_id": active["record_id"], "activation_id": active_receipt["activation_id"], "trigger_references": trigger_refs, "trigger_set_digest": _sha(trigger_refs), "candidate_plan_revision_id": candidate["plan_revision_id"], "candidate_record_id": candidate["record_id"], "candidate_plan_digest": candidate["plan_digest"], "planning_request_id": request["request_id"], "planning_input_set_digest": _sha(request["input_digests"]), "planning_result_digest": result["result_digest"], "planner_task_receipt_digest": task_receipt_digest, "parent_revision": parent, "semantic_plan_diff": _semantic_diff(active, candidate), "wu_dispositions": table, "campaign_state_digest_before": _sha(campaign), "campaign_state_digest_after": _sha(after), "decided_at": decided_at, "required_next_gates": list(NEXT_GATES), "authority": dict(DECISION_AUTHORITY)}
    digest = _sha(body); decision = {**body, "decision_id": "replandec_" + digest, "decision_digest": digest}
    validate_replan_decision(decision)
    _reject_competing_decision(campaign_dir, campaign_id, decision)
    return {"status": "PREVIEW", "decision": decision, "campaign_after": after}


def _write_campaign(path, expected_digest, after):
    try:
        if path.is_symlink() or not path.is_file():
            raise ReplanTriggerBackendUnavailable("campaign state path unavailable or unsafe")
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ReplanTriggerBackendUnavailable("campaign state unreadable during replan apply") from error
    if _sha(current) != expected_digest:
        raise ReplanTriggerStale("campaign changed before replan apply")
    temp = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent), delete=False) as stream:
            temp = pathlib.Path(stream.name)
            json.dump(after, stream, indent=2); stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
        os.replace(temp, path)
        directory_fd = os.open(str(path.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as error:
        if temp is not None:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
        raise ReplanTriggerBackendUnavailable("atomic campaign replan persistence failed") from error


def apply_replan(campaign_dir, campaign_id, decision):
    validate_replan_decision(decision)
    if decision["campaign_id"] != campaign_id:
        raise ReplanTriggerInputInvalid("replan decision belongs to a different campaign")
    campaign_dir = pathlib.Path(campaign_dir)
    with _trigger_lock(campaign_dir):
        with activation._activation_lock(campaign_dir):
            receipt_id = "replan-decision/receipt/" + decision["decision_id"]
            try:
                existing = intel.get_artifact(campaign_dir, campaign_id, receipt_id)["artifact"]["payload"]
            except intel.IntelligenceArtifactInvalid as error:
                if not str(error).startswith("artifact not found"): raise ReplanTriggerBackendUnavailable("replan receipt corrupt") from error
            else:
                validate_replan_receipt(existing)
                if existing["decision_id"] != decision["decision_id"] or existing["decision_digest"] != decision["decision_digest"] or existing["campaign_id"] != campaign_id or existing["candidate_plan_revision_id"] != decision["candidate_plan_revision_id"]:
                    raise ReplanTriggerConflict("replan replay mismatch")
                return {"status": "REUSED", "receipt": existing}
            _reject_competing_decision(campaign_dir, campaign_id, decision)
            intent_id = "replan-decision/intent/" + decision["decision_id"]
            try:
                intel.put_artifact(campaign_dir, campaign_id, decision["expected_campaign_revision"], "decision", intent_id, decision)
            except intel.IntelligenceArtifactConflict as error:
                stored = intel.get_artifact(campaign_dir, campaign_id, intent_id)["artifact"]["payload"]
                if _canon(stored) != _canon(decision): raise ReplanTriggerConflict("replan intent conflict") from error
            path = campaign_dir / "campaign.json"
            try:
                if path.is_symlink() or not path.is_file():
                    raise ReplanTriggerBackendUnavailable("campaign state path unavailable or unsafe")
                current = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise ReplanTriggerBackendUnavailable("campaign state unreadable during replan recovery") from error
            preview = preview_replan(campaign_dir, campaign_id, decision["expected_campaign_revision"], decision["candidate_plan_revision_id"], [item["trigger_id"] for item in decision["trigger_references"]], decision["wu_dispositions"], decision["decided_at"]) if _sha(current) == decision["campaign_state_digest_before"] else None
            if preview is not None:
                if preview["decision"] != decision: raise ReplanTriggerStale("replan decision no longer matches current state")
                _write_campaign(path, decision["campaign_state_digest_before"], preview["campaign_after"])
            elif _sha(current) != decision["campaign_state_digest_after"]:
                raise ReplanTriggerStale("campaign is neither pre-apply nor exact recovered state")
            active = plan_store.get_plan_record(campaign_dir, campaign_id, decision["active_record_id"])
            superseded = plan_store.build_lifecycle_record(active, "superseded", decision["decided_at"])
            head = plan_store.get_plan_head(campaign_dir, campaign_id, decision["active_plan_revision_id"])
            if head["record_id"] == active["record_id"]:
                plan_store.store_plan_record(campaign_dir, campaign_id, decision["expected_campaign_revision"] + 1, superseded)
            elif head["record_id"] != superseded["record_id"]:
                raise ReplanTriggerConflict("active plan lifecycle changed during apply")
            receipt_body = {"schema": "myrmex.replan-apply-receipt/v1", "status": "APPLIED", "decision_id": decision["decision_id"], "decision_digest": decision["decision_digest"], "campaign_id": campaign_id, "campaign_revision_before": decision["expected_campaign_revision"], "campaign_revision_after": decision["expected_campaign_revision"] + 1, "superseded_record_id": superseded["record_id"], "candidate_plan_revision_id": decision["candidate_plan_revision_id"], "required_next_gates": decision["required_next_gates"], "authority": decision["authority"]}
            receipt = {**receipt_body, "receipt_digest": _sha(receipt_body)}
            validate_replan_receipt(receipt)
            try:
                intel.put_artifact(campaign_dir, campaign_id, decision["expected_campaign_revision"] + 1, "decision", receipt_id, receipt)
            except intel.IntelligenceArtifactConflict as error:
                stored = intel.get_artifact(campaign_dir, campaign_id, receipt_id)["artifact"]["payload"]
                if _canon(stored) != _canon(receipt):
                    raise ReplanTriggerConflict("replan receipt conflict") from error
            return {"status": "APPLIED", "receipt": receipt}
