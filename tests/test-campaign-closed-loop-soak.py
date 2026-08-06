#!/usr/bin/env python3
"""End-to-end closed-loop soak and fault-injection test for Myrmex campaigns."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BIN_CAMPAIGN = ROOT / "bin/myrmex-campaign"
BIN_HEAD = ROOT / "bin/myrmex-head"


def run_cmd(cmd: list[str], state_home: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, XDG_STATE_HOME=state_home, PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


def test_closed_loop_execution_and_resilience() -> None:
    with tempfile.TemporaryDirectory(prefix="myrmex-soak-") as td:
        # Create campaign
        cid = "camp-soak-flow"
        proc_init = run_cmd([
            sys.executable, str(BIN_CAMPAIGN),
            "init", "--id", cid,
            "--title", "Soak Test Campaign",
            "--repo-root", str(ROOT),
        ], td)
        assert proc_init.returncode == 0

        # Add 3 interdependent WUs
        run_cmd([
            sys.executable, str(BIN_CAMPAIGN),
            "wu-add", cid,
            "--wu-id", "WU-001",
            "--objective", "Base Component",
            "--verify-cmd", "true",
        ], td)

        run_cmd([
            sys.executable, str(BIN_CAMPAIGN),
            "wu-add", cid,
            "--wu-id", "WU-002",
            "--objective", "Extended Feature",
            "--dependencies", "WU-001",
            "--verify-cmd", "true",
        ], td)

        run_cmd([
            sys.executable, str(BIN_CAMPAIGN),
            "wu-add", cid,
            "--wu-id", "WU-003",
            "--objective", "Integration & CI",
            "--dependencies", "WU-002",
            "--verify-cmd", "true",
        ], td)

        # Fault Injection: Start continuous supervisor, let it run briefly, then terminate abruptly
        env = dict(os.environ, XDG_STATE_HOME=td, PYTHONDONTWRITEBYTECODE="1")
        proc = subprocess.Popen(
            [sys.executable, str(BIN_HEAD), "--campaign-id", cid, "--interval", "1"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        time.sleep(0.3)
        # Terminate
        proc.send_signal(signal.SIGINT)
        proc.communicate(timeout=5)

        # Reconcile after restart
        proc_rec = run_cmd([sys.executable, str(BIN_CAMPAIGN), "reconcile", cid], td)
        assert proc_rec.returncode == 0

        # Run supervisor in once mode until campaign completes
        max_steps = 10
        steps = 0
        while steps < max_steps:
            proc_step = run_cmd([sys.executable, str(BIN_HEAD), "--once", "--campaign-id", cid], td)
            assert proc_step.returncode == 0, f"supervisor step failed: {proc_step.stderr}"
            data = json.loads(run_cmd([sys.executable, str(BIN_CAMPAIGN), "show", cid, "--json"], td).stdout)
            if data["status"] == "completed":
                break
            steps += 1

        assert data["status"] == "completed", f"campaign did not complete in {max_steps} steps"

        # Verify all WUs have evidence bundles
        for wu in data["work_units"]:
            assert wu["status"] == "completed"
            assert wu["evidence"] is not None
            assert wu["evidence"]["diff_digest"] is not None
            assert wu["evidence"]["candidate_sha"] is not None

        # Verify event timeline completeness
        proc_tl = run_cmd([sys.executable, str(BIN_CAMPAIGN), "timeline", cid, "--json"], td)
        assert proc_tl.returncode == 0
        events = json.loads(proc_tl.stdout)
        assert len(events) >= 6
        event_types = {e["event_type"] for e in events}
        assert "CAMPAIGN_INITIALIZED" in event_types
        assert "WORK_UNIT_ADDED" in event_types
        assert "WORK_UNIT_TRANSITION" in event_types
        assert "EVIDENCE_RECORDED" in event_types


def test_remediation_and_budget_exhaustion() -> None:
    with tempfile.TemporaryDirectory(prefix="myrmex-soak-") as td:
        cid = "camp-soak-budget"
        run_cmd([
            sys.executable, str(BIN_CAMPAIGN),
            "init", "--id", cid,
            "--title", "Remediation Budget Test",
            "--corrections-per-wu", "2",
        ], td)

        # Add a WU with a failing verify command
        run_cmd([
            sys.executable, str(BIN_CAMPAIGN),
            "wu-add", cid,
            "--wu-id", "WU-FAIL",
            "--objective", "Always Failing Task",
            "--verify-cmd", "false",
            "--corrections-budget", "1",
        ], td)

        # Run supervisor -> should attempt remediation, exhaust budget, and block WU
        proc_step = run_cmd([sys.executable, str(BIN_HEAD), "--once", "--campaign-id", cid], td)
        assert proc_step.returncode == 0

        data = json.loads(run_cmd([sys.executable, str(BIN_CAMPAIGN), "show", cid, "--json"], td).stdout)
        wu_fail = next(w for w in data["work_units"] if w["id"] == "WU-FAIL")
        assert wu_fail["status"] == "blocked"
        assert wu_fail["blocker"]["type"] == "budget_exhausted"


def main() -> int:
    test_closed_loop_execution_and_resilience()
    test_remediation_and_budget_exhaustion()
    print("campaign closed-loop soak test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
