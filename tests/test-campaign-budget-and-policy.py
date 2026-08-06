#!/usr/bin/env python3
"""Tests for Myrmex campaign budgets, typed blockers, and policy lifecycle."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BIN_CAMPAIGN = ROOT / "bin/myrmex-campaign"


def run_campaign(args: list[str], state_home: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, XDG_STATE_HOME=state_home, PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run(
        [sys.executable, str(BIN_CAMPAIGN), *args],
        capture_output=True,
        text=True,
        env=env,
    )


def test_typed_blockers() -> None:
    with tempfile.TemporaryDirectory(prefix="myrmex-camp-budget-") as td:
        run_campaign(["init", "--id", "camp-blockers", "--title", "Blocker Test"], td)
        run_campaign(["wu-add", "camp-blockers", "--wu-id", "WU-100", "--objective", "Needs Human Decision"], td)

        # Add typed blocker
        proc_blk = run_campaign([
            "blocker-add", "camp-blockers",
            "--type", "human_decision_required",
            "--message", "Architecture decision on database engine required",
            "--wu-id", "WU-100",
            "--context", json.dumps({"options": ["sqlite", "postgres"]}),
        ], td)
        assert proc_blk.returncode == 0, f"blocker-add failed: {proc_blk.stderr}"

        # Show campaign
        proc_show = run_campaign(["show", "camp-blockers", "--json"], td)
        data = json.loads(proc_show.stdout)
        assert len(data["blockers"]) == 1
        assert data["blockers"][0]["type"] == "human_decision_required"
        assert data["work_units"][0]["status"] == "blocked"
        assert data["work_units"][0]["phase"] == "blocked"
        assert "blocked" in data["next_action"].lower()


def test_pause_resume_cancel() -> None:
    with tempfile.TemporaryDirectory(prefix="myrmex-camp-budget-") as td:
        run_campaign(["init", "--id", "camp-control", "--title", "Control Test"], td)

        # Pause
        proc_p = run_campaign(["pause", "camp-control", "--reason", "Maintenance window"], td)
        assert proc_p.returncode == 0, f"pause failed: {proc_p.stderr}"
        data_p = json.loads(run_campaign(["show", "camp-control", "--json"], td).stdout)
        assert data_p["status"] == "paused"
        assert data_p["pause_reason"] == "Maintenance window"

        # Resume
        proc_r = run_campaign(["resume", "camp-control"], td)
        assert proc_r.returncode == 0
        data_r = json.loads(run_campaign(["show", "camp-control", "--json"], td).stdout)
        assert data_r["status"] == "active"
        assert data_r["pause_reason"] is None

        # Cancel
        proc_c = run_campaign(["cancel", "camp-control"], td)
        assert proc_c.returncode == 0
        data_c = json.loads(run_campaign(["show", "camp-control", "--json"], td).stdout)
        assert data_c["status"] == "cancelled"


def test_consecutive_failures_reconciliation() -> None:
    with tempfile.TemporaryDirectory(prefix="myrmex-camp-budget-") as td:
        # Initialize campaign with max 2 consecutive failures
        run_campaign(["init", "--id", "camp-failures", "--title", "Failure Budget", "--max-consecutive-failures", "2"], td)
        run_campaign(["wu-add", "camp-failures", "--wu-id", "WU-01", "--objective", "Failing WU 1"], td)
        run_campaign(["wu-add", "camp-failures", "--wu-id", "WU-02", "--objective", "Failing WU 2"], td)

        # Fail WU 1
        run_campaign(["wu-transition", "camp-failures", "WU-01", "--phase", "failed", "--status", "failed", "--force"], td)
        # Fail WU 2
        run_campaign(["wu-transition", "camp-failures", "WU-02", "--phase", "failed", "--status", "failed", "--force"], td)

        # Reconcile campaign -> should trigger auto-pause due to failure budget
        proc_rec = run_campaign(["reconcile", "camp-failures"], td)
        assert proc_rec.returncode == 0
        rec = json.loads(proc_rec.stdout)
        assert rec["status"] == "paused"

        data = json.loads(run_campaign(["show", "camp-failures", "--json"], td).stdout)
        assert data["status"] == "paused"
        assert "consecutive failures" in data["pause_reason"].lower()


def main() -> int:
    test_typed_blockers()
    test_pause_resume_cancel()
    test_consecutive_failures_reconciliation()
    print("campaign budget and policy test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
