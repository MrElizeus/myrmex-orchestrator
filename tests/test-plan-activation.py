#!/usr/bin/env python3
"""P1-012 governed plan activation authority, recovery, replay, and concurrency."""
from __future__ import annotations

import copy
import json
import os
import pathlib
import runpy
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN = ROOT / "bin/myrmex-campaign"
sys.path.insert(0, str(ROOT / "scripts"))
import myrmex_dag_validate as dag_validate  # noqa: E402
import myrmex_plan_critic as critic  # noqa: E402
import myrmex_plan_revision as activation  # noqa: E402
import myrmex_plan_store as plan_store  # noqa: E402


def command(args, cwd=None, ok=True):
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if ok and proc.returncode != 0:
        raise AssertionError(proc.stdout + proc.stderr)
    if not ok and proc.returncode == 0:
        raise AssertionError("command unexpectedly succeeded: " + " ".join(map(str, args)))
    return proc


def run_campaign(args, state_home, ok=True):
    env = dict(os.environ, XDG_STATE_HOME=str(state_home), PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run([sys.executable, str(BIN), *args], capture_output=True, text=True, env=env)
    if ok and proc.returncode != 0:
        raise AssertionError(proc.stdout + proc.stderr)
    if not ok and proc.returncode == 0:
        raise AssertionError("campaign command unexpectedly succeeded: " + " ".join(args))
    return proc


critic_fixtures = runpy.run_path(str(ROOT / "tests/test-plan-critic.py"))
fixture = critic_fixtures["fixture"]
make_review = critic_fixtures["make_review"]
GATE = {"gate_id": "gate-plan-activation", "decision_type": "approve", "reason": "critical activation", "required_before": "plan_activation"}
ACTIVATED_AT = "2026-08-10T04:30:00+00:00"


def make_authority(plan_revision_id, role="primary_orchestrator", subject="primary", expires="2026-08-10T05:00:00+00:00"):
    body = {
        "schema": activation.AUTHORITY_SCHEMA, "role": role, "subject": subject,
        "plan_revision_id": plan_revision_id, "scope": "plan_activation_only",
        "granted_at": "2026-08-10T04:00:00+00:00", "expires_at": expires,
    }
    digest = activation._sha(body)
    return {**body, "authority_id": "auth_" + digest, "authority_digest": digest}


def make_decision(plan_revision_id, expires="2026-08-10T05:00:00+00:00"):
    body = {
        "schema": activation.DECISION_SCHEMA, "gate_id": GATE["gate_id"],
        "plan_revision_id": plan_revision_id, "outcome": "APPROVED", "decided_by": "operator",
        "decided_at": "2026-08-10T04:05:00+00:00", "expires_at": expires,
    }
    digest = activation._sha(body)
    return {**body, "decision_id": "hdec_" + digest, "decision_digest": digest}


def prepare(root: pathlib.Path, suffix: str):
    repo = root / f"repo-{suffix}"; repo.mkdir()
    command(["git", "init", "-b", "main"], repo)
    (repo / "README.md").write_text("activation fixture\n", encoding="utf-8")
    command(["git", "add", "README.md"], repo)
    command(["git", "-c", "user.name=Myrmex Test", "-c", "user.email=test@example.invalid", "commit", "-m", "test: activation baseline"], repo)
    base_sha = command(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    source_root, campaign_id, planning_request_id, planner_task_id, proposed = fixture(
        suffix, base_sha=base_sha, repository_root=str(repo), human_gates=[GATE],
    )
    prepared = critic.prepare_critic_task(
        source_root, campaign_id, 1, f"review-{suffix}", f"task-critic-{suffix}",
        planning_request_id, planner_task_id, proposed["record_id"], "2026-08-10T04:00:00+00:00",
    )
    review = make_review(prepared["task_intent"])
    review["created_at"] = "2026-08-10T04:00:00+00:00"
    review["review_digest"] = critic._sha({key: value for key, value in review.items() if key != "review_digest"})
    critic.record_review(source_root, campaign_id, 1, f"review-{suffix}", f"task-critic-{suffix}", review)
    state_home = root / f"state-{suffix}"; state_home.mkdir()
    run_campaign(["init", "--id", campaign_id, "--title", "Activation", "--repo-root", str(repo)], state_home)
    campaign_dir = state_home / "myrmex/campaigns" / campaign_id
    shutil.copytree(source_root / "intelligence", campaign_dir / "intelligence", dirs_exist_ok=True)
    compiled = json.loads(run_campaign([
        "plan-compile-apply", campaign_id, "--plan-revision-id", proposed["plan_revision_id"], "--expect-revision", "1",
    ], state_home).stdout)
    assert compiled["status"] == "APPLIED" and compiled["campaign_revision_after"] == 2
    dag_receipt = json.loads(run_campaign([
        "dag", campaign_id, "--plan-revision-id", proposed["plan_revision_id"], "--expect-revision", "2",
    ], state_home).stdout)
    assert dag_receipt["status"] == "PASS"
    return {
        "repo": repo, "base_sha": base_sha, "state_home": state_home, "campaign_dir": campaign_dir,
        "campaign_id": campaign_id, "proposed": proposed, "review": review, "dag": dag_receipt,
    }


def activate_args(ctx, request_id, authority_file, decision_file, dag_file=None, expect_revision="2", review_digest=None):
    return [
        "plan-activate", ctx["campaign_id"], "--request-id", request_id,
        "--plan-revision-id", ctx["proposed"]["plan_revision_id"],
        "--reviewed-record-id", ctx["reviewed_record_id"],
        "--review-digest", review_digest or ctx["review"]["review_digest"],
        "--dag-validation-json", str(dag_file or ctx["dag_file"]),
        "--authority-json", str(authority_file), "--human-decisions-json", str(decision_file),
        "--activated-at", ACTIVATED_AT, "--expect-revision", expect_revision,
    ]


with tempfile.TemporaryDirectory(prefix="myrmex-p1012-") as td:
    root = pathlib.Path(td)
    ctx = prepare(root, "actmain")
    reviewed = plan_store.get_plan_head(ctx["campaign_dir"], ctx["campaign_id"], ctx["proposed"]["plan_revision_id"])
    assert reviewed["lifecycle_status"] == "reviewed"
    ctx["reviewed_record_id"] = reviewed["record_id"]
    authority = make_authority(reviewed["plan_revision_id"])
    decisions = [make_decision(reviewed["plan_revision_id"])]
    ctx["dag_file"] = root / "dag.json"; ctx["dag_file"].write_text(json.dumps(ctx["dag"]), encoding="utf-8")
    authority_file = root / "authority.json"; authority_file.write_text(json.dumps(authority), encoding="utf-8")
    decision_file = root / "decisions.json"; decision_file.write_text(json.dumps(decisions), encoding="utf-8")

    # The generic P1-007 API still cannot construct a new active record.
    validated_preview = plan_store.build_lifecycle_record(reviewed, "validated", ACTIVATED_AT)
    try:
        plan_store.build_lifecycle_record(validated_preview, "active", ACTIVATED_AT)
    except plan_store.PlanActivationAuthorityRequired:
        pass
    else:
        raise AssertionError("PlanActivationAuthorityRequired was weakened")

    campaign_file = ctx["campaign_dir"] / "campaign.json"
    events_file = ctx["campaign_dir"] / "events.jsonl"
    campaign_before, events_before = campaign_file.read_bytes(), events_file.read_bytes()

    stale = run_campaign(activate_args(ctx, "activation-stale", authority_file, decision_file, expect_revision="1"), ctx["state_home"], ok=False)
    assert "stale" in json.loads(stale.stdout)["error"].lower()

    bad_authority = make_authority(reviewed["plan_revision_id"], role="planner", subject="planner-task")
    bad_authority_file = root / "bad-authority.json"; bad_authority_file.write_text(json.dumps(bad_authority), encoding="utf-8")
    unauthorized = run_campaign(activate_args(ctx, "activation-unauthorized", bad_authority_file, decision_file), ctx["state_home"], ok=False)
    assert "cannot activate" in json.loads(unauthorized.stdout)["error"]
    critic_authority = make_authority(reviewed["plan_revision_id"], role="critic", subject="critic-task")
    critic_authority_file = root / "critic-authority.json"; critic_authority_file.write_text(json.dumps(critic_authority), encoding="utf-8")
    critic_denied = run_campaign(activate_args(ctx, "activation-critic-unauthorized", critic_authority_file, decision_file), ctx["state_home"], ok=False)
    assert "cannot activate" in json.loads(critic_denied.stdout)["error"]
    assert plan_store.get_plan_head(ctx["campaign_dir"], ctx["campaign_id"], reviewed["plan_revision_id"])["lifecycle_status"] == "reviewed"

    expired_authority = make_authority(reviewed["plan_revision_id"], expires="2026-08-10T04:20:00+00:00")
    expired_authority_file = root / "expired-authority.json"; expired_authority_file.write_text(json.dumps(expired_authority), encoding="utf-8")
    expired_auth = run_campaign(activate_args(ctx, "activation-expired-authority", expired_authority_file, decision_file), ctx["state_home"], ok=False)
    assert "not valid" in json.loads(expired_auth.stdout)["error"]

    empty_decisions = root / "empty-decisions.json"; empty_decisions.write_text("[]\n", encoding="utf-8")
    missing = run_campaign(activate_args(ctx, "activation-missing-decision", authority_file, empty_decisions), ctx["state_home"], ok=False)
    assert "missing" in json.loads(missing.stdout)["error"].lower()

    expired_file = root / "expired-decisions.json"
    expired_file.write_text(json.dumps([make_decision(reviewed["plan_revision_id"], expires="2026-08-10T04:20:00+00:00")]), encoding="utf-8")
    expired = run_campaign(activate_args(ctx, "activation-expired", authority_file, expired_file), ctx["state_home"], ok=False)
    assert "expired" in json.loads(expired.stdout)["error"].lower()

    wrong_dag = copy.deepcopy(ctx["dag"]); wrong_dag["graph_digest"] = "0" * 64
    wrong_body = {key: value for key, value in wrong_dag.items() if key not in {"validation_id", "validation_digest"}}
    wrong_dag["validation_digest"] = dag_validate._sha(wrong_body); wrong_dag["validation_id"] = "dagval_" + wrong_dag["validation_digest"]
    wrong_dag_file = root / "wrong-dag.json"; wrong_dag_file.write_text(json.dumps(wrong_dag), encoding="utf-8")
    wrong = run_campaign(activate_args(ctx, "activation-wrong-dag", authority_file, decision_file, dag_file=wrong_dag_file), ctx["state_home"], ok=False)
    assert "does not match" in json.loads(wrong.stdout)["error"].lower()

    wrong_review = run_campaign(activate_args(ctx, "activation-wrong-review", authority_file, decision_file, review_digest="f" * 64), ctx["state_home"], ok=False)
    assert "not bound" in json.loads(wrong_review.stdout)["error"].lower()
    assert campaign_file.read_bytes() == campaign_before and events_file.read_bytes() == events_before

    call = dict(
        campaign_dir=ctx["campaign_dir"], campaign_id=ctx["campaign_id"], expected_campaign_revision=2,
        request_id="activation-main-001", plan_revision_id=reviewed["plan_revision_id"],
        expected_reviewed_record_id=reviewed["record_id"], expected_review_digest=ctx["review"]["review_digest"],
        dag_receipt=ctx["dag"], activation_authority=authority, human_decisions=decisions, activated_at=ACTIVATED_AT,
    )

    # Crash after validated persistence but before active persistence.
    original_active_store = activation.plan_store.store_activated_plan_record
    activation.plan_store.store_activated_plan_record = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("injected before active"))
    try:
        activation.activate_plan(**call)
    except RuntimeError as error:
        assert "before active" in str(error)
    else:
        raise AssertionError("active-store interruption was not injected")
    finally:
        activation.plan_store.store_activated_plan_record = original_active_store
    assert plan_store.get_plan_head(ctx["campaign_dir"], ctx["campaign_id"], reviewed["plan_revision_id"])["lifecycle_status"] == "validated"

    # Recovery reaches active, then crashes before the active projection.
    original_put = activation.intel.put_artifact
    injected = {"done": False}
    def fault_put(campaign_dir, campaign_id, revision, kind, artifact_id, payload):
        if artifact_id.startswith("plan-activation/projection/") and not injected["done"]:
            injected["done"] = True
            raise RuntimeError("injected after active")
        return original_put(campaign_dir, campaign_id, revision, kind, artifact_id, payload)
    activation.intel.put_artifact = fault_put
    try:
        activation.activate_plan(**call)
    except activation.PlanActivationBackendUnavailable as error:
        assert "persistence" in str(error)
    else:
        raise AssertionError("post-active interruption was not injected")
    finally:
        activation.intel.put_artifact = original_put
    assert plan_store.get_plan_head(ctx["campaign_dir"], ctx["campaign_id"], reviewed["plan_revision_id"])["lifecycle_status"] == "active"

    receipt = activation.activate_plan(**call)
    activation.validate_activation_receipt(receipt)
    import jsonschema
    jsonschema.validate(receipt, json.loads((ROOT / "contracts/plan-activation-v1.schema.json").read_text(encoding="utf-8")))
    chain = plan_store.get_plan_chain(ctx["campaign_dir"], ctx["campaign_id"], reviewed["plan_revision_id"])
    assert [item["lifecycle_status"] for item in chain] == ["proposed", "reviewed", "validated", "active"]
    assert len(activation._active_heads(ctx["campaign_dir"], ctx["campaign_id"])) == 1
    assert receipt["authority"]["repository_write"] is False and receipt["active_record_id"] == chain[-1]["record_id"]
    assert campaign_file.read_bytes() == campaign_before and events_file.read_bytes() == events_before
    assert command(["git", "rev-parse", "HEAD"], ctx["repo"]).stdout.strip() == ctx["base_sha"]
    assert command(["git", "status", "--porcelain"], ctx["repo"]).stdout == ""

    artifact_bytes = {str(path.relative_to(ctx["campaign_dir"])): path.read_bytes() for path in (ctx["campaign_dir"] / "intelligence").rglob("*.json")}
    replay = activation.activate_plan(**call)
    replay_bytes = {str(path.relative_to(ctx["campaign_dir"])): path.read_bytes() for path in (ctx["campaign_dir"] / "intelligence").rglob("*.json")}
    assert replay == receipt, (replay, receipt)
    assert replay_bytes == artifact_bytes, [key for key in replay_bytes if replay_bytes[key] != artifact_bytes.get(key)]

    cli_replay = json.loads(run_campaign(activate_args(ctx, "activation-main-001", authority_file, decision_file), ctx["state_home"]).stdout)
    assert cli_replay == receipt

    changed_authority = make_authority(reviewed["plan_revision_id"], subject="different-primary")
    changed_authority_file = root / "changed-authority.json"; changed_authority_file.write_text(json.dumps(changed_authority), encoding="utf-8")
    conflict = run_campaign(activate_args(ctx, "activation-main-001", changed_authority_file, decision_file), ctx["state_home"], ok=False)
    assert "conflict" in json.loads(conflict.stdout)["error"].lower()
    duplicate = run_campaign(activate_args(ctx, "activation-second-request", authority_file, decision_file), ctx["state_home"], ok=False)
    assert "current" in json.loads(duplicate.stdout)["error"].lower() or "match" in json.loads(duplicate.stdout)["error"].lower()
    assert len(activation._active_heads(ctx["campaign_dir"], ctx["campaign_id"])) == 1

    # A confirmed exact replay returns the immutable receipt even after later
    # repository/source movement; stale checks govern pre-effect activation,
    # not historical receipt recovery.
    (ctx["repo"] / "new.txt").write_text("new head\n", encoding="utf-8")
    command(["git", "add", "new.txt"], ctx["repo"])
    command(["git", "-c", "user.name=Myrmex Test", "-c", "user.email=test@example.invalid", "commit", "-m", "test: stale head"], ctx["repo"])
    assert activation.activate_plan(**call) == receipt
    command(["git", "checkout", "--detach", ctx["base_sha"]], ctx["repo"])
    assert activation.activate_plan(**call) == receipt

    # A newer normalized snapshot makes the source binding stale.
    activation.intel.put_artifact(
        ctx["campaign_dir"], ctx["campaign_id"], 2, "backlog",
        "normalized-backlog/snapshot/blsnaprec_" + "9" * 64,
        {"schema": "fixture.newer-backlog/v1", "marker": "newer"},
    )
    assert activation.activate_plan(**call) == receipt

    # Fresh activations still fail closed when repository or source identities
    # become stale before the active lifecycle effect.
    stale_ctx = prepare(root, "actstale")
    stale_reviewed = plan_store.get_plan_head(stale_ctx["campaign_dir"], stale_ctx["campaign_id"], stale_ctx["proposed"]["plan_revision_id"])
    stale_authority = make_authority(stale_reviewed["plan_revision_id"])
    stale_decisions = [make_decision(stale_reviewed["plan_revision_id"])]
    stale_call = dict(
        campaign_dir=stale_ctx["campaign_dir"], campaign_id=stale_ctx["campaign_id"], expected_campaign_revision=2,
        request_id="activation-stale-head", plan_revision_id=stale_reviewed["plan_revision_id"],
        expected_reviewed_record_id=stale_reviewed["record_id"], expected_review_digest=stale_ctx["review"]["review_digest"],
        dag_receipt=stale_ctx["dag"], activation_authority=stale_authority, human_decisions=stale_decisions, activated_at=ACTIVATED_AT,
    )
    (stale_ctx["repo"] / "new.txt").write_text("new head\n", encoding="utf-8")
    command(["git", "add", "new.txt"], stale_ctx["repo"])
    command(["git", "-c", "user.name=Myrmex Test", "-c", "user.email=test@example.invalid", "commit", "-m", "test: stale before activation"], stale_ctx["repo"])
    try:
        activation.activate_plan(**stale_call)
    except activation.PlanActivationStale as error:
        assert "HEAD" in str(error)
    else:
        raise AssertionError("fresh activation accepted stale repository HEAD")
    command(["git", "checkout", "--detach", stale_ctx["base_sha"]], stale_ctx["repo"])
    activation.intel.put_artifact(
        stale_ctx["campaign_dir"], stale_ctx["campaign_id"], 2, "backlog",
        "normalized-backlog/snapshot/blsnaprec_" + "8" * 64,
        {"schema": "fixture.newer-backlog/v1", "marker": "newer"},
    )
    stale_call["request_id"] = "activation-stale-source"
    try:
        activation.activate_plan(**stale_call)
    except activation.PlanActivationStale as error:
        assert "newer" in str(error)
    else:
        raise AssertionError("fresh activation accepted stale source snapshot")

    # Fresh concurrent first activation is serialized and both callers recover
    # one identical receipt, with no lifecycle fork or duplicate active record.
    concurrent = prepare(root, "actconcurrent")
    concurrent_reviewed = plan_store.get_plan_head(concurrent["campaign_dir"], concurrent["campaign_id"], concurrent["proposed"]["plan_revision_id"])
    concurrent["reviewed_record_id"] = concurrent_reviewed["record_id"]
    concurrent["dag_file"] = root / "dag-concurrent.json"; concurrent["dag_file"].write_text(json.dumps(concurrent["dag"]), encoding="utf-8")
    concurrent_authority = make_authority(concurrent_reviewed["plan_revision_id"])
    concurrent_decisions = [make_decision(concurrent_reviewed["plan_revision_id"])]
    concurrent_authority_file = root / "authority-concurrent.json"; concurrent_authority_file.write_text(json.dumps(concurrent_authority), encoding="utf-8")
    concurrent_decision_file = root / "decisions-concurrent.json"; concurrent_decision_file.write_text(json.dumps(concurrent_decisions), encoding="utf-8")
    concurrent_args = activate_args(concurrent, "activation-concurrent-001", concurrent_authority_file, concurrent_decision_file)
    env = dict(os.environ, XDG_STATE_HOME=str(concurrent["state_home"]), PYTHONDONTWRITEBYTECODE="1")
    processes = [subprocess.Popen([sys.executable, str(BIN), *concurrent_args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env) for _ in range(2)]
    outcomes = [process.communicate(timeout=60) + (process.returncode,) for process in processes]
    assert all(code == 0 for _, _, code in outcomes), outcomes
    concurrent_receipts = [json.loads(stdout) for stdout, _, _ in outcomes]
    assert concurrent_receipts[0] == concurrent_receipts[1]
    concurrent_chain = plan_store.get_plan_chain(concurrent["campaign_dir"], concurrent["campaign_id"], concurrent_reviewed["plan_revision_id"])
    assert [item["lifecycle_status"] for item in concurrent_chain] == ["proposed", "reviewed", "validated", "active"]
    assert len(activation._active_heads(concurrent["campaign_dir"], concurrent["campaign_id"])) == 1

    # Envelope-internally-valid projection corruption is rejected on replay;
    # immutable receipt, projection, and active lifecycle identities are all
    # revalidated rather than trusted from the projection index.
    concurrent_receipt = concurrent_receipts[0]
    projection_id = concurrent_receipt["active_projection_artifact_id"]
    projection_path = concurrent["campaign_dir"] / "intelligence/artifacts" / (activation.intel.artifact_storage_key(projection_id) + ".json")
    envelope = json.loads(projection_path.read_text(encoding="utf-8"))
    envelope["payload"]["graph_digest"] = "0" * 64
    projection_body = {key: value for key, value in envelope["payload"].items() if key != "projection_digest"}
    envelope["payload"]["projection_digest"] = activation._sha(projection_body)
    envelope["payload_digest"] = activation.intel.compute_payload_digest(envelope["payload"])
    envelope["artifact_digest"] = activation.intel.compute_artifact_digest(
        concurrent["campaign_id"], envelope["kind"], envelope["artifact_id"], envelope["payload"],
    )
    projection_path.write_text(json.dumps(envelope, indent=2) + "\n", encoding="utf-8")
    corrupted = run_campaign(concurrent_args, concurrent["state_home"], ok=False)
    assert "projection chain is corrupt" in json.loads(corrupted.stdout)["error"]

print("plan activation: authority, exact preconditions, crash/replay/stale safety, no-fork concurrency PASS")
