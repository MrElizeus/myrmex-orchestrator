#!/usr/bin/env python3
"""unittest discovery entry point for the P1-008 planner checks."""
from __future__ import annotations

import copy
import multiprocessing
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import myrmex_backlog_normalizer as backlog
import myrmex_campaign_intelligence as intel
import myrmex_planner as planner


def _fixture(create_request=True):
    root = pathlib.Path(tempfile.mkdtemp())
    campaign_id = "camp-p1008-process-test"
    identity = {"kind": "local-roadmap", "canonical_id": "roadmap.md"}
    entity = "srcitem_" + "b" * 64
    item = {
        "schema": backlog.NORMALIZED_ITEM_SCHEMA, "backlog_item_id": "",
        "item_digest": "", "source_adapter": "local-markdown-roadmap/v1",
        "source_identity": identity, "source_entity_type": "local-item",
        "source_entity_id": entity, "title": "process replay", "priority": None,
        "state": None, "dependency_hints": [], "constraints": [],
        "context_constraints": [], "labels": [], "group_ref": None,
    }
    item["backlog_item_id"] = backlog.compute_backlog_item_id(item["source_adapter"], identity, entity)
    item["item_digest"] = backlog.compute_item_digest(item)
    item_id = f"normalized-backlog/item/{item['backlog_item_id']}/{item['item_digest']}"
    intel.put_artifact(root, campaign_id, 1, "backlog", item_id, item)
    source_digest = "c" * 64
    source = {
        "operation_id": "importop-" + "c" * 24,
        "observation_id": "srcobs_" + source_digest,
        "observation_digest": source_digest,
        "request_digest": "d" * 64,
        "content_digest": "e" * 64,
        "outcome": "changed",
        "adapter": "local-markdown-roadmap/v1",
        "source_identity": identity,
    }
    snapshot = {
        "schema": backlog.NORMALIZED_SNAPSHOT_SCHEMA, "snapshot_record_id": "",
        "snapshot_record_digest": "", "snapshot_digest": "", "source_count": 0,
        "sources": [source], "item_count": 1,
        "items": [{"backlog_item_id": item["backlog_item_id"], "item_digest": item["item_digest"], "artifact_id": item_id}],
    }
    snapshot["source_count"] = len(snapshot["sources"])
    snapshot["snapshot_digest"] = backlog.compute_snapshot_digest_from_snapshot(snapshot)
    snapshot["snapshot_record_digest"] = backlog.compute_snapshot_record_digest(snapshot)
    snapshot["snapshot_record_id"] = "blsnaprec_" + snapshot["snapshot_record_digest"]
    intel.put_artifact(root, campaign_id, 1, "backlog", "normalized-backlog/snapshot/" + snapshot["snapshot_record_id"], snapshot)
    repository_context = {
        "schema": "myrmex.repository-context/v1", "run_id": "run-process",
        "objective_id": "obj-process", "repository_root": "/repo", "branch": "main",
        "base_sha": "0" * 40, "git_status": [], "objective": "process replay",
        "relevant_files": ["roadmap.md"], "relevant_symbols": [], "architecture": [],
        "current_behavior": [], "tests": [], "data_contracts": ["myrmex.backlog-snapshot/v1"],
        "observed_conventions": [], "implementation_constraints": ["planning-only"],
        "unresolved_decisions": [], "protected_dirty_paths": [],
        "excluded_sensitive_paths": [".env"], "evidence": ["snapshot"],
    }
    repository_context_id = "repository-context/snapshot/" + intel.compute_payload_digest(repository_context)
    intel.put_artifact(root, campaign_id, 1, "decision", repository_context_id, repository_context)
    constraints = {"allowed_paths": ["src/"], "forbidden_paths": [".git"], "required_invariants": ["planning-only"], "required_sections": ["work_units"]}
    request = None
    if create_request:
        request = planner.create_planning_request(root, campaign_id, 1, "req-process", "run-process", "obj-process", "0" * 40, snapshot["snapshot_record_id"], repository_context_id, constraints)
    return root, campaign_id, request, snapshot, constraints, repository_context_id


def _request_worker(root, campaign_id, snapshot, repository_context_id, constraints, start, output):
    start.wait()
    try:
        request = planner.create_planning_request(
            root, campaign_id, 1, "req-process", "run-process", "obj-process",
            "0" * 40, snapshot["snapshot_record_id"], repository_context_id, constraints,
        )
        output.put(("ok", request))
    except Exception as error:  # pragma: no cover - asserted by parent
        output.put(("error", type(error).__name__, str(error)))


def _record_worker(root, campaign_id, request, start, output):
    start.wait()
    result = {
        "schema": planner.RESULT_SCHEMA, "request_id": request["request_id"],
        "run_id": request["run_id"], "campaign_id": campaign_id,
        "objective_id": request["objective_id"], "base_sha": request["base_sha"],
        "response_type": "already_complete", "plan_revision": None,
        "analysis": None,
        "coverage_matrix": None,
        "clarification": None, "completion_evidence": ["process"],
        "authority": dict(planner.AUTHORITY), "result_digest": "",
        "created_at": "2026-08-09T00:00:00+00:00",
    }
    result["result_digest"] = planner._sha({k: v for k, v in result.items() if k != "result_digest"})
    try:
        receipt = planner.record_planning_result(root, campaign_id, 1, request["request_id"], result)
        output.put(("ok", receipt["status"]))
    except Exception as error:  # pragma: no cover - asserted by parent
        output.put(("error", type(error).__name__, str(error)))


class PlannerOrchestrationDiscovery(unittest.TestCase):
    def test_existing_integrity_matrix(self):
        completed = subprocess.run([sys.executable, str(ROOT / "tests" / "test-planner-orchestration.py")], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_process_duplicate_recording_is_canonical(self):
        root, campaign_id, request, _, _, _ = _fixture()
        start = multiprocessing.Event()
        output = multiprocessing.Queue()
        workers = [multiprocessing.Process(target=_record_worker, args=(root, campaign_id, request, start, output)) for _ in range(2)]
        for worker in workers:
            worker.start()
        start.set()
        for worker in workers:
            worker.join(30)
            self.assertEqual(worker.exitcode, 0)
        outcomes = [output.get(timeout=5) for _ in workers]
        self.assertTrue(all(outcome[0] == "ok" for outcome in outcomes), outcomes)
        self.assertEqual(sorted(outcome[1] for outcome in outcomes), ["recorded", "reused"])
        request_id = "planning-request/request/" + planner._text_sha(request["request_id"])
        response_id = "planning-result/response/" + planner._text_sha(request["request_id"])
        self.assertEqual(intel.get_artifact(root, campaign_id, request_id)["artifact"]["payload"], request)
        self.assertEqual(intel.get_artifact(root, campaign_id, response_id)["artifact"]["payload"]["request_id"], request["request_id"])

    def test_process_duplicate_request_creation_is_canonical(self):
        root, campaign_id, _, snapshot, constraints, repository_context_id = _fixture(create_request=False)
        start = multiprocessing.Event()
        output = multiprocessing.Queue()
        workers = [
            multiprocessing.Process(
                target=_request_worker,
                args=(root, campaign_id, snapshot, repository_context_id, constraints, start, output),
            )
            for _ in range(2)
        ]
        for worker in workers:
            worker.start()
        start.set()
        for worker in workers:
            worker.join(30)
            self.assertEqual(worker.exitcode, 0)
        outcomes = [output.get(timeout=5) for _ in workers]
        self.assertTrue(all(outcome[0] == "ok" for outcome in outcomes), outcomes)
        self.assertEqual(outcomes[0][1], outcomes[1][1])
        request_id = "planning-request/request/" + planner._text_sha("req-process")
        self.assertEqual(
            intel.get_artifact(root, campaign_id, request_id)["artifact"]["payload"],
            outcomes[0][1],
        )


if __name__ == "__main__":
    unittest.main()
