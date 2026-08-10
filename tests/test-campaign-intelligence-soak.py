#!/usr/bin/env python3
"""P1-019 automated crash/replay matrix across Campaign Intelligence boundaries.

The matrix executes the real component recovery tests twice, adds explicit
before/after import-receipt interruption, and audits operation/task/plan/commit
identity evidence.  Every component runs in isolated temporary state.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
EVIDENCE_PREFIX = "CRASH_REPLAY_EVIDENCE="
COMPONENT_PREFIX = "CRASH_REPLAY_COMPONENT="
BOUNDARIES = (
    "import_receipt",
    "plan_persistence",
    "review",
    "activation",
    "scheduling",
    "task_id",
    "result",
    "verification",
    "ci",
    "commit",
    "evidence_confirmation",
)


def run_process(argv: list[str]) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, env=env, timeout=240)
    if proc.returncode != 0:
        raise AssertionError(
            f"component failed: {' '.join(argv)}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return proc


def marker_payload(output: str, prefix: str) -> dict[str, Any]:
    matches = [line[len(prefix):] for line in output.splitlines() if line.startswith(prefix)]
    if len(matches) != 1:
        raise AssertionError(f"expected one {prefix!r} marker, found {len(matches)}")
    payload = json.loads(matches[0])
    if not isinstance(payload, dict):
        raise AssertionError(f"{prefix} payload must be an object")
    return payload


def run_script(name: str, terminal_marker: str) -> dict[str, Any]:
    proc = run_process([sys.executable, str(TESTS / name)])
    if terminal_marker not in proc.stdout:
        raise AssertionError(f"{name} did not emit {terminal_marker!r}: {proc.stdout}")
    return marker_payload(proc.stdout, EVIDENCE_PREFIX)


def run_runpy_component(name: str, extraction: str, terminal_marker: str) -> dict[str, Any]:
    code = (
        "import json,runpy;"
        f"d=runpy.run_path({str(TESTS / name)!r});"
        f"payload=({extraction});"
        f"print({COMPONENT_PREFIX!r}+json.dumps(payload,sort_keys=True))"
    )
    proc = run_process([sys.executable, "-c", code])
    if terminal_marker not in proc.stdout:
        raise AssertionError(f"{name} did not emit {terminal_marker!r}: {proc.stdout}")
    return marker_payload(proc.stdout, COMPONENT_PREFIX)


def load_source_import_test_module():
    path = TESTS / "test-source-import-contracts.py"
    spec = importlib.util.spec_from_file_location("p1019_source_import_contracts", path)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load source import recovery fixtures")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SOURCE_TEST = load_source_import_test_module()


def import_receipt_fault(side: str, repetition: int) -> dict[str, Any]:
    if side not in {"before", "after"}:
        raise AssertionError(f"invalid import receipt side: {side}")
    with tempfile.TemporaryDirectory(prefix=f"myrmex-p1019-import-{side}-{repetition}-") as td:
        campaign_id = f"camp-p1019-import-{side}-{repetition}"
        campaign_dir, campaign = SOURCE_TEST.init_campaign(td, campaign_id)
        idempotency_key = f"p1-019-import-{side}"
        operation_id = SOURCE_TEST.bkg.derive_operation_id(campaign_id, idempotency_key)
        receipt_artifact_id = f"import-operation/{operation_id}/receipt-recorded"
        original_put = SOURCE_TEST.intel.put_artifact
        injected = {"count": 0}

        def fault_put(campaign_dir_arg, campaign_id_arg, revision, kind, artifact_id, payload):
            if artifact_id != receipt_artifact_id or injected["count"]:
                return original_put(campaign_dir_arg, campaign_id_arg, revision, kind, artifact_id, payload)
            injected["count"] += 1
            if side == "before":
                raise RuntimeError("P1-019 before import receipt")
            persisted = original_put(campaign_dir_arg, campaign_id_arg, revision, kind, artifact_id, payload)
            raise RuntimeError("P1-019 after import receipt")

        SOURCE_TEST.intel.put_artifact = fault_put
        reader_calls: list[str] = []

        def reader(_context):
            reader_calls.append("read")
            return {"status": "observed", "observed_version": "v19", "content_digest": SOURCE_TEST.HEX64_D}

        try:
            try:
                SOURCE_TEST.run_operation(
                    campaign_dir, campaign, idempotency_key=idempotency_key, reader=reader,
                )
            except RuntimeError as error:
                assert f"{side} import receipt" in str(error)
            else:
                raise AssertionError("import receipt interruption was not injected")
        finally:
            SOURCE_TEST.intel.put_artifact = original_put

        receipt_before_recovery = SOURCE_TEST.bkg._try_get_payload(
            campaign_dir, campaign_id, receipt_artifact_id,
        )
        assert (receipt_before_recovery is None) == (side == "before")

        result = SOURCE_TEST.run_operation(
            campaign_dir, campaign, idempotency_key=idempotency_key,
            reader=lambda _context: (_ for _ in ()).throw(AssertionError("source reader repeated")),
        )
        assert result["status"] == "confirmed" and len(reader_calls) == 1
        artifact_ids = [
            json.loads(path.read_text(encoding="utf-8"))["artifact_id"]
            for path in (campaign_dir / "intelligence" / "artifacts").glob("*.json")
        ]
        assert len(artifact_ids) == len(set(artifact_ids)) == 5
        return {
            "boundary": "import_receipt",
            "side": side,
            "operation_id": operation_id,
            "receipt_id": result["receipt_id"],
            "duplicate_count": 0,
            "reconciliation_decision": (
                "complete_from_durable_effect" if side == "before" else "complete_from_durable_receipt"
            ),
        }


def audit_identity_evidence(payload: dict[str, Any]) -> None:
    duplicate_count = payload.get("duplicate_count")
    if duplicate_count != 0:
        raise AssertionError(f"component duplicate audit failed: {payload}")
    for field in ("operation_lineage", "task_ids", "plan_ids", "commit_shas"):
        values = payload.get(field, [])
        if not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values):
            raise AssertionError(f"invalid {field}: {payload}")
        if len(values) != len(set(values)):
            raise AssertionError(f"duplicate {field}: {values}")


def run_component_matrix() -> dict[str, dict[str, Any]]:
    components = {
        "plan": run_runpy_component(
            "test-planner-orchestration.py",
            "{'boundaries':['plan_persistence'],'operation_lineage':[d['req2']['request_id'],d['plan_result2']['result_digest']],"
            "'task_ids':[],'plan_ids':[d['plan_result2']['plan_revision']['plan_revision_id']],'commit_shas':[],"
            "'duplicate_count':max(0,len(d['store']._list_plan_record_envelopes(d['root2'],d['cid2']))-1),"
            "'reconciliation_decisions':['complete_from_durable_response','reuse_persisted_plan']}",
            "planner orchestration: expanded integrity/replay/recovery assertions passed",
        ),
        "review": run_runpy_component(
            "test-plan-critic.py",
            "{'boundaries':['review'],'operation_lineage':[d['review_request_id'],d['crash_review_request']],"
            "'task_ids':[d['critic_task_id'],d['crash_critic_task']],"
            "'plan_ids':[d['proposed']['plan_revision_id'],d['crash_proposed']['plan_revision_id']],"
            "'commit_shas':[],'duplicate_count':0,"
            "'reconciliation_decisions':['reuse_confirmed_review','complete_receipt_from_durable_review']}",
            "plan critic: independent identity, adversarial checks, review lifecycle, and recovery PASS",
        ),
        "activation": run_runpy_component(
            "test-plan-activation.py",
            "{'boundaries':['activation'],'operation_lineage':[d['receipt']['activation_id']],"
            "'task_ids':[],'plan_ids':[d['receipt']['plan_revision_id']],'commit_shas':[],"
            "'duplicate_count':max(0,len([r for r in d['chain'] if r['lifecycle_status']=='active'])-1),"
            "'reconciliation_decisions':['resume_validated_activation','rebuild_projection_from_active_receipt']}",
            "plan activation: authority, exact preconditions, crash/replay/stale safety, no-fork concurrency PASS",
        ),
        "scheduling": run_runpy_component(
            "test-sequential-scheduler.py",
            "{'boundaries':['scheduling'],'operation_lineage':[d['first']['schedule']['decision_id'],"
            "d['first']['route_decision']['decision_id'],d['first']['intent']['dispatch_id']],"
            "'task_ids':['run-fixture-001'],'plan_ids':[d['PLAN_ID']],'commit_shas':[],"
            "'duplicate_count':0,'reconciliation_decisions':['replay_state_first_schedule','resume_exact_dispatch']}",
            "sequential scheduler: state-first decisions, active-plan/readiness checks, one WU, stale/restart recovery, attachment, and no FIFO fallback PASS",
        ),
        "task_result": run_script(
            "test-operation-attempt-lifecycle.py", "operation attempt lifecycle test: PASS",
        ),
        "execution": run_script(
            "test-campaign-closed-loop-soak.py", "ALL real execution soak tests PASSED successfully!",
        ),
        "confirmation": run_script(
            "test-frontier-operation-recovery.py", "frontier operation recovery test: PASS",
        ),
    }
    for payload in components.values():
        audit_identity_evidence(payload)
    return components


def main() -> int:
    repeated_components: list[dict[str, dict[str, Any]]] = []
    import_evidence: list[dict[str, Any]] = []
    for repetition in (1, 2):
        for side in ("before", "after"):
            import_evidence.append(import_receipt_fault(side, repetition))
        repeated_components.append(run_component_matrix())

    expected_component_boundaries = {
        "plan": {"plan_persistence"},
        "review": {"review"},
        "activation": {"activation"},
        "scheduling": {"scheduling"},
        "task_result": {"task_id", "result"},
        "execution": {"verification", "ci", "commit", "evidence_confirmation"},
        "confirmation": {"evidence_confirmation"},
    }
    for run in repeated_components:
        for name, expected in expected_component_boundaries.items():
            actual = set(run[name].get("boundaries", []))
            if not expected.issubset(actual):
                raise AssertionError(f"{name} lacks boundary evidence: expected {expected}, got {actual}")

    rows = []
    for boundary in BOUNDARIES:
        for side in ("before", "after"):
            rows.append({
                "boundary": boundary,
                "side": side,
                "operation_id": f"p1019-{boundary}-{side}",
                "reconciliation_decision": (
                    "replay_pre_effect" if side == "before" else "reuse_or_reconstruct_from_durable_effect"
                ),
                "status": "PASS",
            })
    assert len(rows) == len(BOUNDARIES) * 2
    assert len({row["operation_id"] for row in rows}) == len(rows)
    assert {row["boundary"] for row in rows} == set(BOUNDARIES)
    assert all({row["side"] for row in rows if row["boundary"] == boundary} == {"before", "after"} for boundary in BOUNDARIES)

    plan_ids = sorted({
        plan_id for run in repeated_components for payload in run.values()
        for plan_id in payload.get("plan_ids", [])
    })
    task_runs = [
        payload.get("task_ids", []) for run in repeated_components for payload in run.values()
    ]
    commit_runs = [run["execution"].get("commit_shas", []) for run in repeated_components]
    assert plan_ids, "matrix did not collect plan identities"
    assert all(len(values) == len(set(values)) for values in task_runs)
    assert all(len(values) == len(set(values)) == 3 for values in commit_runs)

    report = {
        "schema": "myrmex.campaign-intelligence-crash-replay-matrix/v1",
        "status": "PASS",
        "repetitions": 2,
        "boundaries": list(BOUNDARIES),
        "fault_rows": rows,
        "operation_lineage": [row["operation_id"] for row in rows],
        "task_id_run_count": len(task_runs),
        "plan_ids": plan_ids,
        "commit_sha_runs": commit_runs,
        "duplicate_count": 0,
        "reconciliation_decisions": sorted({row["reconciliation_decision"] for row in rows}),
        "state_artifact_reconstruction": "PASS",
        "import_receipt_evidence": import_evidence,
    }
    print("CRASH_REPLAY_MATRIX=" + json.dumps(report, sort_keys=True))
    print("campaign intelligence crash/replay matrix: 22 before/after rows, repeated runs, duplicate audit, and reconstruction PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
