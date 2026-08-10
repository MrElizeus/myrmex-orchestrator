#!/usr/bin/env python3
"""P1-013 typed replan-trigger ledger, evidence, replay, and reconstruction."""
from __future__ import annotations

import copy
import json
import os
import pathlib
import runpy
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN = ROOT / "bin/myrmex-campaign"
sys.path.insert(0, str(ROOT / "scripts"))
import myrmex_plan_revision as activation  # noqa: E402
import myrmex_plan_store as plan_store  # noqa: E402
import myrmex_replanning as replanning  # noqa: E402


def run_campaign(args, state_home, ok=True):
    env = dict(os.environ, XDG_STATE_HOME=str(state_home), PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run([sys.executable, str(BIN), *args], capture_output=True, text=True, env=env)
    if ok and proc.returncode != 0:
        raise AssertionError(proc.stdout + proc.stderr)
    if not ok and proc.returncode == 0:
        raise AssertionError("campaign command unexpectedly succeeded: " + " ".join(args))
    return proc


activation_fixtures = runpy.run_path(str(ROOT / "tests/test-plan-activation.py"))
prepare = activation_fixtures["prepare"]
make_authority = activation_fixtures["make_authority"]
make_decision = activation_fixtures["make_decision"]
ACTIVATED_AT = activation_fixtures["ACTIVATED_AT"]


with tempfile.TemporaryDirectory(prefix="myrmex-p1013-") as td:
    root = pathlib.Path(td)
    ctx = prepare(root, "triggerledger")
    reviewed = plan_store.get_plan_head(ctx["campaign_dir"], ctx["campaign_id"], ctx["proposed"]["plan_revision_id"])
    receipt = activation.activate_plan(
        ctx["campaign_dir"], ctx["campaign_id"], 2, "activation-trigger-ledger",
        reviewed["plan_revision_id"], reviewed["record_id"], ctx["review"]["review_digest"],
        ctx["dag"], make_authority(reviewed["plan_revision_id"]),
        [make_decision(reviewed["plan_revision_id"])], ACTIVATED_AT,
    )
    active_before = plan_store.get_plan_chain(ctx["campaign_dir"], ctx["campaign_id"], reviewed["plan_revision_id"])
    campaign_file = ctx["campaign_dir"] / "campaign.json"
    events_file = ctx["campaign_dir"] / "events.jsonl"
    campaign_before, events_before = campaign_file.read_bytes(), events_file.read_bytes()
    plan_bytes_before = {
        path.name: path.read_bytes() for path in (ctx["campaign_dir"] / "intelligence/artifacts").glob("*.json")
        if "plan-revision/record/" in path.read_text(encoding="utf-8")
    }

    def put_evidence(name):
        payload = {"schema": "myrmex.test-replan-evidence/v1", "campaign_id": ctx["campaign_id"], "event": name}
        artifact_id = f"replan-evidence/{name}/" + replanning._sha(payload)
        activation.intel.put_artifact(ctx["campaign_dir"], ctx["campaign_id"], 2, "decision", artifact_id, payload)
        return replanning.evidence_reference(ctx["campaign_dir"], ctx["campaign_id"], artifact_id)

    evidence = {trigger_type: put_evidence(trigger_type) for trigger_type in sorted(replanning.TRIGGER_TYPES)}
    source_extra = put_evidence("source-extra")
    records = {}
    for trigger_type in sorted(replanning.TRIGGER_TYPES):
        refs = [evidence[trigger_type], source_extra] if trigger_type == "source_change" else [evidence[trigger_type]]
        result = replanning.record_trigger(
            ctx["campaign_dir"], ctx["campaign_id"], 2, trigger_type,
            f"Observed {trigger_type}", refs, "2026-08-10T05:00:00+00:00",
            {"producer": "test-replan-triggers", "event_id": f"event-{trigger_type}"},
        )
        assert result["status"] == "CREATED"
        replanning.validate_trigger(result["trigger"])
        records[trigger_type] = result

    import jsonschema
    schema = json.loads((ROOT / "contracts/replan-trigger-v1.schema.json").read_text(encoding="utf-8"))
    for result in records.values():
        jsonschema.validate(result["trigger"], schema)

    # Reordered evidence canonicalizes to one stable trigger identity and exact
    # duplicate replay does not rewrite any intelligence JSON bytes.
    before_replay = {str(path.relative_to(ctx["campaign_dir"])): path.read_bytes() for path in (ctx["campaign_dir"] / "intelligence").rglob("*.json")}
    source = records["source_change"]["trigger"]
    replay = replanning.record_trigger(
        ctx["campaign_dir"], ctx["campaign_id"], 2, "source_change", source["summary"],
        list(reversed(source["evidence_references"])), source["detected_at"], source["source"],
    )
    after_replay = {str(path.relative_to(ctx["campaign_dir"])): path.read_bytes() for path in (ctx["campaign_dir"] / "intelligence").rglob("*.json")}
    assert replay["status"] == "REUSED" and replay["trigger"] == source and before_replay == after_replay

    # Stale evidence predates activation's campaign revision.
    backlog_identity = next(item["identity"] for item in reviewed["input_digests"] if item["kind"] == "normalized-backlog")
    stale_reference = replanning.evidence_reference(ctx["campaign_dir"], ctx["campaign_id"], backlog_identity)
    try:
        replanning.record_trigger(
            ctx["campaign_dir"], ctx["campaign_id"], 2, "source_change", "stale",
            [stale_reference], "2026-08-10T05:01:00+00:00", {"producer": "test", "event_id": "stale"},
        )
    except replanning.ReplanTriggerStale:
        pass
    else:
        raise AssertionError("stale evidence accepted")

    wrong_campaign = copy.deepcopy(evidence["defect"]); wrong_campaign["campaign_id"] = "camp-wrong-evidence"
    unavailable = copy.deepcopy(evidence["defect"]); unavailable["artifact_id"] = "replan-evidence/missing/" + "a" * 64
    wrong_digest = copy.deepcopy(evidence["defect"]); wrong_digest["payload_digest"] = "f" * 64
    for candidate, expected in (
        (wrong_campaign, replanning.ReplanTriggerInputInvalid),
        (unavailable, replanning.ReplanTriggerInputInvalid),
        (wrong_digest, replanning.ReplanTriggerConflict),
    ):
        try:
            replanning.record_trigger(
                ctx["campaign_dir"], ctx["campaign_id"], 2, "defect", "invalid evidence",
                [candidate], "2026-08-10T05:02:00+00:00", {"producer": "test", "event_id": "invalid"},
            )
        except expected:
            pass
        else:
            raise AssertionError(f"invalid evidence accepted: {expected.__name__}")

    try:
        replanning.record_trigger(
            ctx["campaign_dir"], ctx["campaign_id"], 1, "defect", "stale campaign",
            [evidence["defect"]], "2026-08-10T05:03:00+00:00", {"producer": "test", "event_id": "stale-campaign"},
        )
    except replanning.ReplanTriggerStale:
        pass
    else:
        raise AssertionError("stale campaign revision accepted")

    ledger = replanning.list_triggers(ctx["campaign_dir"], ctx["campaign_id"])
    repeated_ledger = replanning.list_triggers(ctx["campaign_dir"], ctx["campaign_id"])
    assert ledger == repeated_ledger and ledger["trigger_count"] == len(replanning.TRIGGER_TYPES)
    assert {item["trigger_type"] for item in ledger["triggers"]} == replanning.TRIGGER_TYPES
    assert ledger["ledger_digest"] == replanning._sha([[item["trigger_id"], item["trigger_digest"]] for item in ledger["triggers"]])

    # CLI record replay and deterministic ledger reconstruction.
    evidence_file = root / "trigger-evidence.json"
    evidence_file.write_text(json.dumps(list(reversed(source["evidence_references"]))), encoding="utf-8")
    cli_replay = json.loads(run_campaign([
        "replan-trigger-record", ctx["campaign_id"], "--trigger-type", "source_change",
        "--summary", source["summary"], "--evidence-json", str(evidence_file),
        "--detected-at", source["detected_at"], "--source-producer", source["source"]["producer"],
        "--source-event-id", source["source"]["event_id"], "--expect-revision", "2",
    ], ctx["state_home"]).stdout)
    assert cli_replay["status"] == "REUSED" and cli_replay["trigger"] == source
    cli_ledger = json.loads(run_campaign(["replan-trigger-list", ctx["campaign_id"]], ctx["state_home"]).stdout)
    assert cli_ledger == ledger

    # Trigger-only authority leaves campaign, events, active lifecycle records,
    # and every plan artifact byte-identical.
    assert campaign_file.read_bytes() == campaign_before and events_file.read_bytes() == events_before
    assert plan_store.get_plan_chain(ctx["campaign_dir"], ctx["campaign_id"], reviewed["plan_revision_id"]) == active_before
    plan_bytes_after = {
        path.name: path.read_bytes() for path in (ctx["campaign_dir"] / "intelligence/artifacts").glob("*.json")
        if "plan-revision/record/" in path.read_text(encoding="utf-8")
    }
    assert plan_bytes_after == plan_bytes_before and receipt["active_record_id"] == active_before[-1]["record_id"]

    # Envelope-internally-valid trigger corruption blocks reconstruction.
    corrupt_id = records["defect"]["artifact_id"]
    corrupt_path = ctx["campaign_dir"] / "intelligence/artifacts" / (activation.intel.artifact_storage_key(corrupt_id) + ".json")
    envelope = json.loads(corrupt_path.read_text(encoding="utf-8"))
    envelope["payload"]["summary"] = "corrupt but envelope-valid"
    envelope["payload_digest"] = activation.intel.compute_payload_digest(envelope["payload"])
    envelope["artifact_digest"] = activation.intel.compute_artifact_digest(
        ctx["campaign_id"], envelope["kind"], envelope["artifact_id"], envelope["payload"],
    )
    corrupt_path.write_text(json.dumps(envelope, indent=2) + "\n", encoding="utf-8")
    corrupt_list = run_campaign(["replan-trigger-list", ctx["campaign_id"]], ctx["state_home"], ok=False)
    assert any(term in json.loads(corrupt_list.stdout)["error"] for term in ("digest identity", "cross-checked", "unavailable"))

print("replan triggers: typed evidence, stable digest, idempotent replay, stale/corrupt rejection, reconstruction PASS")
