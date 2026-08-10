#!/usr/bin/env python3
"""P1-017 reconstructible campaign observations, cost projection, and audit."""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN = ROOT / "bin/myrmex-campaign"
sys.path.insert(0, str(ROOT / "scripts"))
import myrmex_campaign_intelligence as intel  # noqa: E402
import myrmex_campaign_observability as observability  # noqa: E402

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
PLAN_ID = "plan_" + DIGEST_A
BACKLOG_SNAPSHOT = "blsnaprec_" + DIGEST_A
BACKLOG_ITEM = "backlog_" + DIGEST_B
ROUTE_DECISION = "routemodel_" + DIGEST_A
SCHEDULE_DECISION = "schedule_" + DIGEST_B


def run(args, state_home, ok=True):
    env = dict(os.environ, XDG_STATE_HOME=str(state_home), PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run([sys.executable, str(BIN), *args], capture_output=True, text=True, env=env)
    if ok and proc.returncode != 0:
        raise AssertionError(proc.stdout + proc.stderr)
    if not ok and proc.returncode == 0:
        raise AssertionError("command unexpectedly succeeded: " + " ".join(args))
    return proc


def tree_bytes(path):
    return {str(item.relative_to(path)): item.read_bytes() for item in path.rglob("*") if item.is_file()}


with tempfile.TemporaryDirectory(prefix="myrmex-p1017-") as td:
    root = pathlib.Path(td)
    repo = root / "repo"; repo.mkdir()
    state = root / "state"
    cid = "camp-observability"
    run(["init", "--id", cid, "--title", "Observability", "--objective", "Rebuild projections", "--repo-root", str(repo)], state)
    run(["wu-add", cid, "--wu-id", "WU-A", "--objective", "First"], state)
    run(["wu-add", cid, "--wu-id", "WU-B", "--objective", "Second", "--dependencies", "WU-A"], state)
    run(["blocker-add", cid, "--type", "human_decision_required", "--message", "Need an explicit decision"], state)

    campaign_dir = state / "myrmex/campaigns" / cid
    campaign_path = campaign_dir / "campaign.json"
    campaign = json.loads(campaign_path.read_text(encoding="utf-8"))
    campaign["revision"] += 1
    campaign["work_units"][0]["task_ids"] = ["task-worker-001"]
    campaign["work_units"][0]["work_order"] = {
        "plan_provenance": {"plan_revision_id": PLAN_ID},
        "backlog_provenance": [{"snapshot_record_id": BACKLOG_SNAPSHOT, "backlog_item_id": BACKLOG_ITEM}],
    }
    campaign_path.write_text(json.dumps(campaign, indent=2) + "\n", encoding="utf-8")

    # Immutable intelligence artifacts provide backlog, plan, decision, and task
    # correlations independently of their rebuildable projection.
    intel.put_artifact(campaign_dir, cid, campaign["revision"], "backlog", "fixture/backlog", {
        "snapshot_record_id": BACKLOG_SNAPSHOT, "backlog_item_id": BACKLOG_ITEM,
    })
    intel.put_artifact(campaign_dir, cid, campaign["revision"], "plan", "fixture/plan", {
        "plan_revision_id": PLAN_ID, "work_unit_id": "WU-A", "task_id": "task-planner-001",
    })
    intel.put_artifact(campaign_dir, cid, campaign["revision"], "decision", "fixture/decisions", {
        "route_model_decision_id": ROUTE_DECISION, "scheduling_decision_id": SCHEDULE_DECISION,
        "work_unit_id": "WU-A", "task_id": "task-worker-001",
    })
    intelligence_before = tree_bytes(campaign_dir / "intelligence")
    campaign_before = campaign_path.read_bytes()

    recorded = json.loads(run(["observation-record", cid, "--observed-at", "2030-01-01T00:00:00+00:00"], state).stdout)
    observation = recorded["observation"]
    observability.validate_campaign_observation(observation)
    import jsonschema
    jsonschema.validate(observation, json.loads((ROOT / "contracts/campaign-observation-v1.schema.json").read_text(encoding="utf-8")))
    assert observation["source_index"]["events"] and len(observation["source_index"]["artifacts"]) == 3
    assert observation["correlations"]["backlog_record_ids"] == [BACKLOG_ITEM, BACKLOG_SNAPSHOT]
    assert observation["correlations"]["plan_revision_ids"] == [PLAN_ID]
    assert observation["correlations"]["task_ids"] == ["task-planner-001", "task-worker-001"]
    assert observation["correlations"]["decision_ids"] == [ROUTE_DECISION, SCHEDULE_DECISION]
    assert observation["critical_path"] == {"length": 2, "work_unit_ids": ["WU-A", "WU-B"]}
    assert observation["blockers"][0]["age_seconds"] > 0
    assert observation["authority"] == observability.CAMPAIGN_AUTHORITY

    unknown_input = root / "unknown-cost.json"
    unknown_input.write_text(json.dumps({
        "observed_at": "2030-01-01T00:01:00+00:00",
        "correlation": {"plan_revision_id": PLAN_ID, "work_unit_id": "WU-A", "task_id": "task-worker-001", "route_model_decision_id": ROUTE_DECISION},
        "source_kind": "unavailable", "source_digest": None,
        "incurred_usd": None, "incurred_unknown_reason": "provider did not report cost",
        "projected_remaining_usd": None, "remaining_unknown_reason": "future model usage is unknown",
    }), encoding="utf-8")
    unknown = json.loads(run(["cost-observation-record", cid, "--input-json", str(unknown_input)], state).stdout)["observation"]
    observability.validate_cost_observation(unknown)
    jsonschema.validate(unknown, json.loads((ROOT / "contracts/cost-observation-v1.schema.json").read_text(encoding="utf-8")))

    known_input = root / "known-cost.json"
    known_input.write_text(json.dumps({
        "observed_at": "2030-01-01T00:02:00+00:00",
        "correlation": {"plan_revision_id": PLAN_ID, "work_unit_id": "WU-B", "task_id": "task-worker-002", "route_model_decision_id": ROUTE_DECISION},
        "source_kind": "provider_receipt", "source_digest": DIGEST_B,
        "incurred_usd": 2.0, "incurred_unknown_reason": None,
        "projected_remaining_usd": 3.0, "remaining_unknown_reason": None,
    }), encoding="utf-8")
    run(["cost-observation-record", cid, "--input-json", str(known_input)], state)
    status = json.loads(run(["status", cid, "--json"], state).stdout)
    cost = status["projection"]["cost"]
    assert cost["known_incurred_usd"] == 2.0 and cost["known_projected_remaining_usd"] == 3.0
    assert cost["total_incurred_usd"] is None and cost["projected_remaining_usd"] is None
    assert cost["projected_total_usd"] is None, "unknown cost must never be coerced to zero"
    assert status["consistency"]["status"] == "PASS"
    assert status["projection"]["authority"] == observability.PROJECTION_AUTHORITY

    # A physically later but temporally older observation is deterministically
    # ordered without replacing the latest observation.
    older = json.loads(run(["observation-record", cid, "--observed-at", "2029-12-31T23:59:00+00:00"], state).stdout)
    assert older["projection"]["latest_campaign_observation_id"] == observation["observation_id"]
    timeline = json.loads(run(["timeline", cid, "--observability", "--json"], state).stdout)
    assert timeline == sorted(timeline, key=lambda row: (row["observed_at"], row["kind"], row["observation_id"]))

    # Projection deletion loses no authority or source data and rebuilds to the
    # exact same bytes from immutable observations.
    projection_path = campaign_dir / "observability/projection.json"
    projection_before = projection_path.read_bytes()
    projection_path.unlink()
    assert run(["status", cid, "--json"], state, ok=False).returncode == 1
    assert campaign_path.read_bytes() == campaign_before
    assert tree_bytes(campaign_dir / "intelligence") == intelligence_before
    run(["observability-rebuild", cid], state)
    assert projection_path.read_bytes() == projection_before

    # Corrupt projections fail closed but can be replaced from valid records.
    projection_path.write_text('{"corrupt":true}\n', encoding="utf-8")
    failed_audit = json.loads(run(["observability-audit", cid], state, ok=False).stdout)
    assert failed_audit["status"] == "FAIL"
    run(["observability-rebuild", cid], state)
    assert json.loads(run(["observability-audit", cid], state).stdout)["status"] == "PASS"

    # New authoritative events make the observation stale; alteration of an
    # observed event is corruption. Recording a fresh observation restores PASS.
    events_path = campaign_dir / "events.jsonl"
    events_before = events_path.read_bytes()
    with events_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"timestamp": "2030-01-01T00:03:00+00:00", "campaign_id": cid, "event_type": "TEST_PROGRESS", "message": "progress", "payload": {}}) + "\n")
    stale = json.loads(run(["observability-audit", cid], state, ok=False).stdout)
    assert stale["status"] == "STALE"
    run(["observation-record", cid, "--observed-at", "2030-01-01T00:04:00+00:00"], state)
    assert json.loads(run(["observability-audit", cid], state).stdout)["status"] == "PASS"
    fresh_events = events_path.read_bytes()
    lines = fresh_events.decode().splitlines()
    first = json.loads(lines[0]); first["event_type"] = "TAMPERED"
    lines[0] = json.dumps(first)
    events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    corrupt_source = json.loads(run(["observability-audit", cid], state, ok=False).stdout)
    assert corrupt_source["status"] == "FAIL"
    events_path.write_bytes(fresh_events)

    # Observation writes and projection rebuilds never mutate campaign or
    # immutable intelligence authority.
    assert campaign_path.read_bytes() == campaign_before
    assert tree_bytes(campaign_dir / "intelligence") == intelligence_before

print("campaign observability: correlations, event/artifact index, unknown cost, blockers, critical path, replay, rebuild, corruption, and audit PASS")
