#!/usr/bin/env python3
"""P1-007 immutable plan revision store tests.

Builds deterministic myrmex.plan-revision/v1 records, persists them through
the P1-007 store, and verifies: contract parity, deterministic digests/IDs,
linear lifecycle chains with exact previous_record_id links, seven-state
transition matrix, terminal states, active-authority denial, fork/stale-head
rejection, duplicate-root rejection, crash/lost-ack recovery, projection
recovery, plan-lock concurrency, campaign/repository immutability, and no
WU/DAG/activation/commit authority.
"""
from __future__ import annotations

import copy
import json
import multiprocessing
import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import myrmex_plan_store as store  # noqa: E402
import myrmex_campaign_intelligence as intel  # noqa: E402

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


def make_campaign(tmp: pathlib.Path) -> tuple[pathlib.Path, str, pathlib.Path]:
    repo = tmp / "repo"
    repo.mkdir()
    state_home = tmp / "state"
    state_home.mkdir()
    env = dict(os.environ, XDG_STATE_HOME=str(state_home))
    proc = subprocess.run(
        [str(ROOT / "bin" / "myrmex-campaign"), "init", "--id", "camp-p1007-test", "--title", "P1-007", "--objective", "plan store", "--repo-root", str(repo)],
        capture_output=True, text=True, env=env,
    )
    if proc.returncode != 0:
        raise AssertionError(f"campaign init failed: {proc.stdout} {proc.stderr}")
    campaign_dir = next(state_home.rglob("campaign.json")).parent
    return repo, "camp-p1007-test", campaign_dir


def make_wu(wu_id: str, title: str, dependencies: list[str] | None = None) -> dict:
    return {
        "id": wu_id,
        "objective": title,
        "non_goals": ["none"],
        "dependencies": dependencies or [],
        "scope": {"allowed_paths": ["src/"], "forbidden_paths": [".env"]},
        "acceptance_criteria": ["AC1"],
        "verification": {"commands": ["python3 -m pytest"], "manual_checks": [], "discover_when_missing": True},
        "risk_class": "bounded",
        "required_route": "frontier-gated",
        "human_gates": [{"gate_id": "g1", "decision_type": "product", "reason": "needs approval", "required_before": "plan_activation"}],
        "required_evidence": ["test output"],
        "terminal_gate": "G-TEST",
    }


def make_record(campaign_id: str, *, status: str = "proposed", prev_record: dict | None = None,
                created_at: str = "2026-08-07T00:00:00+00:00", plan_content_delta: str = "A") -> dict:
    work_units = [make_wu("WU-P1-001", f"WU {plan_content_delta}")]
    edges = [] if not work_units[0]["dependencies"] else [["WU-P1-001", "WU-P1-002"], ]
    base = {
        "schema": "myrmex.plan-revision/v1",
        "campaign_id": campaign_id,
        "objective_id": "obj-p1-007",
        "planning_request_id": "req-p1-007",
        "base_sha": "0" * 40,
        "parent_revision": None,
        "input_digests": [{"kind": "source", "identity": f"src-{plan_content_delta}", "sha256": "a" * 64}],
        "assumptions": [{"assumption_id": "as1", "statement": f"S {plan_content_delta}", "evidence_status": "supported", "impact": "low", "resolution_gate": None}],
        "work_units": work_units,
        "edges": edges,
        "lifecycle_status": status,
        "previous_record_id": prev_record["record_id"] if prev_record else None,
        "created_at": created_at,
    }
    base["plan_digest"] = store.compute_plan_digest(base)
    base["plan_revision_id"] = store.derive_plan_revision_id(base["plan_digest"])
    base["record_digest"] = store.compute_record_digest(base)
    base["record_id"] = store.derive_record_id(base["record_digest"])
    return base


def count_plan_artifacts(campaign_dir: pathlib.Path) -> int:
    artifacts = campaign_dir / "intelligence" / "artifacts"
    if not artifacts.exists():
        return 0
    n = 0
    for p in artifacts.glob("*.json"):
        try:
            if "plan-revision/record/" in p.read_text(errors="ignore"):
                n += 1
        except Exception:
            pass
    return n


def worker_propose_review(campaign_dir, campaign_id, record) -> None:
    try:
        store.store_plan_record(campaign_dir, campaign_id, 1, record)
    except Exception:
        pass


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="myrmex-p1007-") as td:
        tmp = pathlib.Path(td)
        repo, campaign_id, campaign_dir = make_campaign(tmp)

        # Positive proposed store
        proposed = make_record(campaign_id)
        store.validate_plan_revision_record(proposed)
        r1 = store.store_plan_record(campaign_dir, campaign_id, 1, proposed)
        check(r1["status"] == "created", "proposed created")
        check(r1["record_id"] == proposed["record_id"], "record id matches")
        check(count_plan_artifacts(campaign_dir) == 1, "one plan artifact")
        # JSON schema validation
        import jsonschema
        schema = json.loads((ROOT / "contracts" / "plan-revision-v1.schema.json").read_text())
        jsonschema.validate(proposed, schema)

        # Exact replay
        r1b = store.store_plan_record(campaign_dir, campaign_id, 1, proposed)
        check(r1b["status"] == "reused", "proposed replay reused")
        check(count_plan_artifacts(campaign_dir) == 1, "no extra artifact on replay")

        # proposed -> reviewed
        reviewed = store.build_lifecycle_record(proposed, "reviewed", "2026-08-07T01:00:00+00:00")
        check(reviewed["previous_record_id"] == proposed["record_id"], "reviewed links proposed")
        check(reviewed["plan_digest"] == proposed["plan_digest"], "reviewed same plan digest")
        check(reviewed["record_id"] != proposed["record_id"], "reviewed different record id")
        r2 = store.store_plan_record(campaign_dir, campaign_id, 1, reviewed)
        check(r2["status"] == "created", "reviewed created")
        check(r2["chain_length"] == 2, "chain length 2")
        jsonschema.validate(reviewed, schema)

        # reviewed -> validated
        validated = store.build_lifecycle_record(reviewed, "validated", "2026-08-07T02:00:00+00:00")
        r3 = store.store_plan_record(campaign_dir, campaign_id, 1, validated)
        check(r3["chain_length"] == 3, "chain length 3")
        check(r3["head_record_id"] == validated["record_id"], "head is validated")
        jsonschema.validate(validated, schema)

        # Active authority denial
        check(store.validate_lifecycle_transition("validated", "active") is True, "validated->active legal in model")
        active = make_record(campaign_id, status="active", prev_record=validated, created_at="2026-08-07T03:00:00+00:00")
        active["plan_digest"] = validated["plan_digest"]
        active["plan_revision_id"] = validated["plan_revision_id"]
        active["record_digest"] = store.compute_record_digest(active)
        active["record_id"] = store.derive_record_id(active["record_digest"])
        store.validate_plan_revision_record(active)
        jsonschema.validate(active, schema)
        active_before = count_plan_artifacts(campaign_dir)
        try:
            store.store_plan_record(campaign_dir, campaign_id, 1, active)
            check(False, "active creation should require authority")
        except store.PlanActivationAuthorityRequired:
            check(True, "active creation denied")
        check(count_plan_artifacts(campaign_dir) == active_before, "active not persisted")
        # build_lifecycle_record also refuses active
        try:
            store.build_lifecycle_record(validated, "active", "2026-08-07T03:00:00+00:00")
            check(False, "builder should refuse active")
        except store.PlanActivationAuthorityRequired:
            check(True, "builder refuses active")

        # Seven-state transition matrix at pure level
        matrix = {
            "proposed": {"reviewed": True, "validated": False, "rejected": True, "superseded": True, "withdrawn": True},
            "reviewed": {"validated": True, "active": False, "rejected": True, "superseded": True, "withdrawn": True},
            "validated": {"active": True, "rejected": True, "superseded": True, "withdrawn": True},
            "rejected": {"proposed": False, "reviewed": False, "active": False},
            "superseded": {"proposed": False, "reviewed": False},
            "withdrawn": {"proposed": False, "reviewed": False},
        }
        for src, targets in matrix.items():
            for dst, expected in targets.items():
                check(store.validate_lifecycle_transition(src, dst) == expected, f"transition {src}->{dst} == {expected}")

        # Terminal-state persistence on separate revisions + terminal cannot transition
        for terminal_status in ("rejected", "superseded", "withdrawn"):
            rev = make_record(campaign_id, status="proposed", created_at=f"2026-08-07T10:0{['rejected','superseded','withdrawn'].index(terminal_status)}+00:00")
            # ensure distinct revision by distinct content
            rev["assumptions"][0]["statement"] = f"term-{terminal_status}"
            rev["plan_digest"] = store.compute_plan_digest(rev)
            rev["plan_revision_id"] = store.derive_plan_revision_id(rev["plan_digest"])
            rev["record_digest"] = store.compute_record_digest(rev)
            rev["record_id"] = store.derive_record_id(rev["record_digest"])
            store.store_plan_record(campaign_dir, campaign_id, 1, rev)
            term = store.build_lifecycle_record(rev, terminal_status, f"2026-08-07T11:0{['rejected','superseded','withdrawn'].index(terminal_status)}+00:00")
            store.store_plan_record(campaign_dir, campaign_id, 1, term)
            try:
                store.build_lifecycle_record(term, "reviewed", "2026-08-07T12:00:00+00:00")
                check(False, f"{terminal_status} -> reviewed should fail")
            except store.PlanLifecycleInvalid:
                check(True, f"{terminal_status} terminal")

        # Illegal transitions
        for src_state, dst_state in (("proposed", "validated"), ("reviewed", "active"), ("rejected", "reviewed"),
                                     ("superseded", "proposed"), ("withdrawn", "reviewed")):
            check(store.validate_lifecycle_transition(src_state, dst_state) is False, f"illegal {src_state}->{dst_state}")

        # Stale-head/fork: attempt successor from proposed while head is reviewed
        fork = make_record(campaign_id, status="rejected", prev_record=proposed, created_at="2026-08-07T05:00:00+00:00")
        fork["plan_digest"] = proposed["plan_digest"]
        fork["plan_revision_id"] = proposed["plan_revision_id"]
        fork["record_digest"] = store.compute_record_digest(fork)
        fork["record_id"] = store.derive_record_id(fork["record_digest"])
        fork_before = count_plan_artifacts(campaign_dir)
        try:
            store.store_plan_record(campaign_dir, campaign_id, 1, fork)
            check(False, "stale-head fork should fail")
        except (store.PlanLifecycleConflict, store.PlanLifecycleInvalid):
            check(True, "stale-head fork rejected")
        check(count_plan_artifacts(campaign_dir) == fork_before, "fork not persisted")
        dup_root = make_record(campaign_id, status="proposed", created_at="2026-08-07T06:00:00+00:00")
        dup_root["plan_digest"] = proposed["plan_digest"]
        dup_root["plan_revision_id"] = proposed["plan_revision_id"]
        dup_root["record_digest"] = store.compute_record_digest(dup_root)
        dup_root["record_id"] = store.derive_record_id(dup_root["record_digest"])
        try:
            store.store_plan_record(campaign_dir, campaign_id, 1, dup_root)
            check(False, "duplicate root should fail")
        except store.PlanLifecycleInvalid:
            check(True, "duplicate root rejected")

        # Missing previous
        orphan = make_record(campaign_id, status="reviewed", prev_record=None, created_at="2026-08-07T07:00:00+00:00")
        orphan["previous_record_id"] = "planrec_" + "f" * 64
        orphan["record_digest"] = store.compute_record_digest(orphan)
        orphan["record_id"] = store.derive_record_id(orphan["record_digest"])
        try:
            store.store_plan_record(campaign_dir, campaign_id, 1, orphan)
            check(False, "missing previous should fail")
        except store.PlanLifecycleInvalid:
            check(True, "missing previous rejected")

        # Cross-campaign
        cross = make_record("camp-other-test", status="proposed")
        try:
            store.store_plan_record(campaign_dir, campaign_id, 1, cross)
            check(False, "cross-campaign should fail")
        except store.PlanLifecycleInvalid:
            check(True, "cross-campaign rejected")

        # Digest corruption
        for label, mut in (
            ("plan_digest", lambda r: r.__setitem__("plan_digest", "b" * 64)),
            ("record_digest", lambda r: r.__setitem__("record_digest", "b" * 64)),
            ("record_id", lambda r: r.__setitem__("record_id", "planrec_" + "b" * 64)),
        ):
            bad = copy.deepcopy(proposed)
            mut(bad)
            try:
                store.validate_plan_revision_record(bad)
                check(False, f"digest corruption {label} should fail")
            except store.PlanRecordInvalid:
                check(True, f"digest corruption {label} rejected")

        # Structural corruption
        bad_missing = copy.deepcopy(proposed)
        del bad_missing["objective_id"]
        try:
            store.validate_plan_revision_record(bad_missing)
            check(False, "missing field should fail")
        except store.PlanRecordInvalid:
            check(True, "missing field rejected")
        bad_extra = dict(proposed)
        bad_extra["extra"] = 1
        try:
            store.validate_plan_revision_record(bad_extra)
            check(False, "extra field should fail")
        except store.PlanRecordInvalid:
            check(True, "extra field rejected")
        bad_self_dep = copy.deepcopy(proposed)
        bad_self_dep["work_units"] = [make_wu("WU-P1-001", "x", dependencies=["WU-P1-001"])]
        bad_self_dep["edges"] = [["WU-P1-001", "WU-P1-001"]]
        bad_self_dep["plan_digest"] = store.compute_plan_digest(bad_self_dep)
        bad_self_dep["plan_revision_id"] = store.derive_plan_revision_id(bad_self_dep["plan_digest"])
        bad_self_dep["record_digest"] = store.compute_record_digest(bad_self_dep)
        bad_self_dep["record_id"] = store.derive_record_id(bad_self_dep["record_digest"])
        try:
            store.validate_plan_revision_record(bad_self_dep)
            check(False, "self-dependency should fail")
        except store.PlanRecordInvalid:
            check(True, "self-dependency rejected")
        bad_time = copy.deepcopy(proposed)
        bad_time["created_at"] = "2026-08-07T00:00:00"
        bad_time["record_digest"] = store.compute_record_digest(bad_time)
        bad_time["record_id"] = store.derive_record_id(bad_time["record_digest"])
        try:
            store.validate_plan_revision_record(bad_time)
            check(False, "naive timestamp should fail")
        except store.PlanRecordInvalid:
            check(True, "naive timestamp rejected")

        # Safety policy: token in a semantic string
        token_record = make_record(campaign_id, status="proposed", created_at="2026-08-07T08:00:00+00:00")
        token_record["assumptions"][0]["statement"] = "sk-abcdef1234567890"
        token_record["plan_digest"] = store.compute_plan_digest(token_record)
        token_record["plan_revision_id"] = store.derive_plan_revision_id(token_record["plan_digest"])
        token_record["record_digest"] = store.compute_record_digest(token_record)
        token_record["record_id"] = store.derive_record_id(token_record["record_digest"])
        try:
            store.store_plan_record(campaign_dir, campaign_id, 1, token_record)
            check(False, "token-shaped content should be rejected")
        except store.PlanRecordInvalid as exc:
            check("sk-abcdef1234567890" not in str(exc), "secret not echoed")

        # Crash/lost-ack recovery: put writes durably then raises
        crash_record = store.build_lifecycle_record(validated, "superseded", "2026-08-07T09:00:00+00:00")
        orig_put = intel.put_artifact
        injected = {"raised": False}

        def failing_put(*args, **kwargs):
            result = orig_put(*args, **kwargs)
            injected["raised"] = True
            raise RuntimeError("lost ack")
        intel.put_artifact = failing_put
        try:
            try:
                store.store_plan_record(campaign_dir, campaign_id, 1, crash_record)
                check(False, "lost-ack should raise")
            except store.PlanStoreBackendUnavailable:
                check(injected["raised"], "lost-ack raised")
        finally:
            intel.put_artifact = orig_put
        # record durable, retry reuses
        r_crash = store.store_plan_record(campaign_dir, campaign_id, 1, crash_record)
        check(r_crash["status"] == "reused", "lost-ack retry reused")
        check(r_crash["record_id"] == crash_record["record_id"], "lost-ack retry same record")

        # Projection recovery: delete projection, chain still reconstructs
        projection = campaign_dir / "intelligence" / "projection.json"
        if projection.exists():
            projection.unlink()
        head = store.get_plan_head(campaign_dir, campaign_id, proposed["plan_revision_id"])
        check(head["record_id"] == r_crash["head_record_id"], "projection recovery head correct")
        doc = intel.doctor(str(campaign_dir), campaign_id)
        check(doc.get("status") == "healthy", "doctor healthy after projection recovery")

        # Campaign immutability + no WU/DAG
        camp_file = next(campaign_dir.glob("campaign.json"))
        camp_before = camp_file.read_bytes()
        repo_before = {p.relative_to(repo).as_posix(): p.read_bytes() for p in repo.rglob("*") if p.is_file()}
        store.store_plan_record(campaign_dir, campaign_id, 1, proposed)
        camp_after = camp_file.read_bytes()
        repo_after = {p.relative_to(repo).as_posix(): p.read_bytes() for p in repo.rglob("*") if p.is_file()}
        check(camp_before == camp_after, "campaign.json unchanged")
        check(repo_before == repo_after, "repository unchanged")
        campaign_state = json.loads(camp_file.read_text(encoding="utf-8"))
        check(campaign_state.get("work_units") == [], "no WUs created")
        check(campaign_state.get("dag", {}).get("edges") == [], "no DAG edges")

        # Plan-lock concurrency: two competing successors of a fresh proposed head
        concurrency_proposed = make_record(campaign_id, status="proposed", created_at="2026-08-07T20:00:00+00:00")
        concurrency_proposed["assumptions"][0]["statement"] = "concurrent"
        concurrency_proposed["plan_digest"] = store.compute_plan_digest(concurrency_proposed)
        concurrency_proposed["plan_revision_id"] = store.derive_plan_revision_id(concurrency_proposed["plan_digest"])
        concurrency_proposed["record_digest"] = store.compute_record_digest(concurrency_proposed)
        concurrency_proposed["record_id"] = store.derive_record_id(concurrency_proposed["record_digest"])
        store.store_plan_record(campaign_dir, campaign_id, 1, concurrency_proposed)
        succ_reviewed = store.build_lifecycle_record(concurrency_proposed, "reviewed", "2026-08-07T20:01:00+00:00")
        succ_rejected = store.build_lifecycle_record(concurrency_proposed, "rejected", "2026-08-07T20:02:00+00:00")
        ctx = (str(campaign_dir), campaign_id, json.dumps([succ_reviewed, succ_rejected]))
        p1 = multiprocessing.Process(target=_concurrent_worker, args=(ctx, 0))
        p2 = multiprocessing.Process(target=_concurrent_worker, args=(ctx, 1))
        p1.start(); p2.start(); p1.join(); p2.join()
        # exactly one child: chain length 2
        chain = store._load_plan_chain(campaign_dir, campaign_id, concurrency_proposed["plan_revision_id"])
        check(len(chain) == 2, "concurrency: exactly one child (chain length 2)")

    print(f"plan store test: {'FAIL' if failures else 'PASS'} ({len(failures)} failures)")
    if failures:
        for f in failures:
            print("  FAIL:", f)
        raise SystemExit(1)


def _concurrent_worker(ctx: tuple, index: int) -> None:
    campaign_dir, campaign_id, payload = ctx
    records = json.loads(payload)
    try:
        store.store_plan_record(campaign_dir, campaign_id, 1, records[index])
    except Exception:
        pass


if __name__ == "__main__":
    main()
