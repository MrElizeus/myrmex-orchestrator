#!/usr/bin/env python3
"""P1-015 deterministic eligibility, scoring, age, ties, and preview replay."""
from __future__ import annotations

import copy
import json
import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN = ROOT / "bin/myrmex-campaign"
sys.path.insert(0, str(ROOT / "scripts"))
import myrmex_priority_policy as priority  # noqa: E402


def run_campaign(args, state_home, ok=True):
    env = dict(os.environ, XDG_STATE_HOME=str(state_home), PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run([sys.executable, str(BIN), *args], capture_output=True, text=True, env=env)
    if ok and proc.returncode != 0:
        raise AssertionError(proc.stdout + proc.stderr)
    if not ok and proc.returncode == 0:
        raise AssertionError("campaign command unexpectedly succeeded: " + " ".join(args))
    return proc


def tree_bytes(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def work_unit(wu_id, *, dependencies=(), status="pending", created_at="2026-08-10T10:00:00+00:00", corrections_used=0, corrections_budget=3, human_gate=None):
    result = {
        "id": wu_id, "created_at": created_at, "status": status,
        "phase": "completed" if status == "completed" else "ready",
        "dependencies": list(dependencies), "corrections_used": corrections_used,
        "corrections_budget": corrections_budget, "blocker": None,
        "required_route": "direct-only",
    }
    if human_gate is not None:
        result["work_order"] = {"human_gates": [human_gate]}
    return result


def write_fixture(root, suffix, work_units, *, revision=7, updated_at="2026-08-10T12:00:00+00:00"):
    state_home = root / f"state-{suffix}"
    campaign_id = f"camp-priority-{suffix}"
    run_campaign([
        "init", "--id", campaign_id, "--title", "Priority fixture", "--objective", "Select next WU",
        "--repo-root", str(root),
    ], state_home)
    campaign_dir = state_home / "myrmex/campaigns" / campaign_id
    path = campaign_dir / "campaign.json"
    campaign = json.loads(path.read_text(encoding="utf-8"))
    campaign["revision"] = revision
    campaign["created_at"] = "2026-08-01T00:00:00+00:00"
    campaign["updated_at"] = updated_at
    campaign["work_units"] = copy.deepcopy(work_units)
    campaign["dag"]["edges"] = [[dependency, wu["id"]] for wu in work_units for dependency in wu["dependencies"]]
    campaign["active_work_unit"] = None
    path.write_text(json.dumps(campaign, indent=2) + "\n", encoding="utf-8")
    return state_home, campaign_dir, campaign_id, campaign


with tempfile.TemporaryDirectory(prefix="myrmex-p1015-") as td:
    root = pathlib.Path(td)
    gate = {"gate_id": "gate-ready", "decision_type": "approve", "reason": "operator approval", "required_before": "work_unit_ready"}
    golden_wus = [
        work_unit("WU-DONE", status="completed", created_at="2026-08-02T00:00:00+00:00"),
        work_unit("WU-A", dependencies=["WU-DONE"]),
        work_unit("WU-A2", dependencies=["WU-A"]),
        work_unit("WU-B", dependencies=["WU-DONE"]),
        work_unit("WU-BUDGET", dependencies=["WU-DONE"], corrections_used=1, corrections_budget=1),
        work_unit("WU-GATE", dependencies=["WU-DONE"], human_gate=gate),
    ]
    state_home, campaign_dir, campaign_id, campaign = write_fixture(root, "golden", golden_wus)
    before = tree_bytes(campaign_dir)
    decision = priority.preview_schedule(campaign_dir, campaign_id, 7)
    priority.validate_decision(decision)
    import jsonschema
    jsonschema.validate(decision, json.loads((ROOT / "contracts/scheduling-decision-v1.schema.json").read_text(encoding="utf-8")))
    assert tree_bytes(campaign_dir) == before
    assert decision["policy"]["version"] == "1.0.0" and decision["authority"]["dispatch"] is False
    assert decision["selected_work_unit_id"] == "WU-A"
    assert decision["eligible_work_unit_ids"] == ["WU-A", "WU-B"]
    rows = {row["work_unit_id"]: row for row in decision["considered_candidates"]}
    assert rows["WU-A"]["features"]["critical_path_length"] == 2
    assert rows["WU-A"]["features"]["downstream_unlock_count"] == 1
    assert rows["WU-A"]["score"] > rows["WU-B"]["score"]
    assert rows["WU-A2"]["exclusion_reasons"] == ["dependency_not_completed:WU-A"]
    assert rows["WU-GATE"]["score"] is None and rows["WU-GATE"]["exclusion_reasons"] == ["human_gate_required:gate-ready"]
    assert rows["WU-BUDGET"]["exclusion_reasons"] == ["work_unit_correction_budget_exhausted"]
    assert rows["WU-DONE"]["exclusion_reasons"] == ["status_not_schedulable:completed"]
    assert all(row["eligible"] or row["exclusion_reasons"] for row in rows.values())

    cli = json.loads(run_campaign([
        "schedule-preview", campaign_id, "--expect-revision", "7",
    ], state_home).stdout)
    assert cli == decision and tree_bytes(campaign_dir) == before
    assert priority.preview_schedule(campaign_dir, campaign_id, 7) == decision
    stale = run_campaign(["schedule-preview", campaign_id, "--expect-revision", "6"], state_home, ok=False)
    assert "stale" in json.loads(stale.stdout)["error"]
    tampered = copy.deepcopy(decision)
    tampered["eligible_work_unit_ids"] = list(reversed(tampered["eligible_work_unit_ids"]))
    body = {key: value for key, value in tampered.items() if key not in {"decision_id", "decision_digest"}}
    tampered["decision_digest"] = priority._sha(body); tampered["decision_id"] = "schedule_" + tampered["decision_digest"]
    try:
        priority.validate_decision(tampered)
    except priority.PriorityPolicyInputInvalid:
        pass
    else:
        raise AssertionError("rehashed non-policy ranking was accepted")

    # Age eventually outweighs a short critical-path advantage, preventing an
    # older ready WU from being starved by a newly arrived chain.
    starvation_wus = [
        work_unit("WU-OLD", created_at="2026-08-08T12:00:00+00:00"),
        work_unit("WU-NEW", created_at="2026-08-10T12:00:00+00:00"),
        work_unit("WU-NEW-CHILD", dependencies=["WU-NEW"], created_at="2026-08-10T12:00:00+00:00"),
    ]
    _, starvation_dir, starvation_id, _ = write_fixture(root, "starve", starvation_wus)
    starvation = priority.preview_schedule(starvation_dir, starvation_id, 7)
    starvation_rows = {row["work_unit_id"]: row for row in starvation["considered_candidates"]}
    assert starvation["selected_work_unit_id"] == "WU-OLD"
    assert starvation_rows["WU-OLD"]["features"]["age_minutes"] == 2880
    assert starvation_rows["WU-OLD"]["score"] > starvation_rows["WU-NEW"]["score"]

    # Exact feature/score ties resolve by stable WU ID, independent of source
    # array order in the considered-candidate presentation.
    tie_wus = [work_unit("WU-B"), work_unit("WU-A")]
    _, tie_dir, tie_id, _ = write_fixture(root, "tiecase", tie_wus)
    tied = priority.preview_schedule(tie_dir, tie_id, 7)
    assert tied["selected_work_unit_id"] == "WU-A"
    assert tied["eligible_work_unit_ids"] == ["WU-A", "WU-B"]
    assert [row["work_unit_id"] for row in tied["considered_candidates"]] == ["WU-A", "WU-B"]

    # Campaign-wide budget exhaustion is a hard exclusion before scoring.
    budget_wus = [work_unit("WU-A"), work_unit("WU-B")]
    _, budget_dir, budget_id, budget_campaign = write_fixture(root, "budget", budget_wus)
    budget_campaign["budgets"]["corrections_global_used"] = budget_campaign["budgets"]["corrections_global"]
    (budget_dir / "campaign.json").write_text(json.dumps(budget_campaign, indent=2) + "\n", encoding="utf-8")
    budget = priority.preview_schedule(budget_dir, budget_id, 7)
    assert budget["selected_work_unit_id"] is None and budget["selection_reason"] == "no_eligible_work_unit"
    assert all(row["score"] is None and "campaign_correction_budget_exhausted" in row["exclusion_reasons"] for row in budget["considered_candidates"])

    # Malformed policy and cyclic state fail closed rather than silently
    # selecting a WU through a fallback.
    invalid_policy = priority.default_policy(); invalid_policy["weights"]["age_minutes"] = 2
    try:
        priority.preview_schedule(tie_dir, tie_id, 7, invalid_policy)
    except priority.PriorityPolicyInputInvalid:
        pass
    else:
        raise AssertionError("modified policy was silently accepted")
    cyclic = json.loads((tie_dir / "campaign.json").read_text(encoding="utf-8"))
    cyclic["work_units"][0]["dependencies"] = [cyclic["work_units"][1]["id"]]
    cyclic["work_units"][1]["dependencies"] = [cyclic["work_units"][0]["id"]]
    cyclic["dag"]["edges"] = [["WU-A", "WU-B"], ["WU-B", "WU-A"]]
    (tie_dir / "campaign.json").write_text(json.dumps(cyclic, indent=2) + "\n", encoding="utf-8")
    try:
        priority.preview_schedule(tie_dir, tie_id, 7)
    except priority.PriorityPolicyInputInvalid as error:
        assert "cycle" in str(error)
    else:
        raise AssertionError("cyclic scheduling state was accepted")

    # New WUs receive a durable creation timestamp used by the age feature.
    created_home = root / "state-created"
    created_id = "camp-priority-created"
    run_campaign(["init", "--id", created_id, "--title", "Created timestamp", "--repo-root", str(root)], created_home)
    run_campaign(["wu-add", created_id, "--wu-id", "WU-CREATED", "--objective", "timestamped"], created_home)
    created_state = json.loads(run_campaign(["show", created_id, "--json"], created_home).stdout)
    assert priority._time(created_state["work_units"][0]["created_at"], "created_at")

print("priority policy: hard eligibility, critical path, downstream unlock, starvation age, stable tie, exclusions, replay, and no-dispatch preview PASS")
