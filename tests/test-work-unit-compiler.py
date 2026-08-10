#!/usr/bin/env python3
"""P1-010 reviewed plan compiler preview/apply, CAS, and compatibility tests."""
from __future__ import annotations

import copy
import json
import os
import pathlib
import runpy
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN = ROOT / "bin/myrmex-campaign"
sys.path.insert(0, str(ROOT / "scripts"))
import myrmex_plan_critic as critic  # noqa: E402
import myrmex_work_unit_compiler as compiler  # noqa: E402


def run_campaign(args, state_home, ok=True):
    env = dict(os.environ, XDG_STATE_HOME=str(state_home), PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run([sys.executable, str(BIN), *args], capture_output=True, text=True, env=env)
    if ok and proc.returncode != 0:
        raise AssertionError(proc.stdout + proc.stderr)
    if not ok and proc.returncode == 0:
        raise AssertionError("command unexpectedly succeeded: " + " ".join(args))
    return proc


# Reuse the already-adversarial P1-009 fixture builders rather than maintaining
# a second divergent planner/critic setup. runpy executes that standalone test
# first, so a broken dependency fails this compiler gate as well.
critic_fixtures = runpy.run_path(str(ROOT / "tests/test-plan-critic.py"))
fixture = critic_fixtures["fixture"]
make_review = critic_fixtures["make_review"]
source_root, campaign_id, planning_request_id, planner_task_id, proposed = fixture("compiler")

campaign = {
    "id": campaign_id, "revision": 1, "repository_root": "/repo",
}
try:
    compiler.compile_reviewed_plan(source_root, campaign, 1, proposed["plan_revision_id"])
except compiler.WorkUnitCompileInputInvalid as error:
    assert "reviewed" in str(error)
else:
    raise AssertionError("proposed plan compiled before critic PASS")

review_request_id = "review-compiler-001"
critic_task_id = "task-critic-compiler"
prepared = critic.prepare_critic_task(
    source_root, campaign_id, 1, review_request_id, critic_task_id,
    planning_request_id, planner_task_id, proposed["record_id"],
    "2026-08-10T02:00:00+00:00",
)
review = make_review(prepared["task_intent"])
review["created_at"] = "2026-08-10T02:00:00+00:00"
review["review_digest"] = critic._sha({key: value for key, value in review.items() if key != "review_digest"})
critic.record_review(source_root, campaign_id, 1, review_request_id, critic_task_id, review)

with tempfile.TemporaryDirectory(prefix="myrmex-compiler-state-") as state_dir, \
     tempfile.TemporaryDirectory(prefix="myrmex-compiler-repo-") as repo_dir:
    init = run_campaign([
        "init", "--id", campaign_id, "--title", "Compiler Test",
        "--objective", "Compile reviewed plan", "--repo-root", repo_dir,
    ], state_dir)
    assert json.loads(init.stdout)["status"] == "active"
    campaign_path = pathlib.Path(state_dir) / "myrmex/campaigns" / campaign_id
    shutil.copytree(source_root / "intelligence", campaign_path / "intelligence", dirs_exist_ok=True)
    campaign_file = campaign_path / "campaign.json"
    events_file = campaign_path / "events.jsonl"
    before_campaign = campaign_file.read_bytes()
    before_events = events_file.read_bytes()
    before_tree = {str(path.relative_to(campaign_path)): path.read_bytes() for path in campaign_path.rglob("*") if path.is_file()}

    preview = run_campaign([
        "plan-compile-preview", campaign_id, "--plan-revision-id", proposed["plan_revision_id"],
        "--expect-revision", "1", "--protected-dirty-path", "protected.txt",
    ], state_dir)
    preview_data = json.loads(preview.stdout)
    assert preview_data["status"] == "PREVIEW"
    assert campaign_file.read_bytes() == before_campaign and events_file.read_bytes() == before_events
    after_tree = {str(path.relative_to(campaign_path)): path.read_bytes() for path in campaign_path.rglob("*") if path.is_file()}
    assert after_tree == before_tree, "preview must be byte-for-byte read-only"
    assert len(preview_data["work_orders"]) == 1
    order = preview_data["work_orders"][0]
    assert order["plan_provenance"]["plan_revision_id"] == proposed["plan_revision_id"]
    assert order["plan_provenance"]["review_digest"] == review["review_digest"]
    assert order["backlog_provenance"] and order["scope"]["preexisting_dirty_paths"] == ["protected.txt"]
    assert order["objective"] and order["non_goals"] and order["acceptance_criteria"]
    assert order["verification"]["commands"] and order["risk_class"] == "bounded"
    assert order["required_route"] == "direct-only" and order["expected_evidence"] and order["terminal_gate"]
    compiler.validate_work_order(order)

    stale = run_campaign([
        "plan-compile-apply", campaign_id, "--plan-revision-id", proposed["plan_revision_id"],
        "--expect-revision", "0",
    ], state_dir, ok=False)
    assert "revision" in stale.stdout and campaign_file.read_bytes() == before_campaign

    applied = run_campaign([
        "plan-compile-apply", campaign_id, "--plan-revision-id", proposed["plan_revision_id"],
        "--expect-revision", "1", "--protected-dirty-path", "protected.txt",
    ], state_dir)
    applied_data = json.loads(applied.stdout)
    assert applied_data["status"] == "APPLIED" and applied_data["campaign_revision_after"] == 2
    state = json.loads(campaign_file.read_text(encoding="utf-8"))
    assert state["revision"] == 2 and len(state["work_units"]) == 1
    assert state["work_units"][0]["work_order"] == order
    assert applied_data["compilation_digest"] == preview_data["compilation_digest"]

    repeated_preview = json.loads(run_campaign([
        "plan-compile-preview", campaign_id, "--plan-revision-id", proposed["plan_revision_id"],
        "--expect-revision", "2", "--protected-dirty-path", "protected.txt",
    ], state_dir).stdout)
    assert repeated_preview["compilation_digest"] == preview_data["compilation_digest"]
    before_reuse = campaign_file.read_bytes()
    reused = json.loads(run_campaign([
        "plan-compile-apply", campaign_id, "--plan-revision-id", proposed["plan_revision_id"],
        "--expect-revision", "2", "--protected-dirty-path", "protected.txt",
    ], state_dir).stdout)
    assert reused["status"] == "REUSED" and campaign_file.read_bytes() == before_reuse

    # Legacy campaign-v1 WUs remain valid and need no work_order field.
    legacy = run_campaign([
        "wu-add", campaign_id, "--wu-id", "WU-LEGACY", "--objective", "Legacy WU",
    ], state_dir)
    assert json.loads(legacy.stdout)["wu_id"] == "WU-LEGACY"
    legacy_wu = json.loads(campaign_file.read_text(encoding="utf-8"))["work_units"][-1]
    assert "work_order" not in legacy_wu

    # Invalid paths and missing acceptance criteria reject even with refreshed
    # digest identities, proving semantic—not only hash—validation.
    def refresh(candidate):
        candidate["work_order_digest"] = compiler._sha({key: value for key, value in candidate.items() if key not in {"work_order_id", "work_order_digest"}})
        candidate["work_order_id"] = "wo_" + candidate["work_order_digest"]

    missing_criteria = copy.deepcopy(order); missing_criteria["acceptance_criteria"] = []; refresh(missing_criteria)
    invalid_paths = copy.deepcopy(order); invalid_paths["scope"]["forbidden_paths"] = list(invalid_paths["scope"]["allowed_paths"]); refresh(invalid_paths)
    for candidate in (missing_criteria, invalid_paths):
        try:
            compiler.validate_work_order(candidate)
        except compiler.WorkUnitCompileInputInvalid:
            pass
        else:
            raise AssertionError("invalid compiled work order accepted")

# A missing projection is a read-only preview failure, never an implicit repair.
projection = source_root / "intelligence/projection.json"
projection.unlink()
try:
    compiler.compile_reviewed_plan(source_root, campaign, 1, proposed["plan_revision_id"])
except compiler.WorkUnitCompileInputInvalid as error:
    assert "will not repair" in str(error)
else:
    raise AssertionError("preview accepted missing projection")
assert not projection.exists(), "preview repaired projection state"

print("work unit compiler: golden preview/apply, provenance, CAS, replay, and legacy compatibility PASS")
