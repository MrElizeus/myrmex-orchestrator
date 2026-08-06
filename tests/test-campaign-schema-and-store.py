#!/usr/bin/env python3
"""Tests for Myrmex campaign schema, compare-and-swap store, and event logging."""
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


def test_init_and_show() -> None:
    with tempfile.TemporaryDirectory(prefix="myrmex-camp-test-") as td:
        proc = run_campaign(["init", "--id", "camp-test-alpha", "--title", "Test Alpha", "--objective", "Achieve MVP"], td)
        assert proc.returncode == 0, f"init failed: {proc.stderr}"
        res = json.loads(proc.stdout)
        assert res["ok"] is True
        assert res["id"] == "camp-test-alpha"

        # Show JSON
        proc_show = run_campaign(["show", "camp-test-alpha", "--json"], td)
        assert proc_show.returncode == 0, f"show failed: {proc_show.stderr}"
        data = json.loads(proc_show.stdout)
        assert data["schema"] == "myrmex.campaign/v1"
        assert data["schema_version"] == 1
        assert data["revision"] == 1
        assert data["status"] == "active"
        assert data["title"] == "Test Alpha"
        assert data["objective"] == "Achieve MVP"
        assert data["budgets"]["corrections_per_wu"] == 3

        # Show Formatted
        proc_show_fmt = run_campaign(["show", "camp-test-alpha"], td)
        assert proc_show_fmt.returncode == 0
        assert "Test Alpha" in proc_show_fmt.stdout
        assert "ACTIVE" in proc_show_fmt.stdout


def test_cas_and_events() -> None:
    with tempfile.TemporaryDirectory(prefix="myrmex-camp-test-") as td:
        run_campaign(["init", "--id", "camp-test-cas", "--title", "CAS Test"], td)

        # Add WU 1
        proc_wu = run_campaign([
            "wu-add", "camp-test-cas",
            "--wu-id", "WU-001",
            "--objective", "First WU",
            "--scope", "bin/,tests/",
        ], td)
        assert proc_wu.returncode == 0, f"wu-add failed: {proc_wu.stderr}"

        # Check revision incremented
        proc_show = run_campaign(["show", "camp-test-cas", "--json"], td)
        data = json.loads(proc_show.stdout)
        assert data["revision"] == 2
        assert len(data["work_units"]) == 1
        assert data["work_units"][0]["id"] == "WU-001"

        # Check events log
        proc_tl = run_campaign(["timeline", "camp-test-cas", "--json"], td)
        assert proc_tl.returncode == 0
        events = json.loads(proc_tl.stdout)
        assert len(events) >= 2
        event_types = [e["event_type"] for e in events]
        assert "CAMPAIGN_INITIALIZED" in event_types
        assert "WORK_UNIT_ADDED" in event_types


def test_list() -> None:
    with tempfile.TemporaryDirectory(prefix="myrmex-camp-test-") as td:
        run_campaign(["init", "--id", "camp-test-one", "--title", "Camp One"], td)
        run_campaign(["init", "--id", "camp-test-two", "--title", "Camp Two"], td)

        proc_list = run_campaign(["list", "--json"], td)
        assert proc_list.returncode == 0
        items = json.loads(proc_list.stdout)
        assert len(items) == 2
        ids = {c["id"] for c in items}
        assert "camp-test-one" in ids
        assert "camp-test-two" in ids


def main() -> int:
    test_init_and_show()
    test_cas_and_events()
    test_list()
    print("campaign schema and store test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
