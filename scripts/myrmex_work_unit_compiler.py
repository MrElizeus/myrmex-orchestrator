#!/usr/bin/env python3
"""Deterministic reviewed-plan to campaign WorkUnit compiler for P1-010."""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import myrmex_campaign_intelligence as intel  # noqa: E402
import myrmex_backlog_normalizer as backlog  # noqa: E402
import myrmex_plan_store as plan_store  # noqa: E402
import myrmex_planner as planner  # noqa: E402

WORK_ORDER_SCHEMA = "myrmex.work-order/v2"
COMPILATION_SCHEMA = "myrmex.work-unit-compilation/v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class WorkUnitCompilerError(Exception): pass
class WorkUnitCompileInputInvalid(WorkUnitCompilerError): pass
class WorkUnitCompileConflict(WorkUnitCompilerError): pass
class WorkUnitCompileStale(WorkUnitCompilerError): pass


def _canon(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canon(value)).hexdigest()


def _load_payload(campaign_dir, campaign_id, artifact_id, kind):
    try:
        envelope = intel.get_artifact(pathlib.Path(campaign_dir), campaign_id, artifact_id)["artifact"]
    except Exception as error:
        raise WorkUnitCompileInputInvalid(f"required artifact unavailable: {artifact_id}") from error
    if envelope.get("kind") != kind or not isinstance(envelope.get("payload"), dict):
        raise WorkUnitCompileInputInvalid(f"required artifact invalid: {artifact_id}")
    return envelope["payload"]


def _decision_artifact_ids(campaign_dir, campaign_id) -> list[str]:
    listing = intel.list_artifacts(pathlib.Path(campaign_dir), campaign_id, kind="decision")
    if listing.get("status") != "healthy":
        raise WorkUnitCompileInputInvalid("decision projection is unavailable; preview will not repair state")
    artifacts = listing.get("artifacts", {})
    descriptors = artifacts.get("decision", []) if isinstance(artifacts, dict) else []
    return [entry["artifact_id"] for entry in descriptors if isinstance(entry, dict) and isinstance(entry.get("artifact_id"), str)]


def _reviewed_head_read_only(campaign_dir, campaign_id, plan_revision_id):
    """Resolve one plan head without invoking projection-repairing plan-store reads."""
    listing = intel.list_artifacts(pathlib.Path(campaign_dir), campaign_id, kind="plan")
    if listing.get("status") != "healthy":
        raise WorkUnitCompileInputInvalid("plan projection is unavailable; preview will not repair state")
    artifacts = listing.get("artifacts", {})
    descriptors = artifacts.get("plan", []) if isinstance(artifacts, dict) else []
    records = []
    for descriptor in descriptors:
        artifact_id = descriptor.get("artifact_id") if isinstance(descriptor, dict) else None
        if not isinstance(artifact_id, str) or not artifact_id.startswith("plan-revision/record/"):
            continue
        record = _load_payload(campaign_dir, campaign_id, artifact_id, "plan")
        try:
            plan_store.validate_plan_revision_record(record)
        except Exception as error:
            raise WorkUnitCompileInputInvalid("plan record is invalid") from error
        if record["plan_revision_id"] == plan_revision_id:
            records.append(record)
    if not records:
        raise WorkUnitCompileInputInvalid("plan revision is unavailable")
    referenced = {record["previous_record_id"] for record in records if record["previous_record_id"] is not None}
    heads = [record for record in records if record["record_id"] not in referenced]
    if len(heads) != 1:
        raise WorkUnitCompileInputInvalid("plan lifecycle head is missing or ambiguous")
    return heads[0]


def _review_receipt(campaign_dir, campaign_id, reviewed: dict[str, Any]) -> dict[str, Any]:
    matches = []
    for artifact_id in _decision_artifact_ids(campaign_dir, campaign_id):
        if not artifact_id.startswith("plan-critic/receipt/"):
            continue
        receipt = _load_payload(campaign_dir, campaign_id, artifact_id, "decision")
        if (
            receipt.get("schema") == "myrmex.plan-critic-task-receipt/v1"
            and receipt.get("verdict") == "PASS"
            and receipt.get("plan_revision_id") == reviewed["plan_revision_id"]
            and receipt.get("reviewed_plan_record_id") == reviewed["record_id"]
            and receipt.get("plan_record_id") == reviewed["previous_record_id"]
            and receipt.get("identity_separation") is True
        ):
            matches.append(receipt)
    if len(matches) != 1 or not isinstance(matches[0].get("review_digest"), str) or not SHA256_RE.fullmatch(matches[0]["review_digest"]):
        raise WorkUnitCompileInputInvalid("reviewed plan lacks one exact PASS critic receipt")
    return matches[0]


def _planning_result(campaign_dir, campaign_id, reviewed):
    request_id = reviewed["planning_request_id"]
    artifact_id = "planning-result/response/" + hashlib.sha256(request_id.encode("utf-8")).hexdigest()
    result = _load_payload(campaign_dir, campaign_id, artifact_id, "plan")
    request_id_artifact = "planning-request/request/" + hashlib.sha256(request_id.encode("utf-8")).hexdigest()
    request = _load_payload(campaign_dir, campaign_id, request_id_artifact, "plan")
    try:
        planner.validate_planning_result(request, result)
    except Exception as error:
        raise WorkUnitCompileInputInvalid("planning result is invalid") from error
    if result.get("response_type") != "plan" or result["plan_revision"]["plan_revision_id"] != reviewed["plan_revision_id"]:
        raise WorkUnitCompileInputInvalid("planning result does not match reviewed plan")
    return request, result


def _backlog_items(campaign_dir, campaign_id, request):
    backlog_input = next((entry for entry in request["input_digests"] if entry["kind"] == "normalized-backlog"), None)
    if not isinstance(backlog_input, dict):
        raise WorkUnitCompileInputInvalid("reviewed plan lacks normalized backlog input")
    snapshot = _load_payload(campaign_dir, campaign_id, backlog_input["identity"], "backlog")
    try:
        backlog.validate_normalized_snapshot(snapshot)
    except Exception as error:
        raise WorkUnitCompileInputInvalid("normalized backlog snapshot is invalid") from error
    items = {}
    for descriptor in snapshot["items"]:
        item = _load_payload(campaign_dir, campaign_id, descriptor["artifact_id"], "backlog")
        try:
            backlog.validate_normalized_item(item)
        except Exception as error:
            raise WorkUnitCompileInputInvalid("normalized backlog item is invalid") from error
        items[item["backlog_item_id"]] = item
    return snapshot, items


def validate_work_order(order: Any) -> None:
    required = {
        "schema", "work_order_id", "work_order_digest", "campaign_id", "work_unit_id",
        "plan_provenance", "backlog_provenance", "objective", "non_goals", "dependencies",
        "repository_root", "base_sha", "scope", "acceptance_criteria", "verification",
        "risk_class", "required_route", "human_gates", "expected_evidence", "terminal_gate", "git_policy", "no_op_allowed",
    }
    if not isinstance(order, dict) or set(order) != required or order.get("schema") != WORK_ORDER_SCHEMA:
        raise WorkUnitCompileInputInvalid("work order fields/schema invalid")
    digest = _sha({key: value for key, value in order.items() if key not in {"work_order_id", "work_order_digest"}})
    if order["work_order_digest"] != digest or order["work_order_id"] != "wo_" + digest:
        raise WorkUnitCompileInputInvalid("work order digest identity invalid")
    if not isinstance(order["objective"], str) or not order["objective"] or not order["acceptance_criteria"]:
        raise WorkUnitCompileInputInvalid("work order objective/acceptance criteria missing")
    if not order["scope"]["allowed_paths"] or set(order["scope"]["allowed_paths"]) & set(order["scope"]["forbidden_paths"]):
        raise WorkUnitCompileInputInvalid("work order scope invalid")
    verification = order["verification"]
    if not (verification["commands"] or verification["manual_checks"] or verification["discover_when_missing"]):
        raise WorkUnitCompileInputInvalid("work order verification method missing")
    if not order["backlog_provenance"] or not order["expected_evidence"] or order["git_policy"] != {"commit": False, "push": False}:
        raise WorkUnitCompileInputInvalid("work order provenance/evidence/authority invalid")


def compile_work_order(campaign, reviewed, review_receipt, snapshot, items, coverage, plan_wu, protected_dirty_paths):
    mapped_ids = sorted(entry["backlog_item_id"] for entry in coverage if plan_wu["id"] in entry["work_unit_ids"])
    if not mapped_ids or any(item_id not in items for item_id in mapped_ids):
        raise WorkUnitCompileInputInvalid(f"plan WU {plan_wu['id']} lacks exact backlog provenance")
    provenance = [{
        "snapshot_record_id": snapshot["snapshot_record_id"],
        "backlog_item_id": item_id, "item_digest": items[item_id]["item_digest"],
        "source_adapter": items[item_id]["source_adapter"],
        "source_entity_id": items[item_id]["source_entity_id"],
    } for item_id in mapped_ids]
    order = {
        "schema": WORK_ORDER_SCHEMA, "work_order_id": "", "work_order_digest": "",
        "campaign_id": campaign["id"], "work_unit_id": plan_wu["id"],
        "plan_provenance": {
            "plan_revision_id": reviewed["plan_revision_id"], "reviewed_record_id": reviewed["record_id"],
            "plan_digest": reviewed["plan_digest"], "record_digest": reviewed["record_digest"],
            "review_digest": review_receipt["review_digest"],
        },
        "backlog_provenance": provenance, "objective": plan_wu["objective"],
        "non_goals": plan_wu["non_goals"], "dependencies": plan_wu["dependencies"],
        "repository_root": campaign["repository_root"], "base_sha": reviewed["base_sha"],
        "scope": {
            "allowed_paths": plan_wu["scope"]["allowed_paths"],
            "forbidden_paths": plan_wu["scope"]["forbidden_paths"],
            "preexisting_dirty_paths": sorted(set(protected_dirty_paths)),
        },
        "acceptance_criteria": plan_wu["acceptance_criteria"], "verification": plan_wu["verification"],
        "risk_class": plan_wu["risk_class"], "required_route": plan_wu["required_route"],
        "human_gates": plan_wu["human_gates"], "expected_evidence": plan_wu["required_evidence"],
        "terminal_gate": plan_wu["terminal_gate"], "git_policy": {"commit": False, "push": False},
        "no_op_allowed": plan_wu.get("no_op_allowed", False),
    }
    order["work_order_digest"] = _sha({key: value for key, value in order.items() if key not in {"work_order_id", "work_order_digest"}})
    order["work_order_id"] = "wo_" + order["work_order_digest"]
    validate_work_order(order)
    return order


def compile_reviewed_plan(campaign_dir, campaign, expected_campaign_revision, plan_revision_id, protected_dirty_paths=None):
    if not isinstance(campaign, dict) or campaign.get("id") is None or campaign.get("revision") != expected_campaign_revision:
        raise WorkUnitCompileStale("campaign revision does not match expected revision")
    reviewed = _reviewed_head_read_only(campaign_dir, campaign["id"], plan_revision_id)
    if reviewed["lifecycle_status"] != "reviewed":
        raise WorkUnitCompileInputInvalid("plan compiler requires the current reviewed plan head")
    review_receipt = _review_receipt(campaign_dir, campaign["id"], reviewed)
    request, result = _planning_result(campaign_dir, campaign["id"], reviewed)
    snapshot, items = _backlog_items(campaign_dir, campaign["id"], request)
    orders = [compile_work_order(
        campaign, reviewed, review_receipt, snapshot, items, result["coverage_matrix"], work_unit,
        protected_dirty_paths or [],
    ) for work_unit in reviewed["work_units"]]
    campaign_specs = [{
        "id": order["work_unit_id"], "objective": order["objective"],
        "dependencies": order["dependencies"], "scope": order["scope"]["allowed_paths"],
        "acceptance_criteria": order["acceptance_criteria"],
        "verification_commands": order["verification"]["commands"],
         "risk_class": order["risk_class"], "required_route": order["required_route"],
         "no_op_allowed": order["no_op_allowed"],
         "work_order": order,
    } for order in orders]
    traceability = [{
        "backlog_item_id": item_id,
        "work_unit_ids": sorted(entry["work_unit_ids"]),
    } for entry in result["coverage_matrix"] for item_id in [entry["backlog_item_id"]]]
    payload = {
        "campaign_id": campaign["id"], "plan_revision_id": reviewed["plan_revision_id"],
        "reviewed_record_id": reviewed["record_id"], "review_digest": review_receipt["review_digest"],
        "work_orders": orders, "campaign_work_units": campaign_specs,
        "edges": reviewed["edges"], "traceability_matrix": traceability,
    }
    return {
        "schema": COMPILATION_SCHEMA, "status": "PREVIEW", "expected_campaign_revision": expected_campaign_revision,
        **payload, "compilation_digest": _sha(payload),
    }
