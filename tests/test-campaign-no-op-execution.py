#!/usr/bin/env python3
"""Focused review-only no-op execution and replay coverage."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import importlib.machinery
import importlib.util
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN = ROOT / "bin/myrmex-campaign"
HEAD = ROOT / "bin/myrmex-head"
loader = importlib.machinery.SourceFileLoader("myrmex_head_noop", str(HEAD))
spec = importlib.util.spec_from_loader(loader.name, loader)
head_runtime = importlib.util.module_from_spec(spec)
loader.exec_module(head_runtime)


def run(args, state, cwd=None):
    env = dict(os.environ, XDG_STATE_HOME=str(state), PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run([sys.executable, *args], cwd=cwd, env=env, capture_output=True, text=True)


with tempfile.TemporaryDirectory() as repo, tempfile.TemporaryDirectory() as state:
    repo_path, state_path = Path(repo), Path(state)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    (repo_path / "README.md").write_text("noop\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=repo, check=True, capture_output=True)
    before = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    before_count = int(subprocess.check_output(["git", "rev-list", "--count", "HEAD"], cwd=repo, text=True))
    cid = "camp-no-op-focused"
    assert run([str(CAMPAIGN), "init", "--id", cid, "--title", "noop", "--objective", "noop", "--repo-root", str(repo_path)], state).returncode == 0
    verify = f'{sys.executable} -c "print(\'independent verifier\')"'
    ci = f'{sys.executable} -c "print(\'ci\')"'
    add = run([str(CAMPAIGN), "wu-add", cid, "--wu-id", "WU-NOOP", "--objective", "review", "--no-op-allowed", "--impl-cmd", f'{sys.executable} -c "pass"', "--verify-cmd", verify, "--ci-cmd", ci], state)
    assert add.returncode == 0, add.stderr
    counters = {"writer": 0, "verifier": 0, "ci": 0, "commit": 0, "evidence": 0, "completion": 0}
    for method_name, counter_name in (("execute_writer", "writer"), ("execute_verifier", "verifier"), ("execute_ci", "ci")):
        original = getattr(head_runtime.FixtureCommandDriver, method_name)
        def counted(self, *args, _original=original, _counter=counter_name, **kwargs):
            counters[_counter] += 1
            return _original(self, *args, **kwargs)
        setattr(head_runtime.FixtureCommandDriver, method_name, counted)
    original_evidence = head_runtime.CampaignSupervisor.complete_myrmex_state_run
    def counted_completion(self, *args, **kwargs):
        counters["completion"] += 1
        return original_evidence(self, *args, **kwargs)
    head_runtime.CampaignSupervisor.complete_myrmex_state_run = counted_completion
    crashed = {"value": False}
    class ProbeSupervisor(head_runtime.CampaignSupervisor):
        def _transition_wu(self, *args, **kwargs):
            result = super()._transition_wu(*args, **kwargs)
            if kwargs.get("phase") == "delivering" and not crashed["value"]:
                crashed["value"] = True
                raise RuntimeError("controlled crash after committing receipt")
            return result
        def produce_governed_commit(self, *args, **kwargs):
            counters["commit"] += 1
            raise AssertionError("no-op must not call governed commit")
        def _block_wu(self, *args, **kwargs):
            raise AssertionError("controlled crash must not block")
    supervisor = ProbeSupervisor(campaign_id=cid, once=True, state_home=str(state), allow_fixture_driver=True)
    try:
        supervisor.process_campaign(cid)
    except RuntimeError as error:
        assert "controlled crash" in str(error)
    else:
        raise AssertionError("controlled crash was not injected")
    crashed_data = json.loads(run([str(CAMPAIGN), "show", cid, "--json"], state).stdout)
    assert crashed_data["work_units"][0]["phase"] == "delivering"
    assert crashed_data["work_units"][0]["no_op_receipt"]
    resume = ProbeSupervisor(campaign_id=cid, once=True, state_home=str(state), allow_fixture_driver=True)
    resumed_data = resume.get_campaign_data(cid)
    assert resume.run_work_unit(cid, resumed_data, resumed_data["work_units"][0]) is True
    data = json.loads(run([str(CAMPAIGN), "show", cid, "--json"], state).stdout)
    wu = data["work_units"][0]
    assert wu["status"] == "completed"
    assert wu["candidate_sha"] == before and wu["commit_sha"] is None, data
    receipt = wu["no_op_receipt"]
    assert receipt["schema"] == "myrmex.no-op-receipt/v1" and receipt["modified_paths"] == []
    assert wu["commit_receipt"] is None and wu["evidence"]["no_op_receipt"] == receipt
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip() == before
    assert int(subprocess.check_output(["git", "rev-list", "--count", "HEAD"], cwd=repo, text=True)) == before_count
    replay = run([str(HEAD), "--once", "--allow-fixture-driver", "--campaign-id", cid], state)
    assert replay.returncode == 0, replay.stdout + replay.stderr
    assert json.loads(run([str(CAMPAIGN), "show", cid, "--json"], state).stdout)["work_units"][0]["status"] == "completed"
    assert counters["commit"] == 0
    assert counters["writer"] == 1 and counters["verifier"] == 1 and counters["ci"] == 1
    assert counters["completion"] == 1
    tampered = json.loads(run([str(CAMPAIGN), "show", cid, "--json"], state).stdout)
    tampered["work_units"][0]["phase"] = "delivering"
    tampered["work_units"][0]["status"] = "active"
    tampered["work_units"][0]["no_op_receipt"]["candidate_sha"] = "0" * 40
    campaign_file = state_path / "myrmex" / "campaigns" / cid / "campaign.json"
    campaign_file.write_text(json.dumps(tampered, indent=2) + "\n")
    try:
        head_runtime.validate_no_op_receipt(
            tampered["work_units"][0]["no_op_receipt"], cid, "WU-NOOP", before, before,
            tampered["work_units"][0]["diff_digest"], repo_path,
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("tampered no-op receipt accepted")

    # Production no-op recovery must use the real TaskOperationV1 ledger,
    # rather than trusting the campaign writer projection.  The ledger file
    # and all lifecycle transitions below are real; only git/worktree evidence
    # is isolated for this focused test.
    writer_op = head_runtime.myrmex_task_operation.create_task_intent(
        campaign_id=cid, work_unit_id="WU-DURABLE", run_id="run-durable",
        role="writer", agent="myrmex-worker", workspace=str(repo_path),
        base_sha=before, prompt="durable writer", provider="opencode",
        model="default", attempt=1,
    )
    head_runtime.myrmex_task_operation.record_task_observed(writer_op, "task-durable")
    head_runtime.myrmex_task_operation.record_task_terminal(
        writer_op, "completed", "{}", diff_digest="d" * 64,
    )
    head_runtime.myrmex_task_operation.record_receipt_confirmed(writer_op)
    durable_wu = {
        "id": "WU-DURABLE", "campaign_id": cid, "base_sha": before,
        "implementing_agent": "myrmex-worker", "provider": "opencode",
        "model": "default",
    }
    driver = head_runtime.OpenCodeTaskDriver()
    with patch.object(head_runtime.myrmex_worktree, "_get_worktrees_dir", return_value=state_path / "missing"), \
         patch.object(head_runtime, "run_argv", side_effect=lambda args, **kw: type("Result", (), {"stdout": "", "returncode": 0, "stderr": ""})()), \
         patch.object(head_runtime, "compute_candidate_diff_digest", return_value="d" * 64):
        recovered_writer = driver.recover_writer_receipt(durable_wu, repo_path, "run-durable", {})
    assert recovered_writer["task_id"] == "task-durable"
    assert (state_path / "myrmex/task-operations/op-run-durable-WU-DURABLE-writer-att1.json").is_file()
    missing_wu = dict(durable_wu, id="WU-MISSING")
    try:
        driver.recover_writer_receipt(missing_wu, repo_path, "run-durable", {})
    except head_runtime.ExecutionDriverError:
        pass
    else:
        raise AssertionError("missing durable writer operation was accepted")

print("campaign no-op execution: receipt, unchanged HEAD, no commit, and terminal replay PASS")
