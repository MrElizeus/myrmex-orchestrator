#!/usr/bin/env python3
"""Tests for Myrmex campaign DAG validation, cycle detection, and ready WU computation."""
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


def test_linear_dag() -> None:
    with tempfile.TemporaryDirectory(prefix="myrmex-camp-dag-") as td:
        run_campaign(["init", "--id", "camp-linear", "--title", "Linear DAG"], td)

        run_campaign(["wu-add", "camp-linear", "--wu-id", "WU-001", "--objective", "Step 1"], td)
        run_campaign(["wu-add", "camp-linear", "--wu-id", "WU-002", "--objective", "Step 2", "--dependencies", "WU-001"], td)
        run_campaign(["wu-add", "camp-linear", "--wu-id", "WU-003", "--objective", "Step 3", "--dependencies", "WU-002"], td)

        # Initial state: only WU-001 should be ready
        proc_dag = run_campaign(["dag", "camp-linear"], td)
        assert proc_dag.returncode == 0
        dag = json.loads(proc_dag.stdout)
        assert dag["ready_work_units"] == ["WU-001"]
        assert dag["topological_order"] == ["WU-001", "WU-002", "WU-003"]

        # Complete WU-001
        run_campaign(["wu-transition", "camp-linear", "WU-001", "--phase", "completed", "--status", "completed", "--force"], td)

        # Now WU-002 should be ready
        proc_dag2 = run_campaign(["dag", "camp-linear"], td)
        dag2 = json.loads(proc_dag2.stdout)
        assert dag2["ready_work_units"] == ["WU-002"]

        # Complete WU-002
        run_campaign(["wu-transition", "camp-linear", "WU-002", "--phase", "completed", "--status", "completed", "--force"], td)

        # Now WU-003 should be ready
        proc_dag3 = run_campaign(["dag", "camp-linear"], td)
        dag3 = json.loads(proc_dag3.stdout)
        assert dag3["ready_work_units"] == ["WU-003"]


def test_diamond_dag() -> None:
    with tempfile.TemporaryDirectory(prefix="myrmex-camp-dag-") as td:
        run_campaign(["init", "--id", "camp-diamond", "--title", "Diamond DAG"], td)

        run_campaign(["wu-add", "camp-diamond", "--wu-id", "WU-A", "--objective", "Root A"], td)
        run_campaign(["wu-add", "camp-diamond", "--wu-id", "WU-B", "--objective", "Branch B", "--dependencies", "WU-A"], td)
        run_campaign(["wu-add", "camp-diamond", "--wu-id", "WU-C", "--objective", "Branch C", "--dependencies", "WU-A"], td)
        run_campaign(["wu-add", "camp-diamond", "--wu-id", "WU-D", "--objective", "Join D", "--dependencies", "WU-B,WU-C"], td)

        proc_dag = run_campaign(["dag", "camp-diamond"], td)
        dag = json.loads(proc_dag.stdout)
        assert dag["ready_work_units"] == ["WU-A"]

        # Complete A -> both B and C should be ready
        run_campaign(["wu-transition", "camp-diamond", "WU-A", "--phase", "completed", "--status", "completed", "--force"], td)
        proc_dag2 = run_campaign(["dag", "camp-diamond"], td)
        dag2 = json.loads(proc_dag2.stdout)
        assert set(dag2["ready_work_units"]) == {"WU-B", "WU-C"}

        # Complete B -> D is not ready yet because C is pending
        run_campaign(["wu-transition", "camp-diamond", "WU-B", "--phase", "completed", "--status", "completed", "--force"], td)
        proc_dag3 = run_campaign(["dag", "camp-diamond"], td)
        dag3 = json.loads(proc_dag3.stdout)
        assert dag3["ready_work_units"] == ["WU-C"]

        # Complete C -> D is now ready
        run_campaign(["wu-transition", "camp-diamond", "WU-C", "--phase", "completed", "--status", "completed", "--force"], td)
        proc_dag4 = run_campaign(["dag", "camp-diamond"], td)
        dag4 = json.loads(proc_dag4.stdout)
        assert dag4["ready_work_units"] == ["WU-D"]


def test_cycle_rejection() -> None:
    with tempfile.TemporaryDirectory(prefix="myrmex-camp-dag-") as td:
        run_campaign(["init", "--id", "camp-cycle", "--title", "Cycle Test"], td)
        run_campaign(["wu-add", "camp-cycle", "--wu-id", "WU-X", "--objective", "X"], td)
        run_campaign(["wu-add", "camp-cycle", "--wu-id", "WU-Y", "--objective", "Y", "--dependencies", "WU-X"], td)

        # Attempting to add WU-Z depending on Y and making X depend on Z would form a cycle
        # Or adding a WU that creates a direct circular dependency
        proc_bad = run_campaign(["wu-add", "camp-cycle", "--wu-id", "WU-Z", "--objective", "Z", "--dependencies", "WU-Y"], td)
        assert proc_bad.returncode == 0

        # Now manually injecting a cycle into edges or via direct CLI
        proc_cycle = run_campaign(["wu-add", "camp-cycle", "--wu-id", "WU-CYCLE", "--objective", "Cycle", "--dependencies", "WU-CYCLE"], td)
        assert proc_cycle.returncode != 0
        assert "cycle" in proc_cycle.stderr.lower()


def main() -> int:
    test_linear_dag()
    test_diamond_dag()
    test_cycle_rejection()
    print("campaign DAG test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
