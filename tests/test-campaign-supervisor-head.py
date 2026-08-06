#!/usr/bin/env python3
"""Tests for Myrmex campaign persistent supervisor (myrmex-head)."""
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


def test_supervisor_once_flow() -> None:
    with tempfile.TemporaryDirectory(prefix="myrmex-head-test-") as td:
        # Initialize campaign with 2 sequential WUs
        proc_init = run_cmd([
            sys.executable, str(BIN_CAMPAIGN),
            "init", "--id", "camp-head-test",
            "--title", "Head Test",
            "--repo-root", str(ROOT),
        ], td)
        assert proc_init.returncode == 0

        run_cmd([
            sys.executable, str(BIN_CAMPAIGN),
            "wu-add", "camp-head-test",
            "--wu-id", "WU-001",
            "--objective", "First task",
            "--verify-cmd", "true",
        ], td)

        run_cmd([
            sys.executable, str(BIN_CAMPAIGN),
            "wu-add", "camp-head-test",
            "--wu-id", "WU-002",
            "--objective", "Second task",
            "--dependencies", "WU-001",
            "--verify-cmd", "true",
        ], td)

        # Step 1: Run head --once -> should process WU-001
        proc_head1 = run_cmd([sys.executable, str(BIN_HEAD), "--once", "--campaign-id", "camp-head-test"], td)
        assert proc_head1.returncode == 0, f"head1 failed: {proc_head1.stderr}"
        assert "Work unit WU-001 COMPLETED" in proc_head1.stdout

        # Verify WU-001 completed, evidence recorded
        proc_show1 = run_cmd([sys.executable, str(BIN_CAMPAIGN), "show", "camp-head-test", "--json"], td)
        data1 = json.loads(proc_show1.stdout)
        wu1 = next(w for w in data1["work_units"] if w["id"] == "WU-001")
        assert wu1["status"] == "completed"
        assert wu1["evidence"] is not None
        assert wu1["evidence"]["objective"] == "First task"

        # Step 2: Run head --once -> should process WU-002
        proc_head2 = run_cmd([sys.executable, str(BIN_HEAD), "--once", "--campaign-id", "camp-head-test"], td)
        assert proc_head2.returncode == 0, f"head2 failed: {proc_head2.stderr}"
        assert "Work unit WU-002 COMPLETED" in proc_head2.stdout

        # Step 3: Run head --once -> all done, should mark campaign complete
        proc_head3 = run_cmd([sys.executable, str(BIN_HEAD), "--once", "--campaign-id", "camp-head-test"], td)
        assert proc_head3.returncode == 0

        proc_show_final = run_cmd([sys.executable, str(BIN_CAMPAIGN), "show", "camp-head-test", "--json"], td)
        data_final = json.loads(proc_show_final.stdout)
        assert data_final["status"] == "completed"


def test_lease_exclusivity_and_expiry() -> None:
    with tempfile.TemporaryDirectory(prefix="myrmex-head-test-") as td:
        run_cmd([
            sys.executable, str(BIN_CAMPAIGN),
            "init", "--id", "camp-lease-test",
            "--title", "Lease Test",
        ], td)

        # Acquire lease with Holder A
        proc_la = run_cmd([
            sys.executable, str(BIN_CAMPAIGN),
            "lease-acquire", "camp-lease-test",
            "--holder", "worker-node-a:1234",
            "--duration", "60",
        ], td)
        assert proc_la.returncode == 0

        # Attempt to acquire with Holder B -> must fail
        proc_lb = run_cmd([
            sys.executable, str(BIN_CAMPAIGN),
            "lease-acquire", "camp-lease-test",
            "--holder", "worker-node-b:5678",
            "--duration", "60",
        ], td)
        assert proc_lb.returncode != 0

        # Expire lease artificially
        cfile = Path(td) / "myrmex" / "campaigns" / "camp-lease-test" / "campaign.json"
        data = json.loads(cfile.read_text(encoding="utf-8"))
        data["lease"]["expires_at"] = "2020-01-01T00:00:00+00:00"
        cfile.write_text(json.dumps(data, indent=2), encoding="utf-8")

        # Run reconcile -> should reclaim lease
        proc_rec = run_cmd([sys.executable, str(BIN_CAMPAIGN), "reconcile", "camp-lease-test"], td)
        assert proc_rec.returncode == 0

        # Now Holder B should be able to acquire lease
        proc_lb2 = run_cmd([
            sys.executable, str(BIN_CAMPAIGN),
            "lease-acquire", "camp-lease-test",
            "--holder", "worker-node-b:5678",
            "--duration", "60",
        ], td)
        assert proc_lb2.returncode == 0


def test_supervisor_sigterm_cleanup() -> None:
    with tempfile.TemporaryDirectory(prefix="myrmex-head-test-") as td:
        run_cmd([
            sys.executable, str(BIN_CAMPAIGN),
            "init", "--id", "camp-sigterm-test",
            "--title", "Sigterm Test",
        ], td)

        env = dict(os.environ, XDG_STATE_HOME=td, PYTHONDONTWRITEBYTECODE="1")
        proc = subprocess.Popen(
            [sys.executable, str(BIN_HEAD), "--campaign-id", "camp-sigterm-test", "--interval", "1"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        time.sleep(1.0)
        # Send SIGTERM
        proc.send_signal(signal.SIGTERM)
        stdout, stderr = proc.communicate(timeout=5)

        assert proc.returncode == 0
        assert "Supervisor stopped cleanly" in stdout or "Supervisor stopped cleanly" in stderr

        # Verify lease is released
        proc_show = run_cmd([sys.executable, str(BIN_CAMPAIGN), "show", "camp-sigterm-test", "--json"], td)
        data = json.loads(proc_show.stdout)
        assert data["lease"]["holder"] is None


def main() -> int:
    test_supervisor_once_flow()
    test_lease_exclusivity_and_expiry()
    test_supervisor_sigterm_cleanup()
    print("campaign supervisor head test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
