#!/usr/bin/env python3
"""
Unit and Integration Tests for Real OpenCode Task Driver & Worktree Isolation.
Validates intent persistence, idempotency, secret redaction, scope checks, and state transitions.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))

import scripts.opencode_transport as opencode_transport
import scripts.myrmex_worktree as myrmex_worktree
import scripts.myrmex_task_operation as myrmex_task_operation


class TestOpenCodeTaskDriver(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp()
        os.environ["XDG_STATE_HOME"] = str(Path(self.tmp_dir) / "state")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_01_task_intent_persistence_before_dispatch(self) -> None:
        op = myrmex_task_operation.create_task_intent(
            campaign_id="camp-test",
            work_unit_id="WU-001",
            run_id="run-100",
            role="writer",
            agent="myrmex-worker",
            workspace=self.tmp_dir,
            base_sha="abc1234",
            prompt="Test prompt",
        )
        self.assertEqual(op.status, "intent")
        self.assertIsNotNone(op.request_digest)

        retrieved = myrmex_task_operation.find_task_operation(op.operation_id)
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.status, "intent")
        self.assertEqual(retrieved.receipt["phase"], "TASK_INTENT")

    def test_02_task_observed_and_terminal_lifecycle(self) -> None:
        op = myrmex_task_operation.create_task_intent(
            campaign_id="camp-test",
            work_unit_id="WU-002",
            run_id="run-101",
            role="writer",
            agent="myrmex-worker",
            workspace=self.tmp_dir,
            base_sha="abc1234",
            prompt="Test prompt 2",
        )
        op_obs = myrmex_task_operation.record_task_observed(op, "ses_test_12345")
        self.assertEqual(op_obs.status, "task-observed")
        self.assertEqual(op_obs.task_id, "ses_test_12345")

        op_term = myrmex_task_operation.record_task_terminal(
            op_obs,
            "completed",
            "Result text output",
            candidate_sha="def5678",
            diff_digest="sha256diffdigest",
        )
        self.assertEqual(op_term.status, "completed")
        self.assertEqual(op_term.candidate_sha, "def5678")
        self.assertIsNotNone(op_term.result_digest)

    def test_03_secret_redaction(self) -> None:
        raw_text = "Here is my secret sk-proj-123456789012345678901234567890 and GITHUB_TOKEN='ghp_12345678901234567890'"
        sanitized = opencode_transport.sanitize_text(raw_text)
        self.assertNotIn("sk-proj-123456789012345678901234567890", sanitized)
        self.assertNotIn("ghp_12345678901234567890", sanitized)
        self.assertIn("[REDACTED]", sanitized)

    def test_04_worktree_scope_verification(self) -> None:
        # Create a dummy git repo
        repo_dir = Path(self.tmp_dir) / "repo"
        repo_dir.mkdir()
        os.system(f"git -C {repo_dir} init && git -C {repo_dir} config user.name 'Test' && git -C {repo_dir} config user.email 'test@test.com'")
        (repo_dir / "README.md").write_text("Hello")
        os.system(f"git -C {repo_dir} add . && git -C {repo_dir} commit -m 'initial'")

        wt_dir, receipt = myrmex_worktree.create_wu_worktree(
            source_repo=repo_dir,
            campaign_id="camp-test",
            wu_id="WU-003",
            base_sha="HEAD",
            allowed_scope=["src/"],
        )
        self.assertTrue(wt_dir.exists())

        # Modify allowed file inside worktree
        (wt_dir / "src").mkdir(exist_ok=True)
        (wt_dir / "src/main.py").write_text("print(1)")

        violations = myrmex_worktree.verify_workspace_scope(wt_dir, "HEAD", ["src/"])
        self.assertEqual(len(violations), 0)

        # Modify disallowed file outside scope
        (wt_dir / "bad.txt").write_text("bad")
        violations_bad = myrmex_worktree.verify_workspace_scope(wt_dir, "HEAD", ["src/"])
        self.assertTrue(any("outside allowed scope" in v for v in violations_bad))

        # Cleanup
        myrmex_worktree.cleanup_wu_worktree(repo_dir, wt_dir, force=True)


if __name__ == "__main__":
    unittest.main()
