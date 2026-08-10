#!/usr/bin/env python3
"""P1-018 state-first, revision-aware sequential scheduler integration."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN_CAMPAIGN = ROOT / "bin/myrmex-campaign"
BIN_HEAD = ROOT / "bin/myrmex-head"
loader = importlib.machinery.SourceFileLoader("myrmex_head_p1018", str(BIN_HEAD))
spec = importlib.util.spec_from_loader(loader.name, loader)
head_runtime = importlib.util.module_from_spec(spec)
loader.exec_module(head_runtime)
sys.path.insert(0, str(ROOT / "scripts"))
import myrmex_campaign_intelligence as intel  # noqa: E402

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
PLAN_ID = "plan_" + DIGEST_A
HEAD = {"plan_revision_id": PLAN_ID, "plan_digest": DIGEST_A, "record_id": "planrec_" + DIGEST_B}
ACTIVATION = {"activation_id": "activation_" + DIGEST_A, "plan_revision_id": PLAN_ID, "active_record_id": HEAD["record_id"]}


def run_campaign(args, state_home, ok=True):
    env = dict(os.environ, XDG_STATE_HOME=str(state_home), PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run([sys.executable, str(BIN_CAMPAIGN), *args], capture_output=True, text=True, env=env)
    if ok and proc.returncode != 0:
        raise AssertionError(proc.stdout + proc.stderr)
    if not ok and proc.returncode == 0:
        raise AssertionError("campaign command unexpectedly succeeded: " + " ".join(args))
    return proc


def policy_files(root):
    policy = {
        "schema": "myrmex.route-model-policy/v1", "version": "1.0.0", "adaptive_scoring": False,
        "allowed_routes": ["delegated"], "allowed_provider_prefixes": ["fixture/"],
        "allowed_models": ["fixture/local"],
        "role_compatibility": [{"role": "worker", "agents": ["myrmex-worker"], "routes": ["delegated"]}],
        "selection_order": ["priority_asc", "option_id_asc"],
        "options": [{
            "option_id": "fixture-worker", "priority": 10, "route": "delegated",
            "agent": "myrmex-worker", "role": "worker", "provider": "fixture",
            "model": "fixture/local", "max_estimated_cost_usd": 0,
        }],
    }
    availability = {
        "schema": "myrmex.route-model-availability/v1",
        "agents": {"myrmex-worker": True}, "providers": {"fixture": True}, "models": {"fixture/local": True},
    }
    policy_path = root / "route-policy.json"; policy_path.write_text(json.dumps(policy), encoding="utf-8")
    availability_path = root / "route-availability.json"; availability_path.write_text(json.dumps(availability), encoding="utf-8")
    return policy_path, availability_path


def fixture(root, suffix):
    state = root / ("state-" + suffix); repo = root / ("repo-" + suffix); repo.mkdir()
    cid = "camp-scheduler-" + suffix
    run_campaign([
        "init", "--id", cid, "--title", "Sequential scheduler", "--repo-root", str(repo),
        "--driver-policy", "fixture", "--execution-driver", "fixture-command",
    ], state)
    # Source order is deliberately WU-B then WU-A. Legacy FIFO would choose B;
    # deterministic P1 priority must choose stable ID WU-A.
    for wu_id in ("WU-B", "WU-A"):
        run_campaign([
            "wu-add", cid, "--wu-id", wu_id, "--objective", wu_id,
            "--required-route", "delegated", "--implementation-cmd", "true", "--verify-cmd", "true",
        ], state)
    campaign_dir = state / "myrmex/campaigns" / cid
    path = campaign_dir / "campaign.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for wu in data["work_units"]:
        order_body = {
            "schema": "myrmex.work-order/v2", "campaign_id": cid, "work_unit_id": wu["id"],
            "plan_provenance": {"plan_revision_id": PLAN_ID, "plan_digest": DIGEST_A},
            "human_gates": [], "required_route": "delegated",
        }
        order_digest = head_runtime.canonical_sha(order_body)
        wu["work_order"] = {**order_body, "work_order_id": "wo_" + order_digest, "work_order_digest": order_digest}
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return state, cid, campaign_dir


def supervisor(state, cid, policy_path, availability_path, *, allow_fixture=True):
    instance = head_runtime.CampaignSupervisor(
        campaign_id=cid, once=True, state_home=str(state), allow_fixture_driver=allow_fixture,
        route_model_policy_path=str(policy_path), route_model_availability_path=str(availability_path),
    )
    instance._active_plan = lambda _cid: (dict(HEAD), dict(ACTIVATION))
    return instance


with tempfile.TemporaryDirectory(prefix="myrmex-p1018-") as td:
    root = pathlib.Path(td)
    policy_path, availability_path = policy_files(root)
    state, cid, campaign_dir = fixture(root, "main")
    sup = supervisor(state, cid, policy_path, availability_path)
    data = sup.get_campaign_data(cid)

    # A crash before dispatch can replay exact decisions without duplicate
    # artifacts. The selected WU differs from legacy source-order FIFO.
    first = sup._prepare_p1_dispatch(cid, data)
    second = sup._prepare_p1_dispatch(cid, data)
    assert first["intent"] == second["intent"]
    assert first["schedule"]["selected_work_unit_id"] == "WU-A"
    assert first["route_decision"]["selected"]["model"] == "fixture/local"
    listing = intel.list_artifacts(campaign_dir, cid, kind="decision")
    ids = [row["artifact_id"] for row in listing["artifacts"]["decision"]]
    assert len(ids) == 3
    assert any(item.startswith("scheduling-decision/") for item in ids)
    assert any(item.startswith("route-model-decision/") for item in ids)
    assert any(item.startswith("sequential-dispatch/intent/") for item in ids)
    intent = first["intent"]
    assert intent["scheduling_decision_digest"] == first["schedule"]["decision_digest"]
    assert intent["route_model_decision_digest"] == first["route_decision"]["decision_digest"]
    assert intent["plan_revision_id"] == PLAN_ID and intent["work_unit_id"] == "WU-A"

    # The run binding is a task-intent correlation attached to both decisions;
    # it remains non-authorizing by itself.
    sup._attach_dispatch_run(cid, first, "run-fixture-001")
    sup._attach_dispatch_run(cid, first, "run-fixture-001")
    listing = intel.list_artifacts(campaign_dir, cid, kind="decision")
    binding_ids = [row["artifact_id"] for row in listing["artifacts"]["decision"] if row["artifact_id"].startswith("sequential-dispatch/run/")]
    assert len(binding_ids) == 1
    binding_id = binding_ids[0]
    binding = intel.get_artifact(campaign_dir, cid, binding_id)["artifact"]["payload"]
    assert binding["run_id"] == "run-fixture-001"
    assert binding["authority"]["dispatch"] is False

    # Process-level dispatch sees all state-first artifacts before entering the
    # existing P0 execution driver. It dispatches exactly one WU.
    state2, cid2, campaign_dir2 = fixture(root, "process")
    sup2 = supervisor(state2, cid2, policy_path, availability_path)
    captured = []
    def fake_run(cid_value, data_value, wu_value, dispatch_value=None):
        persisted = intel.list_artifacts(campaign_dir2, cid2, kind="decision")
        persisted_ids = [row["artifact_id"] for row in persisted["artifacts"]["decision"]]
        assert any(item.startswith("scheduling-decision/") for item in persisted_ids)
        assert any(item.startswith("sequential-dispatch/intent/") for item in persisted_ids)
        captured.append((wu_value["id"], dispatch_value["intent"]["dispatch_id"]))
        return True
    sup2.run_work_unit = fake_run
    assert sup2.process_campaign(cid2) is True
    assert captured == [("WU-A", captured[0][1])]

    # A changed ready set after intent persistence makes the decision stale and
    # never reaches the execution driver.
    state3, cid3, _ = fixture(root, "stale")
    sup3 = supervisor(state3, cid3, policy_path, availability_path)
    original_revalidate = sup3._revalidate_p1_dispatch
    def mutate_then_revalidate(cid_value, context):
        run_campaign(["wu-transition", cid_value, "WU-A", "--phase", "collecting-context", "--status", "active"], state3)
        return original_revalidate(cid_value, context)
    sup3._revalidate_p1_dispatch = mutate_then_revalidate
    try:
        sup3._prepare_p1_dispatch(cid3, sup3.get_campaign_data(cid3))
    except head_runtime.SequentialSchedulerStale as error:
        assert "changed" in str(error) or "already dispatched" in str(error)
    else:
        raise AssertionError("stale scheduling decision reached dispatch")

    # Active-plan revision is checked again after persistence; a superseding
    # activation invalidates the dispatch before the driver boundary.
    state_plan, cid_plan, _ = fixture(root, "stale-plan")
    sup_plan = supervisor(state_plan, cid_plan, policy_path, availability_path)
    def changing_active_plan(_cid):
        changing_active_plan.calls += 1
        if changing_active_plan.calls == 1:
            return dict(HEAD), dict(ACTIVATION)
        changed_head = {**HEAD, "record_id": "planrec_" + DIGEST_A}
        changed_receipt = {**ACTIVATION, "active_record_id": changed_head["record_id"], "activation_id": "activation_" + DIGEST_B}
        return changed_head, changed_receipt
    changing_active_plan.calls = 0
    sup_plan._active_plan = changing_active_plan
    try:
        sup_plan._prepare_p1_dispatch(cid_plan, sup_plan.get_campaign_data(cid_plan))
    except head_runtime.SequentialSchedulerStale as error:
        assert "active plan changed" in str(error)
    else:
        raise AssertionError("stale active plan revision reached dispatch")

    # Restart after scheduling/initial dispatch recovers the exact persisted
    # scheduling, route/model, plan, WU, and driver binding without a new FIFO choice.
    run_campaign(["wu-transition", cid, "WU-A", "--phase", "collecting-context", "--status", "active"], state)
    restarted = supervisor(state, cid, policy_path, availability_path)
    active_data = restarted.get_campaign_data(cid)
    active_wu = next(wu for wu in active_data["work_units"] if wu["id"] == "WU-A")
    recovered = restarted._resume_p1_dispatch(cid, active_data, active_wu)
    assert recovered["intent"]["dispatch_id"] == first["intent"]["dispatch_id"]
    before_ids = [row["artifact_id"] for row in intel.list_artifacts(campaign_dir, cid, kind="decision")["artifacts"]["decision"]]
    restarted.run_work_unit = lambda _cid, _data, wu, dispatch=None: captured.append((wu["id"], dispatch["intent"]["dispatch_id"])) or True
    assert restarted.process_campaign(cid) is True
    after_ids = [row["artifact_id"] for row in intel.list_artifacts(campaign_dir, cid, kind="decision")["artifacts"]["decision"]]
    assert after_ids == before_ids, "already-dispatched recovery created a new scheduling decision"

    # Fixture and production policy are intentionally distinct. A fixture model
    # cannot be dispatched by the production OpenCode driver.
    production_data = json.loads(json.dumps(data))
    production_data["driver_policy"] = "production"; production_data["execution_driver"] = "opencode-task"
    production_wu = next(wu for wu in production_data["work_units"] if wu["id"] == "WU-A")
    production_sup = supervisor(state, cid, policy_path, availability_path, allow_fixture=False)
    try:
        production_sup._check_driver_decision(production_data, production_wu, first["route_decision"])
    except head_runtime.SequentialSchedulerError as error:
        assert "production" in str(error)
    else:
        raise AssertionError("fixture route/model decision entered production driver")

    # P1 never falls back when explicit route/model inputs are missing.
    state4, cid4, _ = fixture(root, "missing-policy")
    no_policy = head_runtime.CampaignSupervisor(campaign_id=cid4, once=True, state_home=str(state4), allow_fixture_driver=True)
    no_policy._active_plan = lambda _cid: (dict(HEAD), dict(ACTIVATION))
    try:
        no_policy._prepare_p1_dispatch(cid4, no_policy.get_campaign_data(cid4))
    except head_runtime.SequentialSchedulerError as error:
        assert "explicit route/model policy" in str(error)
    else:
        raise AssertionError("P1 silently fell back without route/model policy")

print("sequential scheduler: state-first decisions, active-plan/readiness checks, one WU, stale/restart recovery, attachment, and no FIFO fallback PASS")
