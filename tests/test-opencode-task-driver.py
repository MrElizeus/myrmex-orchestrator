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
import hashlib
import os
import shutil
import sqlite3
import stat
import tempfile
import unittest
import importlib.machinery
import importlib.util
import types
import inspect
from contextlib import ExitStack, closing
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

    def test_03b_transport_replaces_invalid_utf8_without_losing_identity(self) -> None:
        dummy_bin = Path(self.tmp_dir) / "opencode_invalid_utf8"
        dummy_bin.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "if len(sys.argv) > 1 and sys.argv[1] == 'export':\n"
            "    sys.stdout.buffer.write(b'{\\\"truncated\\\":\\\"\\xef\\xbf')\n"
            "else:\n"
            "    sys.stdout.buffer.write(b'\\xef\\xbf\\n')\n"
            "    print(json.dumps({'sessionID': 'ses_invalid_utf8'}))\n",
            encoding="utf-8",
        )
        dummy_bin.chmod(0o755)

        data_home = Path(self.tmp_dir) / "data-empty"
        with patch.dict(os.environ, {
            "OPENCODE_BIN": str(dummy_bin),
            "XDG_DATA_HOME": str(data_home),
        }):
            identity, proc = opencode_transport.create_task({
                "prompt": "Test prompt",
                "agent": "myrmex-worker",
                "workspace": self.tmp_dir,
            })
            self.assertEqual(identity.task_id, "ses_invalid_utf8")
            proc.wait(timeout=5)

            snapshot = opencode_transport.get_task(
                identity.task_id,
                transport_state_dir=self.state_dir / "myrmex/task-operations",
            )
            self.assertEqual(snapshot.status, "failed")
            self.assertIsNone(snapshot.raw_export)

    def test_03c_truncated_export_recovers_exact_local_session(self) -> None:
        dummy_bin = Path(self.tmp_dir) / "opencode_truncated_export"
        dummy_bin.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "sys.stdout.buffer.write(b'{' + b'x' * 65535)\n",
            encoding="utf-8",
        )
        dummy_bin.chmod(0o755)

        data_home = Path(self.tmp_dir) / "data"
        db_path = data_home / "opencode/opencode.db"
        db_path.parent.mkdir(parents=True)
        session_id = "ses_large_exact"
        with closing(sqlite3.connect(db_path)) as connection:
            connection.executescript("""
                CREATE TABLE session (
                    id TEXT PRIMARY KEY, project_id TEXT, slug TEXT, directory TEXT,
                    title TEXT, version TEXT, permission TEXT, time_created INTEGER,
                    time_updated INTEGER, path TEXT, agent TEXT, model TEXT, cost REAL,
                    tokens_input INTEGER, tokens_output INTEGER, tokens_reasoning INTEGER,
                    tokens_cache_read INTEGER, tokens_cache_write INTEGER
                );
                CREATE TABLE message (
                    id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT
                );
                CREATE TABLE part (
                    id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
                    time_created INTEGER, data TEXT
                );
            """)
            connection.execute(
                "INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (session_id, "project", "slug", self.tmp_dir, "title", "1", "[]",
                 1, 4, self.tmp_dir, "myrmex-worker", json.dumps({"id": "model"}),
                 0.0, 1, 2, 3, 4, 5),
            )
            connection.executemany(
                "INSERT INTO message VALUES (?,?,?,?)",
                [
                    ("msg_user", session_id, 1, json.dumps({"role": "user", "time": {"created": 1}})),
                    ("msg_final", session_id, 3, json.dumps({
                        "role": "assistant", "finish": "stop", "time": {"created": 3, "completed": 4}
                    })),
                ],
            )
            connection.executemany(
                "INSERT INTO part VALUES (?,?,?,?,?)",
                [
                    ("part_large", "msg_user", session_id, 2,
                     json.dumps({"type": "text", "text": "x" * 70000})),
                    ("part_final", "msg_final", session_id, 4,
                     json.dumps({"type": "text", "text": json.dumps({
                         "decision": "COMPLETED", "summary": "no changes"
                     })})),
                ],
            )
            connection.commit()

        with patch.dict(os.environ, {
            "OPENCODE_BIN": str(dummy_bin),
            "XDG_DATA_HOME": str(data_home),
        }):
            snapshot = opencode_transport.get_task(session_id, transport_state_dir=self.state_dir)
            self.assertEqual(snapshot.status, "completed")
            self.assertEqual(snapshot.raw_export["info"]["id"], session_id)
            self.assertEqual(len(snapshot.raw_export["messages"][0]["parts"][0]["text"]), 70000)
            result = opencode_transport.get_result(session_id, transport_state_dir=self.state_dir)
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.json_payload["decision"], "COMPLETED")

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

    def test_10_verifier_cycle_attempt_is_durable_and_recovery_is_idempotent(self) -> None:
        loader = importlib.machinery.SourceFileLoader("cycle_head", str(ROOT / "bin/myrmex-head"))
        spec = importlib.util.spec_from_loader(loader.name, loader); head = importlib.util.module_from_spec(spec); loader.exec_module(head)
        wt = Path(self.tmp_dir) / "repo"; wt.mkdir(); vw = Path(self.tmp_dir) / "vw"; vw.mkdir()
        wu = {"id":"WU", "campaign_id":"camp", "verifying_agent":"verifier", "provider":"p", "model":"m", "verification_commands":[]}
        op = types.SimpleNamespace(task_id="task-2", operation_id="op-r-WU-verifier-att2", campaign_id="camp", agent="verifier", provider="p", model="m", workspace=str(vw), diff_digest="d"*64)
        with patch.object(head.myrmex_worktree, "_get_worktrees_dir", return_value=Path(self.tmp_dir)), patch.object(head.myrmex_worktree, "create_verifier_worktree", return_value=(vw, {})), patch.object(head.myrmex_worktree, "compute_workspace_hash", return_value="h"), patch.object(head, "run_argv", return_value=types.SimpleNamespace(stdout="c"*40, returncode=0, stderr="")), patch.object(head.myrmex_task_operation, "find_existing_op_for_phase", return_value=None) as lookup, patch.object(head.myrmex_task_operation, "create_task_intent", return_value=types.SimpleNamespace()) as intent, patch.object(head.opencode_transport, "create_task", return_value=(types.SimpleNamespace(task_id="new"), object())), patch.object(head.opencode_transport, "wait_task", return_value=types.SimpleNamespace(status="completed")), patch.object(head.opencode_transport, "get_result", return_value=types.SimpleNamespace(text_content="{}", json_payload={"decision":"PASS", "candidate_sha":"c"*40, "diff_digest":"d"*64})), patch.object(head.myrmex_task_operation, "record_task_observed"), patch.object(head.myrmex_task_operation, "record_task_terminal"), patch.object(head.myrmex_task_operation, "record_receipt_confirmed"), patch.object(head, "compute_candidate_diff_digest", return_value="d"*64):
            head.OpenCodeTaskDriver().execute_verifier(wu, wt, "r", {}, attempt=2)
        lookup.assert_called_once_with("r", "WU", "verifier", 2)
        self.assertEqual(intent.call_args.kwargs["attempt"], 2)

        recovered = types.SimpleNamespace(**{**op.__dict__, "status":"completed", "candidate_sha":"c"*40,
            "result_digest":"d"*64, "receipt":{"schema":"myrmex.task-operation/v1", "phase":"RESULT_RECEIPT_CONFIRMED",
            "observed_task_id":"task-2", "result_digest":"d"*64,
            "result_payload":{"decision":"PASS", "candidate_sha":"c"*40, "diff_digest":"d"*64}}})
        with patch.object(head.myrmex_worktree, "create_verifier_worktree", return_value=(vw, {})), patch.object(head.myrmex_worktree, "compute_workspace_hash", return_value="h"), patch.object(head, "run_argv", return_value=types.SimpleNamespace(stdout="c"*40, returncode=0, stderr="")), patch.object(head.myrmex_task_operation, "find_existing_op_for_phase", return_value=recovered) as lookup, patch.object(head.opencode_transport, "recover_existing_task", return_value=types.SimpleNamespace(status="completed")), patch.object(head.opencode_transport, "get_result", return_value=types.SimpleNamespace(json_payload={"decision":"PASS", "candidate_sha":"c"*40, "diff_digest":"d"*64}, text_content="{}")), patch.object(head.opencode_transport, "create_task") as create, patch.object(head.myrmex_task_operation, "create_task_intent") as new_intent, patch.object(head, "compute_candidate_diff_digest", return_value="d"*64):
            head.OpenCodeTaskDriver().execute_verifier(wu, wt, "r", {}, attempt=2)
        lookup.assert_called_once_with("r", "WU", "verifier", 2); create.assert_not_called(); new_intent.assert_not_called()

    def test_11_fixture_verifier_accepts_attempt_and_active_attempt_math(self) -> None:
        loader = importlib.machinery.SourceFileLoader("fixture_head", str(ROOT / "bin/myrmex-head"))
        spec = importlib.util.spec_from_loader(loader.name, loader); head = importlib.util.module_from_spec(spec); loader.exec_module(head)
        self.assertIn("attempt", inspect.signature(head.FixtureCommandDriver.execute_verifier).parameters)
        self.assertEqual(head.OpenCodeTaskDriver.active_recovery_attempts("remediating", 0), (1, 2))
        self.assertEqual(head.OpenCodeTaskDriver.active_recovery_attempts("verifying", 1), (2, 2))

    def _production_head(self):
        loader = importlib.machinery.SourceFileLoader("production_head", str(ROOT / "bin/myrmex-head"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        head = importlib.util.module_from_spec(spec); loader.exec_module(head)
        return head

    def _production_ops(self, head):
        sha = "0" * 64; candidate = "42fcc7" + "0" * 34
        wt = Path(self.tmp_dir) / "camp" / "WU"; vw = Path(self.tmp_dir) / "camp" / "WU-verifier"
        wt.mkdir(parents=True); vw.mkdir()
        def receipt(task, payload):
            return {"schema":"myrmex.task-operation/v1", "phase":"RESULT_RECEIPT_CONFIRMED", "observed_task_id":task,
                    "result_digest":sha, "result_payload":payload, "sanitized_summary":""}
        writer = types.SimpleNamespace(operation_id="op-run-WU-writer-att1", campaign_id="camp", work_unit_id="WU", run_id="run", role="writer", agent="writer", provider="provider", model="model", workspace=str(wt.resolve()), base_sha=candidate, candidate_sha=None, diff_digest=sha, status="completed", task_id="writer-task", result_digest=sha, receipt=receipt("writer-task", None))
        verifier_payload = {"decision":"FAIL", "candidate_sha":candidate, "diff_digest":sha, "defects":[{"issue":"x"}]}
        verifier = types.SimpleNamespace(operation_id="op-run-WU-verifier-att1", campaign_id="camp", work_unit_id="WU", run_id="run", role="verifier", agent="verifier", provider="provider", model="model", workspace=str(vw.resolve()), base_sha=candidate, candidate_sha=candidate, diff_digest=sha, status="failed", task_id="verifier-task", result_digest=sha, receipt=receipt("verifier-task", verifier_payload))
        remediator = types.SimpleNamespace(operation_id="op-run-WU-remediator-att1", campaign_id="camp", work_unit_id="WU", run_id="run", role="remediator", agent="writer", provider="provider", model="model", workspace=str(wt.resolve()), base_sha=None, candidate_sha=None, diff_digest=None, status="intent", task_id=None, result_digest=None, receipt={"phase":"TASK_INTENT"})
        wu = {"id":"WU", "campaign_id":"camp", "base_sha":candidate, "implementing_agent":"writer", "verifying_agent":"verifier", "provider":"provider", "model":"model"}
        return sha, candidate, wt, vw, writer, verifier, remediator, wu

    def test_A_exact_production_receipts_recover(self) -> None:
        head = self._production_head(); sha, candidate, wt, vw, writer, verifier, remediator, wu = self._production_ops(head)
        lookup = lambda r, w, role, attempt: writer if role == "writer" else verifier
        argv = lambda args, **kw: types.SimpleNamespace(stdout=candidate if args[:3] == ["git", "rev-parse", "HEAD"] else "", returncode=0, stderr="")
        with patch.object(head.myrmex_worktree, "_get_worktrees_dir", return_value=Path(self.tmp_dir)), patch.object(head.myrmex_task_operation, "find_existing_op_for_phase", side_effect=lookup), patch.object(head, "run_argv", side_effect=argv), patch.object(head, "compute_candidate_diff_digest", return_value=sha), patch.object(head.myrmex_worktree, "compute_workspace_hash", return_value="stable"):
            self.assertEqual(head.OpenCodeTaskDriver().recover_writer_receipt(wu, Path(self.tmp_dir), "run", {})["diff_digest"], sha)
            writer_r, verifier_r = head.OpenCodeTaskDriver().recover_active_receipts(wu, Path(self.tmp_dir), "run", {}, 1)
            self.assertEqual(verifier_r["status"], "FAIL")

    def test_B_every_operation_identity_field_fails_closed(self) -> None:
        head = self._production_head(); sha, candidate, wt, vw, writer, verifier, _, _ = self._production_ops(head)
        fields = {"run_id":"bad", "work_unit_id":"bad", "role":"bad", "operation_id":"bad", "campaign_id":"bad", "agent":"bad", "provider":"bad", "model":"bad", "workspace":str(Path(self.tmp_dir)/"bad"), "base_sha":"bad", "task_id":"other", "result_digest":"bad"}
        for field, value in fields.items():
            with self.subTest(field=field):
                mutated = types.SimpleNamespace(**writer.__dict__); setattr(mutated, field, value)
                with patch.object(head.myrmex_task_operation, "find_existing_op_for_phase", return_value=mutated):
                    with self.assertRaises(head.ExecutionDriverError):
                        head.OpenCodeTaskDriver()._confirmed_operation("run", "WU", "writer", 1, "camp", "writer", "provider", "model", wt, candidate)

    def test_C_writer_diff_and_verifier_payload_mismatch_rejected(self) -> None:
        head = self._production_head(); sha, candidate, wt, vw, writer, verifier, _, wu = self._production_ops(head)
        with patch.object(head.myrmex_worktree, "_get_worktrees_dir", return_value=Path(self.tmp_dir)/"camp"), patch.object(head.myrmex_task_operation, "find_existing_op_for_phase", return_value=writer), patch.object(head, "run_argv", return_value=types.SimpleNamespace(stdout="", returncode=0, stderr="")), patch.object(head, "compute_candidate_diff_digest", return_value="f"*64):
            with self.assertRaises(head.ExecutionDriverError): head.OpenCodeTaskDriver().recover_writer_receipt(wu, Path(self.tmp_dir), "run", {})
        bad = types.SimpleNamespace(**verifier.__dict__); bad.receipt = dict(verifier.receipt); bad.receipt["result_payload"] = {"decision":"MAYBE", "candidate_sha":"bad", "diff_digest":"bad"}
        with patch.object(head.myrmex_task_operation, "find_existing_op_for_phase", return_value=bad):
            with self.assertRaises(head.ExecutionDriverError): head.OpenCodeTaskDriver()._confirmed_operation("run", "WU", "verifier", 1, "camp", "verifier", "provider", "model", vw, candidate)

    def test_D_task_intent_reused_once_and_malformed_intent_rejected(self) -> None:
        head = self._production_head(); sha, candidate, wt, vw, _, _, intent, wu = self._production_ops(head)
        created = types.SimpleNamespace(task_id="new-task"); task = types.SimpleNamespace(text_content="", json_payload={}, status="completed", error_type=None)
        with patch.object(head.myrmex_worktree, "_get_worktrees_dir", return_value=Path(self.tmp_dir)), patch.object(head.myrmex_task_operation, "find_existing_op_for_phase", return_value=intent), patch.object(head.myrmex_task_operation, "create_task_intent") as new_intent, patch.object(head.opencode_transport, "create_task", return_value=(created, object())) as create, patch.object(head.opencode_transport, "wait_task", return_value=types.SimpleNamespace(status="completed")), patch.object(head.opencode_transport, "get_result", return_value=task), patch.object(head.myrmex_task_operation, "record_task_observed") as observed, patch.object(head.myrmex_task_operation, "record_task_terminal"), patch.object(head.myrmex_task_operation, "record_receipt_confirmed"), patch.object(head, "run_argv", return_value=types.SimpleNamespace(stdout=candidate, returncode=0, stderr="")):
            head.OpenCodeTaskDriver().execute_remediation(wu, [{"issue":"x"}], 1, Path(self.tmp_dir), "run", {})
            new_intent.assert_not_called(); create.assert_called_once(); observed.assert_called_once_with(intent, "new-task")
        malformed = types.SimpleNamespace(**intent.__dict__); malformed.receipt = {"phase":"BAD"}
        with patch.object(head.myrmex_task_operation, "find_existing_op_for_phase", return_value=malformed), patch.object(head.opencode_transport, "create_task") as create:
            with self.assertRaises(head.ExecutionDriverError): head.OpenCodeTaskDriver().execute_remediation(wu, [], 1, Path(self.tmp_dir), "run", {})
            create.assert_not_called()

    def test_H_remediator_confirmed_recovery_mismatch_fails_closed(self) -> None:
        head = self._production_head(); sha, candidate, wt, _, _, _, _, wu = self._production_ops(head)
        def make(**changes):
            values = dict(operation_id="op-run-WU-remediator-att1", campaign_id="camp", work_unit_id="WU", run_id="run", role="remediator", agent="writer", provider="provider", model="model", workspace=str(wt.resolve()), base_sha=None, candidate_sha=candidate, diff_digest=sha, status="completed", task_id="remediator-task", result_digest=sha, started_at="now", completed_at="now", receipt={"schema":"myrmex.task-operation/v1", "phase":"RESULT_RECEIPT_CONFIRMED", "observed_task_id":"remediator-task", "result_digest":sha, "result_payload":{"decision":"COMPLETED"}})
            values.update(changes); return types.SimpleNamespace(**values)
        transport = [patch.object(head.opencode_transport, name) for name in ("recover_existing_task", "wait_task", "get_result", "create_task")]
        persist = [patch.object(head.myrmex_task_operation, name) for name in ("create_task_intent", "record_task_observed", "record_task_terminal", "record_receipt_confirmed")]
        mocks = []
        with ExitStack() as stack:
            for context in transport + persist: mocks.append(stack.enter_context(context))
            stack.enter_context(patch.object(head.myrmex_worktree, "_get_worktrees_dir", return_value=Path(self.tmp_dir)))
            stack.enter_context(patch.object(head.myrmex_task_operation, "find_existing_op_for_phase", return_value=make()))
            stack.enter_context(patch.object(head, "run_argv", return_value=types.SimpleNamespace(stdout=candidate, returncode=0, stderr="")))
            stack.enter_context(patch.object(head, "compute_candidate_diff_digest", return_value=sha))
            result = head.OpenCodeTaskDriver().execute_remediation(wu, [{"issue":"x"}], 1, Path(self.tmp_dir), "run", {})
        self.assertEqual(result["candidate_sha"], candidate)
        for mocked in mocks: mocked.assert_not_called()
        mutations = ("status", "task_id", "result_digest", "candidate_sha", "diff_digest")
        for field in mutations:
            bad = make(**{field: "bad" if field != "status" else "failed"})
            with self.subTest(field=field), patch.object(head.myrmex_task_operation, "find_existing_op_for_phase", return_value=bad), patch.object(head, "run_argv", return_value=types.SimpleNamespace(stdout=candidate, returncode=0, stderr="")), patch.object(head, "compute_candidate_diff_digest", return_value=sha):
                with self.assertRaises(head.ExecutionDriverError): head.OpenCodeTaskDriver().execute_remediation(wu, [], 1, Path(self.tmp_dir), "run", {})
        for field, value in (("observed_task_id", "bad"), ("receipt_result_digest", "bad"), ("payload_decision", "PASS")):
            bad = make();
            if field == "observed_task_id": bad.receipt["observed_task_id"] = value
            elif field == "receipt_result_digest": bad.receipt["result_digest"] = value
            else: bad.receipt["result_payload"]["decision"] = value
            with self.subTest(field=field), patch.object(head.myrmex_task_operation, "find_existing_op_for_phase", return_value=bad), patch.object(head, "run_argv", return_value=types.SimpleNamespace(stdout=candidate, returncode=0, stderr="")), patch.object(head, "compute_candidate_diff_digest", return_value=sha):
                with self.assertRaises(head.ExecutionDriverError): head.OpenCodeTaskDriver().execute_remediation(wu, [], 1, Path(self.tmp_dir), "run", {})

    def test_I_request_digest_mismatch_blocks_dispatch_for_each_role(self) -> None:
        head = self._production_head(); sha, candidate, wt, vw, writer, verifier, remediator, wu = self._production_ops(head)
        writer.receipt["phase"] = "TASK_INTENT"; writer.status = "intent"; writer.task_id = None; writer.request_digest = "f" * 64; writer.base_sha = candidate
        verifier.receipt["phase"] = "TASK_INTENT"; verifier.status = "intent"; verifier.task_id = None; verifier.request_digest = "f" * 64
        remediator.request_digest = "f" * 64
        cases = [("writer", lambda d: d.execute_writer(wu, candidate, Path(self.tmp_dir), "run", {}), writer),
                 ("verifier", lambda d: d.execute_verifier(wu, Path(self.tmp_dir), "run", {}, 1), verifier),
                 ("remediator", lambda d: d.execute_remediation(wu, [{"issue":"x"}], 1, Path(self.tmp_dir), "run", {}), remediator)]
        for role, invoke, op in cases:
            with self.subTest(role=role), patch.object(head.myrmex_worktree, "create_wu_worktree", return_value=(wt, {})), patch.object(head.myrmex_worktree, "create_verifier_worktree", return_value=(vw, {})), patch.object(head.myrmex_worktree, "_get_worktrees_dir", return_value=Path(self.tmp_dir)), patch.object(head.myrmex_worktree, "compute_workspace_hash", return_value="stable"), patch.object(head, "run_argv", return_value=types.SimpleNamespace(stdout=candidate, returncode=0, stderr="")), patch.object(head, "compute_candidate_diff_digest", return_value=sha), patch.object(head.myrmex_task_operation, "find_existing_op_for_phase", return_value=op), patch.object(head.opencode_transport, "create_task") as create:
                with self.assertRaises(head.ExecutionDriverError): invoke(head.OpenCodeTaskDriver())
                create.assert_not_called()

    def _seed_real_op(self, head, *, campaign, wuid, run, role, attempt, agent, workspace,
                      base_sha, task_id, status, candidate_sha=None, diff_digest=None,
                      payload=None, prompt="seed"):
        op = head.myrmex_task_operation.create_task_intent(
            campaign_id=campaign, work_unit_id=wuid, run_id=run, role=role,
            agent=agent, workspace=str(workspace), base_sha=base_sha,
            prompt=prompt, provider="opencode", model="default", attempt=attempt)
        head.myrmex_task_operation.record_task_observed(op, task_id)
        head.myrmex_task_operation.record_task_terminal(
            op, status, "{}", candidate_sha=candidate_sha, diff_digest=diff_digest,
            result_payload=payload)
        head.myrmex_task_operation.record_receipt_confirmed(op)
        return op

    def _stable_external_boundaries(self, head, root, writer, verifier, candidate, digest):
        def argv(args, **kw):
            stdout = candidate if args[:3] == ["git", "rev-parse", "HEAD"] else ""
            return types.SimpleNamespace(stdout=stdout, returncode=0, stderr="")
        return [
            patch.object(head.myrmex_worktree, "_get_worktrees_dir", return_value=root),
            patch.object(head.myrmex_worktree, "create_wu_worktree", return_value=(writer, {})),
            patch.object(head.myrmex_worktree, "create_verifier_worktree", return_value=(verifier, {})),
            patch.object(head.myrmex_worktree, "verify_workspace_scope", return_value=[]),
            patch.object(head.myrmex_worktree, "compute_workspace_hash", return_value="stable"),
            patch.object(head, "run_argv", side_effect=argv),
            patch.object(head, "compute_candidate_diff_digest", return_value=digest),
        ]

    def test_E_production_remediating_recovery_effects_are_bound(self) -> None:
        head = self._production_head(); root = Path(self.tmp_dir) / "ledger-E"
        writer = root / "camp-E" / "WU-E"; verifier = root / "camp-E" / "WU-E-verifier"
        writer.mkdir(parents=True); verifier.mkdir(parents=True)
        candidate, digest = "4" * 40, "e" * 64
        defects = [{"issue": "seeded defect"}]
        wu = {"id":"WU-E", "campaign_id":"camp-E", "objective":"recover E", "phase":"remediating",
              "corrections_used":0, "corrections_budget":4, "no_op_allowed":True, "scope":[],
              "verification_commands":[], "base_sha":candidate, "implementing_agent":"writer",
              "verifying_agent":"verifier", "provider":"opencode", "model":"default",
              "correction_runs":[]}
        run = "run-E"
        writer_op = self._seed_real_op(head, campaign="camp-E", wuid="WU-E", run=run, role="writer", attempt=1,
            agent="writer", workspace=writer, base_sha=candidate, task_id="task-E-writer", status="completed", diff_digest=digest)
        verifier_op = self._seed_real_op(head, campaign="camp-E", wuid="WU-E", run=run, role="verifier", attempt=1,
            agent="verifier", workspace=verifier, base_sha=candidate, task_id="task-E-verifier-1", status="failed",
            candidate_sha=candidate, diff_digest=digest, payload={"decision":"FAIL", "candidate_sha":candidate,
            "diff_digest":digest, "defects":defects})
        driver = head.OpenCodeTaskDriver()
        prompt = (f"Remediation Task (Attempt 1) for Work Unit WU-E\n"
            f"Work-order context: {json.dumps(driver._work_order_context(wu), sort_keys=True)}\n"
            f"Defects to correct: {json.dumps(defects)}\nAllowed Scope: []\n"
            "Apply bounded corrections to resolve all defects.\n"
            "Do NOT execute git commit, git push, git pr, or git merge.\n"
            'Format response as JSON: {"decision": "COMPLETED", "summary": "..."}')
        remediator_op = head.myrmex_task_operation.create_task_intent(
            campaign_id="camp-E", work_unit_id="WU-E", run_id=run, role="remediator", agent="writer",
            workspace=str(writer), base_sha=candidate, prompt=prompt, provider="opencode", model="default", attempt=1)
        created = []; payloads = [{"decision":"COMPLETED"}, {"decision":"PASS", "candidate_sha":candidate, "diff_digest":digest}]
        def create_task(request):
            created.append(request); return types.SimpleNamespace(task_id=f"task-E-{len(created)}"), object()
        def get_result(task_id):
            return types.SimpleNamespace(text_content="{}", json_payload=payloads.pop(0))
        contexts = self._stable_external_boundaries(head, root, writer, verifier, candidate, digest)
        contexts += [patch.object(head.opencode_transport, "create_task", side_effect=create_task),
                     patch.object(head.opencode_transport, "wait_task", return_value=types.SimpleNamespace(status="completed")),
                     patch.object(head.opencode_transport, "get_result", side_effect=get_result),
                      patch.object(head.opencode_transport, "recover_existing_task"),
                     patch.object(head.CampaignSupervisor, "ensure_myrmex_state_run", return_value=run),
                     patch.object(head.CampaignSupervisor, "resolve_execution_driver", return_value=driver),
                     patch.object(head.CampaignSupervisor, "_transition_wu"), patch.object(head.CampaignSupervisor, "sync_myrmex_state_phase"),
                     patch.object(head.CampaignSupervisor, "produce_governed_commit"), patch.object(head.CampaignSupervisor, "_block_wu"), patch.object(head.subprocess, "run", return_value=types.SimpleNamespace(returncode=0, stdout="", stderr="")),
                     patch.object(head.CampaignSupervisor, "complete_myrmex_state_run", return_value=True)]
        with ExitStack() as stack:
            for context in contexts: stack.enter_context(context)
            result = head.CampaignSupervisor().run_work_unit("camp-E", {"repository_root":str(root), "branch":"main", "status":"active"}, wu)
        self.assertTrue(result); self.assertEqual(len(created), 2)
        self.assertEqual([p["prompt"].split()[0] for p in created], ["Remediation", "Verification"])
        for role, attempt, expected in (("writer",1,"completed"),("verifier",1,"failed"),("remediator",1,"completed"),("verifier",2,"completed")):
            op = head.myrmex_task_operation.find_existing_op_for_phase(run, "WU-E", role, attempt)
            self.assertIsNotNone(op); self.assertEqual(op.receipt["phase"], "RESULT_RECEIPT_CONFIRMED"); self.assertEqual(op.status, expected)
            expected_result_digest = hashlib.sha256(b"{}").hexdigest()
            self.assertEqual(op.result_digest, expected_result_digest)
            self.assertEqual(op.receipt["result_digest"], expected_result_digest)
        self.assertEqual(writer_op.task_id, "task-E-writer"); self.assertEqual(verifier_op.task_id, "task-E-verifier-1")
        self.assertEqual(remediator_op.operation_id, "op-run-E-WU-E-remediator-att1")
        self.assertTrue((Path(self.state_dir) / "myrmex/task-operations/op-run-E-WU-E-verifier-att2.json").is_file())

    def test_F_multi_cycle_attempts_are_one_two_three(self) -> None:
        head = self._production_head(); root = Path(self.tmp_dir) / "ledger-F"; writer = root / "camp-F" / "WU-F"; verifier = root / "camp-F" / "WU-F-verifier"
        writer.mkdir(parents=True); verifier.mkdir(parents=True); candidate, digest, run = "5" * 40, "f" * 64, "run-F"
        wu = {"id":"WU-F", "campaign_id":"camp-F", "objective":"cycle F", "phase":"pending", "corrections_used":0,
              "corrections_budget":4, "no_op_allowed":True, "scope":[], "verification_commands":[], "base_sha":candidate,
              "implementing_agent":"writer", "verifying_agent":"verifier", "provider":"opencode", "model":"default", "correction_runs":[]}
        created = []; sequence = [("writer", {"decision":"COMPLETED"}), ("verifier", {"decision":"FAIL", "candidate_sha":candidate, "diff_digest":digest, "defects":[{"issue":"one"}]}),
                   ("remediator", {"decision":"COMPLETED"}), ("verifier", {"decision":"FAIL", "candidate_sha":candidate, "diff_digest":digest, "defects":[{"issue":"two"}]}),
                   ("remediator", {"decision":"COMPLETED"}), ("verifier", {"decision":"PASS", "candidate_sha":candidate, "diff_digest":digest})]
        def create_task(request):
            created.append(request); return types.SimpleNamespace(task_id=f"task-F-{len(created)}"), object()
        def get_result(task_id): return types.SimpleNamespace(text_content="{}", json_payload=sequence[len(created)-1][1])
        contexts = self._stable_external_boundaries(head, root, writer, verifier, candidate, digest)
        contexts += [patch.object(head.opencode_transport, "create_task", side_effect=create_task), patch.object(head.opencode_transport, "wait_task", return_value=types.SimpleNamespace(status="completed")), patch.object(head.opencode_transport, "get_result", side_effect=get_result), patch.object(head.subprocess, "run", return_value=types.SimpleNamespace(returncode=0, stdout="", stderr="")),
                     patch.object(head.CampaignSupervisor, "ensure_myrmex_state_run", return_value=run), patch.object(head.CampaignSupervisor, "resolve_execution_driver", return_value=head.OpenCodeTaskDriver()), patch.object(head.CampaignSupervisor, "_transition_wu"), patch.object(head.CampaignSupervisor, "sync_myrmex_state_phase"), patch.object(head.CampaignSupervisor, "produce_governed_commit"), patch.object(head.CampaignSupervisor, "_block_wu"), patch.object(head.CampaignSupervisor, "complete_myrmex_state_run", return_value=True)]
        with ExitStack() as stack:
            for context in contexts: stack.enter_context(context)
            self.assertTrue(head.CampaignSupervisor().run_work_unit("camp-F", {"repository_root":str(root), "branch":"main", "status":"active"}, wu))
        self.assertEqual(len(created), 6); self.assertEqual([x[0] for x in sequence], ["writer","verifier","remediator","verifier","remediator","verifier"])
        task_ids = []
        for role, attempts, statuses in (("writer", [1], ["completed"]),("verifier", [1,2,3], ["failed","failed","completed"]),("remediator", [1,2], ["completed","completed"])):
            for attempt, status in zip(attempts, statuses):
                op = head.myrmex_task_operation.find_existing_op_for_phase(run, "WU-F", role, attempt); self.assertIsNotNone(op)
                self.assertEqual(op.status, status); self.assertEqual(op.receipt["phase"], "RESULT_RECEIPT_CONFIRMED"); task_ids.append(op.task_id)
        self.assertEqual(len(task_ids), len(set(task_ids)))

    def test_D_no_op_delivery_recovery_uses_durable_writer_or_blocks(self) -> None:
        head = self._production_head(); root = Path(self.tmp_dir) / "noop-D"; root.mkdir()
        candidate, digest, run, cid, wuid = "6" * 40, "d" * 64, "run-D", "camp-D", "WU-D"
        writer = root / cid / wuid; writer.mkdir(parents=True)
        op = self._seed_real_op(head, campaign=cid, wuid=wuid, run=run, role="writer", attempt=1,
            agent="writer", workspace=writer, base_sha=candidate, task_id="task-D-writer", status="completed", diff_digest=digest)
        driver = head.OpenCodeTaskDriver()
        argv = lambda args, **kw: types.SimpleNamespace(stdout=candidate if args[:3] == ["git", "rev-parse", "HEAD"] else "", returncode=0, stderr="")
        with patch.object(head.myrmex_worktree, "_get_worktrees_dir", return_value=root), patch.object(head, "run_argv", side_effect=argv), patch.object(head, "compute_candidate_diff_digest", return_value=digest):
            recovered = driver.recover_writer_receipt({"id":wuid,"campaign_id":cid,"base_sha":candidate,"implementing_agent":"writer","provider":"opencode","model":"default"}, root, run, {})
        no_op = head.deterministic_no_op_receipt(cid, wuid, candidate, candidate, digest)
        persisted = dict(recovered, campaign_id=cid, work_unit_id=wuid, base_sha=candidate, candidate_sha=candidate)
        wu = {"id":wuid,"campaign_id":cid,"objective":"noop","phase":"delivering","status":"active","base_sha":candidate,
              "candidate_sha":candidate,"diff_digest":digest,"no_op_allowed":True,"no_op_receipt":no_op,
              "writer_receipt":persisted,"verifier_receipt":{"status":"PASS","agent":"verifier"},
              "ci_operation":{"status":"pass"},"implementing_agent":"writer","verifying_agent":"verifier","provider":"opencode","model":"default"}
        data = {"repository_root":str(root),"branch":"main","status":"active"}
        contexts = [patch.object(head.CampaignSupervisor, "ensure_myrmex_state_run", return_value=run), patch.object(head.CampaignSupervisor, "resolve_execution_driver", return_value=driver),
                    patch.object(head.CampaignSupervisor, "complete_myrmex_state_run", return_value=True), patch.object(head.CampaignSupervisor, "_transition_wu"), patch.object(head.CampaignSupervisor, "_block_wu"), patch.object(head.CampaignSupervisor, "sync_myrmex_state_phase"),
                     patch.object(head, "run_argv", side_effect=argv), patch.object(head, "compute_candidate_diff_digest", return_value=digest), patch.object(head.myrmex_worktree, "_get_worktrees_dir", return_value=root), patch.object(head.opencode_transport, "create_task"), patch.object(head.subprocess, "run", return_value=types.SimpleNamespace(returncode=0, stdout="", stderr=""))]
        with ExitStack() as stack:
            for context in contexts: stack.enter_context(context)
            self.assertTrue(head.CampaignSupervisor().run_work_unit(cid, data, wu))
        valid_recovered = dict(recovered)
        valid_persisted = dict(persisted)
        for field in ("campaign_id", "work_unit_id", "base_sha", "candidate_sha"):
            for mutation in ("missing", None, "wrong"):
                mutated = dict(valid_persisted)
                if mutation == "missing":
                    mutated.pop(field)
                else:
                    mutated[field] = mutation
                with self.subTest(field=field, mutation=mutation), self.assertRaises(head.ExecutionDriverError):
                    head.CampaignSupervisor._validate_recovered_no_op_writer(
                        mutated, valid_recovered, cid, wuid, candidate, candidate, digest)
        aliased = dict(valid_recovered, driver="opencode")
        with self.assertRaises(head.ExecutionDriverError):
            head.CampaignSupervisor._validate_recovered_no_op_writer(
                valid_persisted, aliased, cid, wuid, candidate, candidate, digest)
        missing = dict(wu, id="WU-D-MISSING", no_op_receipt=head.deterministic_no_op_receipt(cid, "WU-D-MISSING", candidate, candidate, digest), writer_receipt=persisted)
        blocked = head.CampaignSupervisor._block_wu
        with patch.object(head.CampaignSupervisor, "ensure_myrmex_state_run", return_value=run), patch.object(head.CampaignSupervisor, "resolve_execution_driver", return_value=driver), patch.object(head.CampaignSupervisor, "_block_wu") as block, patch.object(head.CampaignSupervisor, "complete_myrmex_state_run", return_value=True), patch.object(head, "run_argv", side_effect=argv), patch.object(head, "compute_candidate_diff_digest", return_value=digest), patch.object(head.myrmex_worktree, "_get_worktrees_dir", return_value=root):
            self.assertFalse(head.CampaignSupervisor().run_work_unit(cid, data, missing)); block.assert_called_once(); self.assertEqual(block.call_args.args[2], "ambiguous_missing_receipt")

    def test_G_running_to_failed_recovery_never_dispatches(self) -> None:
        head = self._production_head(); _, candidate, wt, _, _, _, _, wu = self._production_ops(head)
        for role in ("writer", "verifier", "remediator"):
            with self.subTest(role=role):
                with patch.object(head.myrmex_worktree, "create_wu_worktree", return_value=(wt, {})), patch.object(head.myrmex_worktree, "create_verifier_worktree", return_value=(wt, {})), patch.object(head.myrmex_worktree, "_get_worktrees_dir", return_value=Path(self.tmp_dir)), patch.object(head.opencode_transport, "recover_existing_task", return_value=types.SimpleNamespace(status="running")), patch.object(head.opencode_transport, "wait_task", return_value=types.SimpleNamespace(status="failed")), patch.object(head.opencode_transport, "create_task") as create:
                    op = types.SimpleNamespace(task_id="task", operation_id=f"op-run-WU-{role}-att1", run_id="run", work_unit_id="WU", role=role, campaign_id="camp", agent="writer" if role != "verifier" else "verifier", provider="provider", model="model", workspace=str(wt.resolve()), base_sha=candidate, receipt={"phase":"TASK_INTENT"}, status="intent")
                    with patch.object(head.myrmex_task_operation, "find_existing_op_for_phase", return_value=op):
                        with self.assertRaises(head.ExecutionDriverError):
                            if role == "writer": head.OpenCodeTaskDriver().execute_writer(wu, candidate, Path(self.tmp_dir), "run", {})
                            elif role == "remediator": head.OpenCodeTaskDriver().execute_remediation(wu, [], 1, Path(self.tmp_dir), "run", {})
                            else: head.OpenCodeTaskDriver().execute_verifier(wu, Path(self.tmp_dir), "run", {}, 1)
                    create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
