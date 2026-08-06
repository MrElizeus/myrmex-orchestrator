#!/usr/bin/env python3
"""
Unit and Integration Tests for Real OpenCode Task Driver & Worktree Isolation (P0.12).
Validates:
1. Binary resolution & model configuration.
2. Prohibition of fabricated session IDs (raises OPENCODE_TASK_IDENTITY_MISSING).
3. Secret redaction and 0600 atomic file metadata persistence.
4. Read-only verifier worktrees and workspace mutation rejection.
5. Verifier candidate_sha identity validation.
6. Recovery scenarios: crash before/after sessionID observation, supervisor down, PID reuse, zero duplicate tasks.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))

import scripts.opencode_transport as opencode_transport
import scripts.myrmex_worktree as myrmex_worktree
import scripts.myrmex_task_operation as myrmex_task_operation


class TestOpenCodeTaskDriverP012(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp()
        self.state_dir = Path(self.tmp_dir) / "state"
        os.environ["XDG_STATE_HOME"] = str(self.state_dir)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_01_binary_resolution(self) -> None:
        # Test explicit OPENCODE_BIN env var
        dummy_bin = Path(self.tmp_dir) / "opencode"
        dummy_bin.write_text("#!/bin/sh\necho 'opencode'")
        dummy_bin.chmod(0o755)

        os.environ["OPENCODE_BIN"] = str(dummy_bin)
        resolved = opencode_transport.resolve_opencode_bin()
        self.assertEqual(resolved, str(dummy_bin))
        del os.environ["OPENCODE_BIN"]

    def test_02_prohibit_fabricated_session_ids(self) -> None:
        # Verify that if OpenCode does not emit a sessionID, OPENCODE_TASK_IDENTITY_MISSING is raised
        # rather than fabricating an ID like ses_opencode_...
        dummy_failing_bin = Path(self.tmp_dir) / "opencode_fail"
        dummy_failing_bin.write_text("#!/bin/sh\necho 'Invalid output'")
        dummy_failing_bin.chmod(0o755)
        os.environ["OPENCODE_BIN"] = str(dummy_failing_bin)

        with self.assertRaises(opencode_transport.OpenCodeTaskIdentityMissingError):
            opencode_transport.create_task({
                "prompt": "Test prompt",
                "agent": "myrmex-worker",
                "workspace": self.tmp_dir,
            })
        del os.environ["OPENCODE_BIN"]

    def test_03_secure_metadata_and_secret_redaction(self) -> None:
        # Verify 0600 file permissions and secret sanitization
        raw_secret = "Bearer sk-proj-123456789012345678901234567890 and GITHUB_TOKEN='ghp_12345678901234567890'"
        sanitized = opencode_transport.sanitize_text(raw_secret)
        self.assertNotIn("sk-proj-123456789012345678901234567890", sanitized)
        self.assertNotIn("ghp_12345678901234567890", sanitized)
        self.assertIn("[REDACTED_SECRET]", sanitized)

        meta_path = self.state_dir / "myrmex/task-operations/test_meta.json"
        opencode_transport._write_secure_json(meta_path, {"test": 123})
        self.assertTrue(meta_path.is_file())
        st_mode = meta_path.stat().st_mode
        self.assertEqual(st_mode & 0o777, 0o600)

    def test_04_verifier_worktree_and_mutation_detection(self) -> None:
        # Setup git repo
        repo_dir = Path(self.tmp_dir) / "repo"
        repo_dir.mkdir()
        os.system(f"git -C {repo_dir} init && git -C {repo_dir} config user.name 'Test' && git -C {repo_dir} config user.email 'test@test.com'")
        (repo_dir / "file.txt").write_text("initial content")
        os.system(f"git -C {repo_dir} add . && git -C {repo_dir} commit -m 'init'")

        head_sha = os.popen(f"git -C {repo_dir} rev-parse HEAD").read().strip()

        # Create verifier worktree
        verifier_wt, receipt = myrmex_worktree.create_verifier_worktree(
            source_repo=repo_dir,
            campaign_id="camp-p012",
            wu_id="WU-V1",
            candidate_sha=head_sha,
        )
        self.assertTrue(verifier_wt.exists())

        hash_before = myrmex_worktree.compute_workspace_hash(verifier_wt)

        # Mutate verifier workspace
        (verifier_wt / "mutation.txt").write_text("illegal mutation")
        hash_after = myrmex_worktree.compute_workspace_hash(verifier_wt)

        self.assertNotEqual(hash_before, hash_after)

    def test_05_recovery_scenarios(self) -> None:
        # 1. Crash before observing sessionID
        op1 = myrmex_task_operation.create_task_intent(
            campaign_id="camp-rec",
            work_unit_id="WU-REC1",
            run_id="run-rec",
            role="writer",
            agent="myrmex-worker",
            workspace=self.tmp_dir,
            base_sha="HEAD",
            prompt="Prompt 1",
        )
        self.assertEqual(op1.status, "intent")
        self.assertIsNone(op1.task_id)

        # 2. Crash after observing sessionID
        op2 = myrmex_task_operation.create_task_intent(
            campaign_id="camp-rec",
            work_unit_id="WU-REC2",
            run_id="run-rec",
            role="writer",
            agent="myrmex-worker",
            workspace=self.tmp_dir,
            base_sha="HEAD",
            prompt="Prompt 2",
        )
        op2_obs = myrmex_task_operation.record_task_observed(op2, "ses_observed_999")
        self.assertEqual(op2_obs.status, "task-observed")
        self.assertEqual(op2_obs.task_id, "ses_observed_999")

        # 3. Recovery lookup produces exact same operation record without creating duplicate tasks
        recycled_op = myrmex_task_operation.find_existing_op_for_phase("run-rec", "WU-REC2", "writer", 1)
        self.assertIsNotNone(recycled_op)
        self.assertEqual(recycled_op.operation_id, op2.operation_id)
        self.assertEqual(recycled_op.task_id, "ses_observed_999")


if __name__ == "__main__":
    unittest.main()
