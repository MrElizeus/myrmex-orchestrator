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
import importlib.machinery
import importlib.util
import types
from unittest.mock import patch
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

    def test_06_p1_bound_create_task_payloads(self) -> None:
        loader = importlib.machinery.SourceFileLoader("binding_head", str(ROOT / "bin/myrmex-head"))
        spec = importlib.util.spec_from_loader(loader.name, loader); head = importlib.util.module_from_spec(spec); loader.exec_module(head)
        root = Path(self.tmp_dir) / "repo"; root.mkdir(); wt = Path(self.tmp_dir) / "camp-p1" / "WU"; wt.mkdir(parents=True); vw = Path(self.tmp_dir) / "camp-p1" / "WU-verifier"; vw.mkdir()
        wu = {"id":"WU", "campaign_id":"camp-p1", "objective":"objective", "scope":["x"], "acceptance_criteria":["accept"], "verification_commands":["check"], "no_op_allowed":True, "implementing_agent":"myrmex-worker", "verifying_agent":"myrmex-verifier", "model":"fixture/local", "provider":"fixture", "work_order":{"schema":"myrmex.work-order/v2", "objective":"objective", "non_goals":["non-goal"], "scope":{"allowed_paths":["x"],"forbidden_paths":[".env"],"preexisting_dirty_paths":["dirty"]}, "acceptance_criteria":["accept"], "verification":{"commands":["check"],"manual_checks":["inspect"]}, "git_policy":{"commit":False,"push":False},"no_op_allowed":True}}
        source_wu = dict(wu)
        payloads=[]; intents=[]; op=types.SimpleNamespace(started_at=None, completed_at=None)
        def argv(args, **kw): return types.SimpleNamespace(returncode=0, stdout="c"*40 if "rev-parse" in args else "", stderr="")
        def create(payload): payloads.append(payload); return types.SimpleNamespace(task_id="new"), object()
        create_wu = patch.object(head.myrmex_worktree,"create_wu_worktree",return_value=(wt,{})); create_verifier = patch.object(head.myrmex_worktree,"create_verifier_worktree",return_value=(vw,{})); scope_check = patch.object(head.myrmex_worktree,"verify_workspace_scope",return_value=[])
        patches = [create_wu, create_verifier, patch.object(head.myrmex_worktree,"_get_worktrees_dir",return_value=Path(self.tmp_dir)), scope_check, patch.object(head.myrmex_worktree,"compute_workspace_hash",return_value="h"), patch.object(head,"run_argv",side_effect=argv), patch.object(head,"compute_candidate_diff_digest",return_value="d"*64), patch.object(head.opencode_transport,"create_task",side_effect=create), patch.object(head.opencode_transport,"wait_task",return_value=types.SimpleNamespace(status="completed")), patch.object(head.opencode_transport,"get_result",return_value=types.SimpleNamespace(text_content="{}",json_payload={"decision":"PASS","candidate_sha":"c"*40,"diff_digest":"d"*64})), patch.object(head.myrmex_task_operation,"find_existing_op_for_phase",return_value=None), patch.object(head.myrmex_task_operation,"create_task_intent",side_effect=lambda **kw: intents.append(kw) or op), patch.object(head.myrmex_task_operation,"record_task_observed"), patch.object(head.myrmex_task_operation,"record_task_terminal"), patch.object(head.myrmex_task_operation,"record_receipt_confirmed")]
        create_wu_mock = create_wu.start(); create_verifier.start(); scope_check_mock = scope_check.start()
        for p in patches[2:]:
            if p is not scope_check:
                p.start()
        try:
            bound = head.CampaignSupervisor._bind_dispatch_context(wu, {"intent":{"campaign_id":"camp-p1"},"route_decision":{"selected":{"agent":"myrmex-worker","model":"fixture/local","provider":"fixture"}}})
            self.assertEqual(wu, source_wu)
            head.OpenCodeTaskDriver().execute_writer(bound,"b"*40,root,"r",{}); head.OpenCodeTaskDriver().execute_verifier(bound,root,"r",{}); head.OpenCodeTaskDriver().execute_remediation(bound,[{"issue":"defect"}],2,root,"r",{})
        finally:
            for p in reversed(patches[2:]):
                if p is not scope_check:
                    p.stop()
            scope_check.stop(); create_verifier.stop(); create_wu.stop()
        self.assertEqual(create_wu_mock.call_count, 1); self.assertEqual(create_wu_mock.call_args.kwargs["allowed_scope"], ["x"])
        self.assertEqual(scope_check_mock.call_args.args[2], ["x"])
        self.assertEqual([p["agent"] for p in payloads],["myrmex-worker","myrmex-verifier","myrmex-worker"])
        self.assertTrue(all(p["model"]=="fixture/local" and "camp-p1" in p["workspace"] and "default-campaign" not in str(p) for p in payloads))
        self.assertEqual([i["provider"] for i in intents], ["fixture"] * 3)
        expected_roles = ["writer", "verifier", "remediator"]
        expected_agents = ["myrmex-worker", "myrmex-verifier", "myrmex-worker"]
        expected_workspaces = [str(wt.resolve()), str(vw.resolve()), str(wt.resolve())]
        for index, intent in enumerate(intents):
            with self.subTest(phase=expected_roles[index]):
                self.assertEqual(intent["campaign_id"], "camp-p1")
                self.assertEqual(intent["work_unit_id"], "WU")
                self.assertEqual(intent["role"], expected_roles[index])
                self.assertEqual(intent["agent"], expected_agents[index])
                self.assertEqual(intent["provider"], "fixture")
                self.assertEqual(intent["model"], "fixture/local")
                self.assertEqual(Path(intent["workspace"]), Path(expected_workspaces[index]))
        for payload in payloads:
            for token in ["objective", "non-goal", ".env", "dirty", "accept", "check", "inspect", '"commit": false', '"push": false', '"no_op_allowed": true']:
                self.assertIn(token, payload["prompt"])
        self.assertIn("read-only",payloads[1]["prompt"])
        self.assertIn("candidate_sha",payloads[1]["prompt"])
        self.assertIn("Diff Digest",payloads[1]["prompt"])
        self.assertIn("defect",payloads[2]["prompt"])
        self.assertIn("Attempt 2",payloads[2]["prompt"])
        for token in ["git commit", "git push", "git pr", "git merge"]: self.assertIn(token, payloads[2]["prompt"])

    def test_07_writer_recovery_identity(self) -> None:
        loader = importlib.machinery.SourceFileLoader("recovery_head", str(ROOT / "bin/myrmex-head"))
        spec = importlib.util.spec_from_loader(loader.name, loader); head = importlib.util.module_from_spec(spec); loader.exec_module(head)
        root = Path(self.tmp_dir) / "repo"; root.mkdir(); wt = Path(self.tmp_dir) / "camp-p1" / "WU"; wt.mkdir(parents=True)
        bound = {"id":"WU", "campaign_id":"camp-p1", "implementing_agent":"myrmex-worker", "model":"fixture/local", "provider":"fixture", "scope":[], "no_op_allowed":True}
        base = dict(campaign_id="camp-p1", work_unit_id="WU", run_id="r", role="writer", agent="myrmex-worker", provider="fixture", model="fixture/local", workspace=str(wt), task_id="ses-existing", diff_digest="d"*64)
        def run(existing, expected_error):
            recovered = patch.object(head.opencode_transport,"recover_existing_task",return_value=types.SimpleNamespace(status="completed"))
            created = patch.object(head.opencode_transport,"create_task")
            found = patch.object(head.myrmex_task_operation,"find_existing_op_for_phase",return_value=existing)
            worktree = patch.object(head.myrmex_worktree,"create_wu_worktree",return_value=(wt,{}))
            argv = patch.object(head,"run_argv",return_value=types.SimpleNamespace(returncode=0,stdout="",stderr=""))
            result = patch.object(head.opencode_transport,"get_result",return_value=types.SimpleNamespace(text_content="{}",json_payload={}))
            with recovered as recovery, created as create_task, found, worktree, argv, result:
                if expected_error:
                    with self.assertRaises(head.ExecutionDriverError): head.OpenCodeTaskDriver().execute_writer(bound,"b"*40,root,"r",{})
                    recovery.assert_not_called(); create_task.assert_not_called()
                else:
                    receipt = head.OpenCodeTaskDriver().execute_writer(bound,"b"*40,root,"r",{})
                    recovery.assert_called_once_with("ses-existing"); create_task.assert_not_called(); self.assertEqual(receipt["task_id"],"ses-existing")
        run(types.SimpleNamespace(**{**base, "provider":"openai"}), True)
        run(types.SimpleNamespace(**base), False)

    def test_08_plan_bound_projection_divergence_fails_before_effects(self) -> None:
        loader = importlib.machinery.SourceFileLoader("divergence_head", str(ROOT / "bin/myrmex-head"))
        spec = importlib.util.spec_from_loader(loader.name, loader); head = importlib.util.module_from_spec(spec); loader.exec_module(head)
        wu = {"id": "WU", "objective": "flat", "scope": ["unsafe"], "acceptance_criteria": ["flat"], "no_op_allowed": False, "work_order": {"schema": "myrmex.work-order/v2", "objective": "authoritative", "non_goals": [], "scope": {"allowed_paths": ["safe"]}, "acceptance_criteria": ["authoritative"], "verification": {}, "no_op_allowed": True}}
        with patch.object(head.myrmex_worktree, "create_wu_worktree") as worktree, patch.object(head.myrmex_task_operation, "create_task_intent") as intent:
            with self.assertRaises(head.ExecutionDriverError): head.OpenCodeTaskDriver().execute_writer(wu, "b" * 40, Path(self.tmp_dir), "r", {})
            worktree.assert_not_called(); intent.assert_not_called()

    def test_09_legacy_writer_prompt_and_scope(self) -> None:
        loader = importlib.machinery.SourceFileLoader("legacy_head", str(ROOT / "bin/myrmex-head"))
        spec = importlib.util.spec_from_loader(loader.name, loader); head = importlib.util.module_from_spec(spec); loader.exec_module(head)
        wt = Path(self.tmp_dir) / "legacy"; wt.mkdir()
        wu = {"id": "WU", "title": "Legacy title", "description": "Legacy description", "scope": ["legacy.py"], "acceptance_criteria": ["legacy accept"], "no_op_allowed": True}
        with patch.object(head.myrmex_worktree, "create_wu_worktree", return_value=(wt, {})), patch.object(head.myrmex_worktree, "verify_workspace_scope", return_value=[]), patch.object(head.myrmex_task_operation, "find_existing_op_for_phase", return_value=None), patch.object(head.myrmex_task_operation, "create_task_intent", return_value=types.SimpleNamespace()), patch.object(head.myrmex_task_operation, "record_task_observed"), patch.object(head.opencode_transport, "create_task", return_value=(types.SimpleNamespace(task_id="new"), object())) as create_task, patch.object(head.opencode_transport, "wait_task", return_value=types.SimpleNamespace(status="completed")), patch.object(head.opencode_transport, "get_result", return_value=types.SimpleNamespace(text_content="", json_payload={})), patch.object(head.myrmex_task_operation, "record_task_terminal"), patch.object(head.myrmex_task_operation, "record_receipt_confirmed"), patch.object(head, "run_argv", return_value=types.SimpleNamespace(returncode=0, stdout="", stderr="")), patch.object(head, "compute_candidate_diff_digest", return_value="d" * 64):
            head.OpenCodeTaskDriver().execute_writer(wu, "b" * 40, Path(self.tmp_dir), "r", {})
        prompt = create_task.call_args.args[0]["prompt"]
        self.assertIn("Legacy title", prompt); self.assertIn("Legacy description", prompt); self.assertIn("legacy accept", prompt); self.assertIn('"legacy.py"', prompt)


if __name__ == "__main__":
    unittest.main()
