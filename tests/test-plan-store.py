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
        for idx, terminal_status in enumerate(("rejected", "superseded", "withdrawn")):
            rev = make_record(campaign_id, status="proposed", created_at=f"2026-08-07T10:0{idx}:00+00:00")
            # ensure distinct revision by distinct content
            rev["assumptions"][0]["statement"] = f"term-{terminal_status}"
            rev["plan_digest"] = store.compute_plan_digest(rev)
            rev["plan_revision_id"] = store.derive_plan_revision_id(rev["plan_digest"])
            rev["record_digest"] = store.compute_record_digest(rev)
            rev["record_id"] = store.derive_record_id(rev["record_digest"])
            store.store_plan_record(campaign_dir, campaign_id, 1, rev)
            term = store.build_lifecycle_record(rev, terminal_status, f"2026-08-07T11:0{idx}:00+00:00")
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
        q = multiprocessing.Queue()
        p1 = multiprocessing.Process(target=_concurrent_worker_q, args=(ctx, 0, q))
        p2 = multiprocessing.Process(target=_concurrent_worker_q, args=(ctx, 1, q))
        p1.start(); p2.start(); p1.join(); p2.join()
        outcomes = [q.get(timeout=10), q.get(timeout=10)]
        created = sum(1 for o in outcomes if o == "created")
        conflicts = sum(1 for o in outcomes if o in ("PlanLifecycleConflict", "PlanLifecycleInvalid"))
        check(created == 1, f"concurrency: exactly one created (got {outcomes})")
        check(conflicts == 1, f"concurrency: exactly one typed conflict (got {outcomes})")
        chain = store._load_plan_chain(campaign_dir, campaign_id, concurrency_proposed["plan_revision_id"])
        check(len(chain) == 2, "concurrency: exactly one child (chain length 2)")
        doc_conc = intel.doctor(str(campaign_dir), campaign_id)
        check(doc_conc.get("status") == "healthy", "doctor healthy after concurrency")

        # ---- Frontier corrective-plan regressions (p1-007-val-req-0001) ----
        # F2: cross-kind same-identity conflict (P1-001 parity)
        cross_kind = make_record(campaign_id, status="proposed", created_at="2026-08-07T21:00:00+00:00")
        cross_kind["input_digests"] = [
            {"kind": "repository-context", "identity": "same", "sha256": "a" * 64},
            {"kind": "backlog-snapshot", "identity": "same", "sha256": "b" * 64},
        ]
        cross_kind["plan_digest"] = store.compute_plan_digest(cross_kind)
        cross_kind["plan_revision_id"] = store.derive_plan_revision_id(cross_kind["plan_digest"])
        cross_kind["record_digest"] = store.compute_record_digest(cross_kind)
        cross_kind["record_id"] = store.derive_record_id(cross_kind["record_digest"])
        try:
            store.validate_plan_revision_record(cross_kind)
            check(False, "cross-kind same-identity conflict should fail")
        except store.PlanRecordInvalid:
            check(True, "cross-kind same-identity conflict rejected")

        # F3: empty acceptance criterion
        empty_ac = make_record(campaign_id, status="proposed", created_at="2026-08-07T21:01:00+00:00")
        empty_ac["work_units"] = [make_wu("WU-P1-001", "x")]
        empty_ac["work_units"][0]["acceptance_criteria"] = [""]
        empty_ac["plan_digest"] = store.compute_plan_digest(empty_ac)
        empty_ac["plan_revision_id"] = store.derive_plan_revision_id(empty_ac["plan_digest"])
        empty_ac["record_digest"] = store.compute_record_digest(empty_ac)
        empty_ac["record_id"] = store.derive_record_id(empty_ac["record_digest"])
        try:
            store.validate_plan_revision_record(empty_ac)
            check(False, "empty acceptance criterion should fail")
        except store.PlanRecordInvalid:
            check(True, "empty acceptance criterion rejected")

        # F4: human_gates null / non-list typed failure
        for bad_gates in (None, {}):
            bad_hg = make_record(campaign_id, status="proposed", created_at="2026-08-07T21:02:00+00:00")
            bad_hg["work_units"] = [make_wu("WU-P1-001", "x")]
            bad_hg["work_units"][0]["human_gates"] = bad_gates
            bad_hg["plan_digest"] = store.compute_plan_digest(bad_hg)
            bad_hg["plan_revision_id"] = store.derive_plan_revision_id(bad_hg["plan_digest"])
            bad_hg["record_digest"] = store.compute_record_digest(bad_hg)
            bad_hg["record_id"] = store.derive_record_id(bad_hg["record_digest"])
            try:
                store.validate_plan_revision_record(bad_hg)
                check(False, f"human_gates={bad_gates!r} should fail typed")
            except store.PlanRecordInvalid:
                check(True, f"human_gates={bad_gates!r} typed rejection")
            except Exception as exc:
                check(False, f"human_gates={bad_gates!r} raised incidental {type(exc).__name__}")

        # F5: strict RFC3339
        for bad_time in ("2026-08-07 00:00:00+00:00", "2026-08-07T00:00:00", "2026-08-07T00:00:00+99:99", "not-a-date"):
            bad_t = make_record(campaign_id, status="proposed", created_at=bad_time)
            bad_t["record_digest"] = store.compute_record_digest(bad_t)
            bad_t["record_id"] = store.derive_record_id(bad_t["record_digest"])
            try:
                store.validate_plan_revision_record(bad_t)
                check(False, f"bad RFC3339 {bad_time!r} should fail")
            except store.PlanRecordInvalid:
                check(True, f"bad RFC3339 {bad_time!r} rejected")

        # F9: fcntl-unavailable subprocess → PlanStoreBackendUnavailable + zero writes
        import textwrap as _tw
        _fcntl_probe = _tw.dedent(
            """
            import sys, builtins, pathlib, json
            real_import = builtins.__import__
            def blocked(name, *a, **k):
                if name == "fcntl":
                    raise ImportError("blocked")
                return real_import(name, *a, **k)
            builtins.__import__ = blocked
            sys.path.insert(0, "scripts")
            import myrmex_plan_store as store
            import tempfile, os, subprocess
            tmp = pathlib.Path(tempfile.mkdtemp(prefix="fblock-"))
            repo = tmp/"repo"; repo.mkdir()
            state = tmp/"state"; state.mkdir()
            env = dict(os.environ, XDG_STATE_HOME=str(state))
            subprocess.run([str(pathlib.Path("bin/myrmex-campaign")), "init", "--id", "camp-fblock-001", "--title", "p", "--objective", "x", "--repo-root", str(repo)], capture_output=True, text=True, env=env)
            cdir = next(state.rglob("campaign.json")).parent
            rec = {
                "schema": "myrmex.plan-revision/v1",
                "campaign_id": "camp-fblock-001",
                "objective_id": "obj",
                "planning_request_id": "req",
                "base_sha": "0"*40,
                "parent_revision": None,
                "input_digests": [{"kind": "k", "identity": "i", "sha256": "a"*64}],
                "assumptions": [{"assumption_id": "a1", "statement": "s", "evidence_status": "supported", "impact": "low", "resolution_gate": None}],
                "work_units": [{
                    "id": "WU-P1-001", "objective": "o", "non_goals": [], "dependencies": [],
                    "scope": {"allowed_paths": ["src/"], "forbidden_paths": [".env"]},
                    "acceptance_criteria": ["AC"], "verification": {"commands": [], "manual_checks": [], "discover_when_missing": True},
                    "risk_class": "bounded", "required_route": "frontier-gated",
                    "human_gates": [{"gate_id": "g", "decision_type": "d", "reason": "r", "required_before": "plan_activation"}],
                    "required_evidence": [], "terminal_gate": "G",
                }],
                "edges": [],
                "lifecycle_status": "proposed",
                "previous_record_id": None,
                "created_at": "2026-08-07T00:00:00+00:00",
            }
            rec["plan_digest"] = store.compute_plan_digest(rec)
            rec["plan_revision_id"] = store.derive_plan_revision_id(rec["plan_digest"])
            rec["record_digest"] = store.compute_record_digest(rec)
            rec["record_id"] = store.derive_record_id(rec["record_digest"])
            try:
                store.store_plan_record(cdir, "camp-fblock-001", 1, rec)
                print("NOERROR")
            except Exception as e:
                print(type(e).__name__)
            """
        )
        _r = subprocess.run([sys.executable, "-c", _fcntl_probe], capture_output=True, text=True, cwd=str(ROOT))
        check("PlanStoreBackendUnavailable" in _r.stdout, "fcntl-unavailable raises PlanStoreBackendUnavailable")

        # F6/F11: direct namespace-kind mismatch — seed kind=backlog in plan namespace
        # Use a NEW valid record (not yet persisted) so P1-002 allows the backlog-kind write.
        forged = make_record(campaign_id, status="proposed", created_at="2026-08-07T21:03:00+00:00")
        forged["assumptions"][0]["statement"] = "forged-namespace"
        forged["plan_digest"] = store.compute_plan_digest(forged)
        forged["plan_revision_id"] = store.derive_plan_revision_id(forged["plan_digest"])
        forged["record_digest"] = store.compute_record_digest(forged)
        forged["record_id"] = store.derive_record_id(forged["record_digest"])
        namespace_id = store.ARTIFACT_NAMESPACE + forged["record_id"]
        intel.put_artifact(pathlib.Path(campaign_dir), campaign_id, 1, "backlog", namespace_id, forged)
        try:
            store.get_plan_record(campaign_dir, campaign_id, forged["record_id"])
            check(False, "namespace-kind mismatch should be rejected")
        except store.PlanLifecycleInvalid:
            check(True, "namespace-kind mismatch rejected")

    print(f"plan store test: {'FAIL' if failures else 'PASS'} ({len(failures)} failures)")
    if failures:
        for f in failures:
            print("  FAIL:", f)
        raise SystemExit(1)


def _concurrent_worker_q(ctx: tuple, index: int, q: multiprocessing.Queue) -> None:
    campaign_dir, campaign_id, payload = ctx
    records = json.loads(payload)
    try:
        result = store.store_plan_record(campaign_dir, campaign_id, 1, records[index])
        q.put(result["status"])
    except store.PlanLifecycleConflict:
        q.put("PlanLifecycleConflict")
    except store.PlanLifecycleInvalid:
        q.put("PlanLifecycleInvalid")
    except Exception as exc:
        q.put(f"other-error:{type(exc).__name__}")


if __name__ == "__main__":
    main()
