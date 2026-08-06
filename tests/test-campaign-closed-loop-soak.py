#!/usr/bin/env python3
"""End-to-end closed-loop soak, real execution bridge, and fault-injection test for Myrmex campaigns.

Verifies:
1. Real writer changes to repository workspace.
2. Independent read-only verifiers with real failure detection.
3. Real remediation loop tied to defect sets and candidate SHAs.
4. Real CI execution commands with digest capture.
5. Real diff digest calculation from binary git diffs.
6. Real chained commits in git repository.
7. Crash boundary survival without duplicate runs or commits.
8. Verifier mutation prevention and CI failure gating.
"""
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
BIN_STATE = ROOT / "bin/myrmex-state"


def run_cmd(cmd: list[str], state_home: str, cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, XDG_STATE_HOME=state_home, PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=cwd)


def test_real_execution_closed_loop_and_chained_commits() -> None:
    with tempfile.TemporaryDirectory(prefix="myrmex-soak-repo-") as repo_dir, \
         tempfile.TemporaryDirectory(prefix="myrmex-soak-state-") as state_dir:

        # 1. Initialize fixture git repository
        subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Myrmex Tester"], cwd=repo_dir, check=True)
        subprocess.run(["git", "config", "user.email", "tester@myrmex.local"], cwd=repo_dir, check=True)
        readme = Path(repo_dir) / "README.md"
        readme.write_text("# Myrmex Autonomous Test Repository\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=repo_dir, check=True)
        subprocess.run(["git", "commit", "-m", "chore: initial repository baseline"], cwd=repo_dir, check=True)

        proc_base_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, capture_output=True, text=True, check=True)
        initial_sha = proc_base_sha.stdout.strip()

        # 2. Initialize campaign
        cid = "camp-real-soak"
        proc_init = run_cmd([
            sys.executable, str(BIN_CAMPAIGN),
            "init", "--id", cid,
            "--title", "Real Autonomous Campaign E2E",
            "--objective", "Deliver 3 chained real modules",
            "--repo-root", repo_dir,
            "--corrections-per-wu", "3",
        ], state_dir)
        assert proc_init.returncode == 0, f"init failed: {proc_init.stderr}"

        # 3. Define 3 interdependent WUs with real writer, verifier, remediation, and CI commands
        # WU-001: Calc module with intentional initial bug to test verifier + remediation loop
        impl_wu1 = (
            f"{sys.executable} -c \"from pathlib import Path; "
            f"Path('calc.py').write_text('def add(a, b):\\n    return a - b\\n\\ndef sub(a, b):\\n    return a - b\\n', encoding='utf-8')\""
        )
        verify_wu1 = (
            f"{sys.executable} -c \"import calc; "
            f"assert calc.add(2, 3) == 5, f'add failed: {{calc.add(2, 3)}}'; "
            f"assert calc.sub(5, 3) == 2, 'sub failed'\""
        )
        remed_wu1 = (
            f"{sys.executable} -c \"from pathlib import Path; "
            f"Path('calc.py').write_text('def add(a, b):\\n    return a + b\\n\\ndef sub(a, b):\\n    return a - b\\n', encoding='utf-8')\""
        )
        ci_wu1 = f"{sys.executable} -c \"import calc; assert calc.add(10, 20) == 30; assert calc.sub(30, 10) == 20\""

        proc_wu1 = run_cmd([
            sys.executable, str(BIN_CAMPAIGN),
            "wu-add", cid,
            "--wu-id", "WU-001",
            "--objective", "Implement base calculator arithmetic",
            "--impl-cmd", impl_wu1,
            "--verify-cmd", verify_wu1,
            "--remed-cmd", remed_wu1,
            "--ci-cmd", ci_wu1,
            "--corrections-budget", "2",
        ], state_dir)
        assert proc_wu1.returncode == 0, f"wu1 add failed: {proc_wu1.stderr}"

        # WU-002: Formatter module depending on WU-001
        impl_wu2 = (
            f"{sys.executable} -c \"from pathlib import Path; "
            f"Path('formatter.py').write_text('import calc\\n\\ndef format_add(a, b):\\n    return f\\\"{{a}} + {{b}} = {{calc.add(a, b)}}\\\"\\n', encoding='utf-8')\""
        )
        verify_wu2 = (
            f"{sys.executable} -c \"import formatter; "
            f"assert formatter.format_add(2, 3) == '2 + 3 = 5'\""
        )
        ci_wu2 = f"{sys.executable} -c \"import formatter; assert formatter.format_add(10, 5) == '10 + 5 = 15'\""

        proc_wu2 = run_cmd([
            sys.executable, str(BIN_CAMPAIGN),
            "wu-add", cid,
            "--wu-id", "WU-002",
            "--objective", "Implement expression formatting service",
            "--dependencies", "WU-001",
            "--impl-cmd", impl_wu2,
            "--verify-cmd", verify_wu2,
            "--ci-cmd", ci_wu2,
        ], state_dir)
        assert proc_wu2.returncode == 0, f"wu2 add failed: {proc_wu2.stderr}"

        # WU-003: App module depending on WU-002
        impl_wu3 = (
            f"{sys.executable} -c \"from pathlib import Path; "
            f"Path('app.py').write_text('import formatter\\n\\ndef run():\\n    return formatter.format_add(10, 20)\\n\\nif __name__ == \\\"__main__\\\":\\n    print(run())\\n', encoding='utf-8')\""
        )
        verify_wu3 = (
            f"{sys.executable} -c \"import app; "
            f"assert app.run() == '10 + 20 = 30'\""
        )
        ci_wu3 = f"{sys.executable} app.py"

        proc_wu3 = run_cmd([
            sys.executable, str(BIN_CAMPAIGN),
            "wu-add", cid,
            "--wu-id", "WU-003",
            "--objective", "Implement main CLI application integration",
            "--dependencies", "WU-002",
            "--impl-cmd", impl_wu3,
            "--verify-cmd", verify_wu3,
            "--ci-cmd", ci_wu3,
        ], state_dir)
        assert proc_wu3.returncode == 0, f"wu3 add failed: {proc_wu3.stderr}"

        # 4. Fault-Injection: Start continuous supervisor, let it run briefly, kill it with SIGTERM to test crash recovery
        env = dict(os.environ, XDG_STATE_HOME=state_dir, PYTHONDONTWRITEBYTECODE="1")
        proc_sup = subprocess.Popen(
            [sys.executable, str(BIN_HEAD), "--campaign-id", cid, "--interval", "1"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        time.sleep(0.3)
        proc_sup.send_signal(signal.SIGTERM)
        try:
            proc_sup.communicate(timeout=5)
        except Exception:
            proc_sup.kill()

        # Reconcile campaign after interruption
        proc_rec = run_cmd([sys.executable, str(BIN_CAMPAIGN), "reconcile", cid], state_dir)
        assert proc_rec.returncode == 0

        # 5. Continue execution until campaign completes
        max_steps = 15
        steps = 0
        while steps < max_steps:
            proc_step = run_cmd([sys.executable, str(BIN_HEAD), "--once", "--campaign-id", cid], state_dir)
            print(f"STEP {steps} stdout: {proc_step.stdout.strip()}")
            if proc_step.stderr:
                print(f"STEP {steps} stderr: {proc_step.stderr.strip()}")
            assert proc_step.returncode == 0, f"supervisor step error: {proc_step.stderr}"
            data = json.loads(run_cmd([sys.executable, str(BIN_CAMPAIGN), "show", cid, "--json"], state_dir).stdout)
            if data["status"] == "completed":
                break
            steps += 1

        assert data["status"] == "completed", f"Campaign did not complete in {max_steps} steps: {data}"

        # 6. Verify Git Commits: Exactly 3 chained real commits produced
        proc_log = subprocess.run(["git", "log", "--oneline"], cwd=repo_dir, capture_output=True, text=True, check=True)
        log_lines = [l for l in proc_log.stdout.strip().splitlines() if l.strip()]
        assert len(log_lines) == 4, f"Expected 4 commits (initial + 3 WUs), found: {log_lines}"

        # Verify commit messages follow conventional commit format without AI attribution
        assert "feat(wu-003)" in log_lines[0]
        assert "feat(wu-002)" in log_lines[1]
        assert "feat(wu-001)" in log_lines[2]
        assert "chore: initial repository baseline" in log_lines[3]

        # Verify committed files work together
        proc_app = subprocess.run([sys.executable, "app.py"], cwd=repo_dir, capture_output=True, text=True, check=True)
        assert proc_app.stdout.strip() == "10 + 20 = 30"

        # 7. Verify Receipts & Durability
        wu1 = next(w for w in data["work_units"] if w["id"] == "WU-001")
        wu2 = next(w for w in data["work_units"] if w["id"] == "WU-002")
        wu3 = next(w for w in data["work_units"] if w["id"] == "WU-003")

        # WU-001 had a defect detected by verifier and fixed by remediation
        assert wu1["status"] == "completed"
        assert wu1["evidence"] is not None
        assert wu1["evidence"]["writer_receipt"]["status"] == "SUCCESS"
        assert "calc.py" in wu1["evidence"]["writer_receipt"]["files_modified"]
        assert wu1["evidence"]["verifier_receipt"]["status"] == "PASS"
        assert len(wu1["evidence"]["correction_runs"]) == 1, "WU-001 should record exactly 1 correction run"
        assert wu1["evidence"]["ci_operation"]["status"] == "pass"
        assert len(wu1["evidence"]["diff_digest"]) == 64
        assert wu1["evidence"]["commit_sha"] is not None

        # WU-002 and WU-003 receipts
        assert wu2["evidence"]["writer_receipt"]["status"] == "SUCCESS"
        assert wu2["evidence"]["verifier_receipt"]["status"] == "PASS"
        assert wu2["evidence"]["ci_operation"]["status"] == "pass"

        assert wu3["evidence"]["writer_receipt"]["status"] == "SUCCESS"
        assert wu3["evidence"]["verifier_receipt"]["status"] == "PASS"
        assert wu3["evidence"]["ci_operation"]["status"] == "pass"

        # 8. Verify durable myrmex-state run bindings
        for wu in [wu1, wu2, wu3]:
            run_id = wu["run_id"]
            proc_st = run_cmd([sys.executable, str(BIN_STATE), "show", run_id, "--json"], state_dir)
            assert proc_st.returncode == 0, f"myrmex-state show failed for {wu['id']} (run_id={run_id}): stdout={proc_st.stdout}, stderr={proc_st.stderr}"
            st_data = json.loads(proc_st.stdout)
            assert st_data["status"] == "dormant", f"Expected dormant status for {run_id}, got {st_data.get('status')}"


def test_verifier_workspace_mutation_rejection() -> None:
    """Ensures a verifier that mutates the workspace is immediately blocked and prohibited from completing."""
    with tempfile.TemporaryDirectory(prefix="myrmex-verif-repo-") as repo_dir, \
         tempfile.TemporaryDirectory(prefix="myrmex-verif-state-") as state_dir:

        subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo_dir, check=True)
        subprocess.run(["git", "config", "user.email", "tester@test.local"], cwd=repo_dir, check=True)
        Path(repo_dir, "init.txt").write_text("ok\n")
        subprocess.run(["git", "add", "init.txt"], cwd=repo_dir, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir, check=True)

        cid = "camp-mutating-verifier"
        run_cmd([
            sys.executable, str(BIN_CAMPAIGN),
            "init", "--id", cid,
            "--title", "Mutating Verifier Rejection Test",
            "--repo-root", repo_dir,
        ], state_dir)

        # Verifier intentionally writes a file to mutate workspace
        mutating_verifier = f"{sys.executable} -c \"from pathlib import Path; Path('sneaky_file.txt').write_text('bad')\""
        impl = f"{sys.executable} -c \"from pathlib import Path; Path('good.txt').write_text('good')\""

        run_cmd([
            sys.executable, str(BIN_CAMPAIGN),
            "wu-add", cid,
            "--wu-id", "WU-MUTATE",
            "--objective", "Test mutating verifier block",
            "--impl-cmd", impl,
            "--verify-cmd", mutating_verifier,
        ], state_dir)

        # Run supervisor
        proc_step = run_cmd([sys.executable, str(BIN_HEAD), "--once", "--campaign-id", cid], state_dir)
        assert proc_step.returncode == 0

        data = json.loads(run_cmd([sys.executable, str(BIN_CAMPAIGN), "show", cid, "--json"], state_dir).stdout)
        wu = next(w for w in data["work_units"] if w["id"] == "WU-MUTATE")
        assert wu["status"] == "blocked"
        assert wu["blocker"]["type"] == "verifier_workspace_mutation"


def test_ci_failure_blocks_completion() -> None:
    """Ensures CI failure blocks completion and prevents commit."""
    with tempfile.TemporaryDirectory(prefix="myrmex-ci-repo-") as repo_dir, \
         tempfile.TemporaryDirectory(prefix="myrmex-ci-state-") as state_dir:

        subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo_dir, check=True)
        subprocess.run(["git", "config", "user.email", "tester@test.local"], cwd=repo_dir, check=True)
        Path(repo_dir, "init.txt").write_text("ok\n")
        subprocess.run(["git", "add", "init.txt"], cwd=repo_dir, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir, check=True)

        cid = "camp-ci-fail"
        run_cmd([
            sys.executable, str(BIN_CAMPAIGN),
            "init", "--id", cid,
            "--title", "CI Failure Block Test",
            "--repo-root", repo_dir,
        ], state_dir)

        impl = f"{sys.executable} -c \"from pathlib import Path; Path('file.txt').write_text('content')\""
        verify_cmd = f"{sys.executable} -c \"assert True\""
        failing_ci = f"{sys.executable} -c \"import sys; sys.exit(1)\""

        run_cmd([
            sys.executable, str(BIN_CAMPAIGN),
            "wu-add", cid,
            "--wu-id", "WU-CI-FAIL",
            "--objective", "Test CI failure block",
            "--impl-cmd", impl,
            "--verify-cmd", verify_cmd,
            "--ci-cmd", failing_ci,
        ], state_dir)

        # Run supervisor
        proc_step = run_cmd([sys.executable, str(BIN_HEAD), "--once", "--campaign-id", cid], state_dir)
        assert proc_step.returncode == 0

        data = json.loads(run_cmd([sys.executable, str(BIN_CAMPAIGN), "show", cid, "--json"], state_dir).stdout)
        wu = next(w for w in data["work_units"] if w["id"] == "WU-CI-FAIL")
        assert wu["status"] == "blocked"
        assert wu["blocker"]["type"] == "ci_failed"


def main() -> int:
    print("[1/3] Running real execution closed loop & 3 chained commits soak test...")
    test_real_execution_closed_loop_and_chained_commits()
    print("[2/3] Running verifier workspace mutation rejection test...")
    test_verifier_workspace_mutation_rejection()
    print("[3/3] Running CI failure block test...")
    test_ci_failure_blocks_completion()
    print("ALL real execution soak tests PASSED successfully!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
