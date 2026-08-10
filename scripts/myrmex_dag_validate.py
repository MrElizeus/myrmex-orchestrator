#!/usr/bin/env python3
"""Fail-closed semantic validation for plan-compiled campaign DAGs (P1-011)."""
from __future__ import annotations

import hashlib
import heapq
import json
import pathlib
import posixpath
import re
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import myrmex_campaign_intelligence as intel  # noqa: E402
import myrmex_plan_store as plan_store  # noqa: E402
import myrmex_work_unit_compiler as compiler  # noqa: E402

SCHEMA = "myrmex.dag-validation-result/v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PLAN_RE = re.compile(r"^plan_[0-9a-f]{64}$")


class DAGValidationResultInvalid(ValueError): pass


def _canon(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canon(value)).hexdigest()


def _path_legal(path: Any) -> bool:
    if not isinstance(path, str) or not path or "\x00" in path or path.startswith(("/", "~")):
        return False
    normalized = posixpath.normpath(path.replace("\\", "/"))
    return normalized not in {".", ".."} and not normalized.startswith("../") and not normalized.startswith(".git/") and normalized != ".git"


def _paths_overlap(left: str, right: str) -> bool:
    left = posixpath.normpath(left.replace("\\", "/")).rstrip("/")
    right = posixpath.normpath(right.replace("\\", "/")).rstrip("/")
    if any(char in left + right for char in "*?["):
        return left == right or left in {"*", "**"} or right in {"*", "**"}
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


def _topology(ids: set[str], edges: set[tuple[str, str]]) -> tuple[list[str], dict[str, set[str]]]:
    adjacency = {wu_id: set() for wu_id in ids}
    indegree = {wu_id: 0 for wu_id in ids}
    for source, target in edges:
        adjacency[source].add(target)
        indegree[target] += 1
    queue = [wu_id for wu_id, degree in indegree.items() if degree == 0]
    heapq.heapify(queue)
    order = []
    while queue:
        current = heapq.heappop(queue)
        order.append(current)
        for target in sorted(adjacency[current]):
            indegree[target] -= 1
            if indegree[target] == 0:
                heapq.heappush(queue, target)
    return order, adjacency


def _critical_path(order: list[str], adjacency: dict[str, set[str]]) -> list[str]:
    best: dict[str, tuple[str, ...]] = {}
    predecessors = {wu_id: [] for wu_id in order}
    for source, targets in adjacency.items():
        for target in targets:
            predecessors[target].append(source)
    for wu_id in order:
        candidates = [best[source] + (wu_id,) for source in predecessors[wu_id]]
        best[wu_id] = min((path for path in candidates if len(path) == max(map(len, candidates))), default=(wu_id,))
    return list(min((path for path in best.values() if len(path) == max(map(len, best.values()))), default=()))


def _reachable(adjacency: dict[str, set[str]]) -> set[tuple[str, str]]:
    result = set()
    for source in adjacency:
        stack = list(adjacency[source])
        seen = set()
        while stack:
            target = stack.pop()
            if target in seen:
                continue
            seen.add(target); result.add((source, target)); stack.extend(adjacency[target])
    return result


def validate_result(result: Any) -> None:
    fields = {
        "schema", "status", "campaign_id", "campaign_revision", "expected_campaign_revision",
        "plan_revision_id", "reviewed_plan_record_id", "review_digest", "graph_digest",
        "topological_order", "critical_path", "ready_work_units", "human_gated_work_units",
        "uncovered_backlog_item_ids", "defects", "authority", "validation_id", "validation_digest",
    }
    if not isinstance(result, dict) or set(result) != fields or result.get("schema") != SCHEMA:
        raise DAGValidationResultInvalid("DAG validation result fields/schema invalid")
    body = {key: value for key, value in result.items() if key not in {"validation_id", "validation_digest"}}
    digest = _sha(body)
    if result["validation_digest"] != digest or result["validation_id"] != "dagval_" + digest:
        raise DAGValidationResultInvalid("DAG validation result digest identity invalid")
    if result["status"] not in {"PASS", "FAIL"} or not isinstance(result["campaign_id"], str):
        raise DAGValidationResultInvalid("DAG validation status/campaign invalid")
    for field in ("campaign_revision", "expected_campaign_revision"):
        if not isinstance(result[field], int) or isinstance(result[field], bool):
            raise DAGValidationResultInvalid(f"{field} invalid")
    if not isinstance(result["plan_revision_id"], str) or not SHA256_RE.fullmatch(result["graph_digest"]):
        raise DAGValidationResultInvalid("DAG validation plan/graph identity invalid")
    for field in ("topological_order", "critical_path", "ready_work_units", "human_gated_work_units", "uncovered_backlog_item_ids", "defects"):
        if not isinstance(result[field], list):
            raise DAGValidationResultInvalid(f"{field} must be an array")
    if len(result["topological_order"]) != len(set(result["topological_order"])) or not set(result["critical_path"]).issubset(result["topological_order"]):
        raise DAGValidationResultInvalid("DAG validation order/path invalid")
    if result["authority"] != {"scope": "validation_only", "activate_plan": False, "repository_write": False, "commit": False, "push": False}:
        raise DAGValidationResultInvalid("DAG validation authority invalid")
    if result["status"] == "PASS":
        if result["defects"] or result["uncovered_backlog_item_ids"] or result["campaign_revision"] != result["expected_campaign_revision"]:
            raise DAGValidationResultInvalid("PASS result contains defects, uncovered backlog, or stale revision")
        if not PLAN_RE.fullmatch(result["plan_revision_id"]) or not isinstance(result["reviewed_plan_record_id"], str) or not isinstance(result["review_digest"], str):
            raise DAGValidationResultInvalid("PASS result lacks exact plan/review identity")
        if not result["topological_order"] or not result["critical_path"] or not SHA256_RE.fullmatch(result["review_digest"]):
            raise DAGValidationResultInvalid("PASS result lacks a completion path or review digest")
    elif not result["defects"]:
        raise DAGValidationResultInvalid("FAIL result must contain defects")


def validate_semantic_dag(
    campaign_dir: pathlib.Path,
    campaign: Any,
    expected_campaign_revision: int,
    plan_revision_id: str,
) -> dict[str, Any]:
    """Return a deterministic PASS/FAIL receipt; malformed or ambiguous input fails closed."""
    defects: list[dict[str, Any]] = []

    def defect(code: str, category: str, message: str, work_units=(), evidence=()):
        defects.append({
            "code": code, "category": category, "severity": "BLOCKING", "message": message,
            "work_unit_ids": sorted(set(work_units)), "evidence_references": sorted(set(evidence)) or ["campaign"],
        })

    campaign_id = campaign.get("id", "invalid") if isinstance(campaign, dict) else "invalid"
    campaign_revision_raw = campaign.get("revision", -1) if isinstance(campaign, dict) else -1
    campaign_revision = campaign_revision_raw if isinstance(campaign_revision_raw, int) and not isinstance(campaign_revision_raw, bool) else -1
    expected_revision = expected_campaign_revision if isinstance(expected_campaign_revision, int) and not isinstance(expected_campaign_revision, bool) else -1
    reviewed = None
    review_receipt = None
    planning_result = None
    if not isinstance(campaign, dict):
        defect("DAG-001", "identity", "campaign must be an object")
    elif campaign_revision_raw != expected_campaign_revision or expected_revision < 0:
        defect("DAG-002", "stale_revision", "campaign revision does not match one valid expected revision", evidence=[f"expected:{expected_campaign_revision}", f"actual:{campaign_revision_raw}"])
    if not isinstance(campaign_id, str) or campaign_id != pathlib.Path(campaign_dir).name:
        defect("DAG-034", "identity", "campaign identity does not match its storage location")
    try:
        if isinstance(campaign, dict):
            reviewed = compiler._reviewed_head_read_only(campaign_dir, campaign_id, plan_revision_id)
            if reviewed.get("lifecycle_status") != "reviewed":
                defect("DAG-003", "authority", "semantic validation requires the current reviewed plan head", evidence=[reviewed.get("record_id", "invalid")])
            review_receipt = compiler._review_receipt(campaign_dir, campaign_id, reviewed)
            _, planning_result = compiler._planning_result(campaign_dir, campaign_id, reviewed)
    except Exception as error:
        defect("DAG-004", "provenance", f"reviewed plan inputs are unavailable or invalid: {type(error).__name__}", evidence=[plan_revision_id])

    campaign_wus = campaign.get("work_units", []) if isinstance(campaign, dict) else []
    campaign_edges_raw = campaign.get("dag", {}).get("edges", []) if isinstance(campaign, dict) and isinstance(campaign.get("dag"), dict) else []
    wu_by_id: dict[str, dict[str, Any]] = {}
    duplicate_ids = set()
    if not isinstance(campaign_wus, list):
        defect("DAG-005", "structure", "campaign work_units must be an array")
        campaign_wus = []
    for wu in campaign_wus:
        wu_id = wu.get("id") if isinstance(wu, dict) else None
        if not isinstance(wu_id, str):
            defect("DAG-006", "references", "campaign contains a WorkUnit without a valid ID")
        elif wu_id in wu_by_id:
            duplicate_ids.add(wu_id)
        else:
            wu_by_id[wu_id] = wu
    if duplicate_ids:
        defect("DAG-007", "references", "duplicate WorkUnit IDs", duplicate_ids)

    raw_edges: list[tuple[str, str]] = []
    malformed_edges = False
    if not isinstance(campaign_edges_raw, list):
        malformed_edges = True
    else:
        for edge in campaign_edges_raw:
            if not isinstance(edge, list) or len(edge) != 2 or not all(isinstance(item, str) for item in edge):
                malformed_edges = True
            else:
                raw_edges.append((edge[0], edge[1]))
    if malformed_edges:
        defect("DAG-008", "edges", "DAG edges must be two-element string arrays")
    campaign_edges = set(raw_edges)
    if len(raw_edges) != len(campaign_edges):
        defect("DAG-009", "edges", "duplicate DAG edges are forbidden")

    ids = set(wu_by_id)
    missing = sorted({node for edge in campaign_edges for node in edge} - ids)
    self_edges = sorted(source for source, target in campaign_edges if source == target)
    if missing:
        defect("DAG-010", "references", "DAG edges reference missing WorkUnits", missing)
    if self_edges:
        defect("DAG-011", "dependencies", "self-dependencies are forbidden", self_edges)

    dependency_edges = set()
    for wu_id, wu in wu_by_id.items():
        dependencies = wu.get("dependencies")
        if not isinstance(dependencies, list) or any(not isinstance(item, str) for item in dependencies):
            defect("DAG-012", "dependencies", "WorkUnit dependencies must be string arrays", [wu_id]); continue
        if len(dependencies) != len(set(dependencies)):
            defect("DAG-013", "dependencies", "duplicate WorkUnit dependencies are forbidden", [wu_id])
        dependency_edges.update((dependency, wu_id) for dependency in dependencies)
    if dependency_edges != campaign_edges:
        defect("DAG-014", "edges", "campaign edges must exactly equal WorkUnit dependency edges", evidence=[f"dependencies:{_sha(sorted(dependency_edges))}", f"edges:{_sha(sorted(campaign_edges))}"])

    safe_edges = {(source, target) for source, target in campaign_edges if source in ids and target in ids and source != target}
    order, adjacency = _topology(ids, safe_edges)
    if len(order) != len(ids):
        defect("DAG-015", "cycles", "campaign DAG contains a cycle", sorted(ids - set(order)))
    critical_path = _critical_path(order, adjacency) if len(order) == len(ids) else []
    if not ids or not critical_path:
        defect("DAG-016", "completion", "no complete objective path exists")

    expected_wus = {wu["id"]: wu for wu in reviewed.get("work_units", [])} if isinstance(reviewed, dict) else {}
    expected_edges = {tuple(edge) for edge in reviewed.get("edges", [])} if isinstance(reviewed, dict) else set()
    if ids != set(expected_wus):
        defect("DAG-017", "references", "campaign WorkUnits do not exactly match the reviewed plan", sorted(ids ^ set(expected_wus)))
    if campaign_edges != expected_edges:
        defect("DAG-018", "edges", "campaign edges do not exactly match the reviewed plan", evidence=[f"plan:{_sha(sorted(expected_edges))}", f"campaign:{_sha(sorted(campaign_edges))}"])

    common_provenance = None
    for wu_id, campaign_wu in wu_by_id.items():
        order_contract = campaign_wu.get("work_order")
        try:
            compiler.validate_work_order(order_contract)
        except Exception:
            defect("DAG-019", "work_order", "WorkUnit lacks a valid compiled work order", [wu_id]); continue
        provenance = order_contract["plan_provenance"]
        if common_provenance is None:
            common_provenance = provenance
        elif provenance != common_provenance:
            defect("DAG-020", "provenance", "compiled work orders have divergent plan provenance", [wu_id])
        if order_contract["campaign_id"] != campaign_id or order_contract["work_unit_id"] != wu_id:
            defect("DAG-021", "identity", "work order campaign/WorkUnit identity mismatch", [wu_id])
        if order_contract["repository_root"] != campaign.get("repository_root") or (isinstance(reviewed, dict) and order_contract["base_sha"] != reviewed["base_sha"]):
            defect("DAG-035", "provenance", "work order repository/base identity mismatch", [wu_id])
        expected = expected_wus.get(wu_id)
        if expected is not None:
            pairs = (
                ("objective", "objective"), ("non_goals", "non_goals"), ("dependencies", "dependencies"),
                ("acceptance_criteria", "acceptance_criteria"), ("verification", "verification"),
                ("risk_class", "risk_class"), ("required_route", "required_route"),
                ("human_gates", "human_gates"), ("expected_evidence", "required_evidence"),
                ("terminal_gate", "terminal_gate"),
            )
            if any(order_contract[left] != expected[right] for left, right in pairs) or order_contract["scope"] != {
                "allowed_paths": expected["scope"]["allowed_paths"],
                "forbidden_paths": expected["scope"]["forbidden_paths"],
                "preexisting_dirty_paths": order_contract["scope"]["preexisting_dirty_paths"],
            }:
                defect("DAG-022", "work_order", "compiled work order diverges from reviewed plan semantics", [wu_id])
            projection = {
                "objective": order_contract["objective"], "dependencies": order_contract["dependencies"],
                "scope": order_contract["scope"]["allowed_paths"], "acceptance_criteria": order_contract["acceptance_criteria"],
                "verification_commands": order_contract["verification"]["commands"], "risk_class": order_contract["risk_class"],
                "required_route": order_contract["required_route"],
            }
            if any(campaign_wu.get(key) != value for key, value in projection.items()):
                defect("DAG-023", "work_order", "campaign WorkUnit projection diverges from its work order", [wu_id])
        scope = order_contract["scope"]
        all_paths = scope["allowed_paths"] + scope["forbidden_paths"] + scope["preexisting_dirty_paths"]
        if any(not _path_legal(path) for path in all_paths):
            defect("DAG-024", "scope", "scope contains an unsafe repository path", [wu_id])
        if any(_paths_overlap(a, f) for a in scope["allowed_paths"] for f in scope["forbidden_paths"]):
            defect("DAG-025", "scope", "allowed and forbidden scopes overlap", [wu_id])
        if any(_paths_overlap(a, p) for a in scope["allowed_paths"] for p in scope["preexisting_dirty_paths"]):
            defect("DAG-026", "scope", "allowed scope overlaps protected pre-existing work", [wu_id])

    if isinstance(reviewed, dict) and isinstance(review_receipt, dict):
        exact = {
            "plan_revision_id": reviewed["plan_revision_id"], "reviewed_record_id": reviewed["record_id"],
            "plan_digest": reviewed["plan_digest"], "record_digest": reviewed["record_digest"],
            "review_digest": review_receipt["review_digest"],
        }
        if common_provenance != exact:
            defect("DAG-027", "provenance", "work orders are not bound to the exact reviewed plan and critic PASS")
        parent = reviewed.get("parent_revision")
        if parent is not None:
            try:
                envelope = intel.get_artifact(campaign_dir, campaign_id, parent["artifact_id"])["artifact"]
                parent_record = envelope["payload"]
                plan_store.validate_plan_revision_record(parent_record)
                if envelope["artifact_digest"] != parent["artifact_digest"] or parent_record["campaign_id"] != campaign_id or parent_record["plan_revision_id"] == plan_revision_id:
                    raise ValueError("parent identity mismatch")
            except Exception:
                defect("DAG-028", "supersession", "plan supersession reference is unavailable or invalid", evidence=[parent.get("artifact_id", "invalid") if isinstance(parent, dict) else "invalid"])

    expected_coverage = {}
    if isinstance(planning_result, dict) and isinstance(planning_result.get("coverage_matrix"), list):
        expected_coverage = {entry["backlog_item_id"]: sorted(entry["work_unit_ids"]) for entry in planning_result["coverage_matrix"]}
    actual_coverage: dict[str, list[str]] = {}
    for wu_id, wu in wu_by_id.items():
        order_contract = wu.get("work_order")
        if isinstance(order_contract, dict):
            for entry in order_contract.get("backlog_provenance", []):
                if isinstance(entry, dict) and isinstance(entry.get("backlog_item_id"), str):
                    actual_coverage.setdefault(entry["backlog_item_id"], []).append(wu_id)
    actual_coverage = {key: sorted(value) for key, value in actual_coverage.items()}
    uncovered = sorted(set(expected_coverage) - set(actual_coverage))
    if uncovered:
        defect("DAG-029", "coverage", "reviewed backlog items are uncovered", evidence=uncovered)
    if actual_coverage != expected_coverage:
        defect("DAG-030", "coverage", "work-order backlog traceability differs from the reviewed planning result", evidence=[f"expected:{_sha(expected_coverage)}", f"actual:{_sha(actual_coverage)}"])

    reachable = _reachable(adjacency) if len(order) == len(ids) else set()
    for index, left in enumerate(sorted(ids)):
        left_order = wu_by_id[left].get("work_order", {})
        left_paths = left_order.get("scope", {}).get("allowed_paths", []) if isinstance(left_order, dict) else []
        for right in sorted(ids)[index + 1:]:
            if (left, right) in reachable or (right, left) in reachable:
                continue
            right_order = wu_by_id[right].get("work_order", {})
            right_paths = right_order.get("scope", {}).get("allowed_paths", []) if isinstance(right_order, dict) else []
            if any(_paths_overlap(a, b) for a in left_paths for b in right_paths):
                defect("DAG-031", "resource", "unordered WorkUnits have overlapping repository resources", [left, right])

    completed = {wu_id for wu_id, wu in wu_by_id.items() if wu.get("status") == "completed"}
    ready, gated = [], []
    for wu_id in order:
        wu = wu_by_id[wu_id]
        if wu.get("status") not in {"pending", "ready", "active", "verifying", "remediating", "ci", "delivering", "completed", "blocked", "failed", "cancelled", "superseded"}:
            defect("DAG-036", "readiness", "WorkUnit has an invalid lifecycle status", [wu_id])
        if (wu.get("status") == "completed") != (wu.get("phase") == "completed"):
            defect("DAG-037", "readiness", "WorkUnit completed status/phase are inconsistent", [wu_id])
        dependencies = set(wu.get("dependencies", []))
        gates = wu.get("work_order", {}).get("human_gates", []) if isinstance(wu.get("work_order"), dict) else []
        ready_gates = [gate for gate in gates if isinstance(gate, dict) and gate.get("required_before") == "work_unit_ready"]
        structurally_ready = wu.get("status") in {"pending", "ready"} and dependencies.issubset(completed)
        if ready_gates and wu.get("status") in {"ready", "active", "verifying", "remediating", "ci", "delivering"}:
            defect("DAG-032", "readiness", "human-gated WorkUnit advanced without a decision", [wu_id], [gate.get("gate_id", "invalid") for gate in ready_gates])
        if structurally_ready and ready_gates:
            gated.append(wu_id)
        elif structurally_ready:
            ready.append(wu_id)
        if wu.get("status") in {"ready", "active", "verifying", "remediating", "ci", "delivering"} and not dependencies.issubset(completed):
            defect("DAG-033", "readiness", "WorkUnit advanced before dependencies completed", [wu_id])

    graph_payload = {
        "plan_revision_id": plan_revision_id, "work_unit_ids": sorted(ids),
        "work_orders": sorted([
            [wu_id, order_contract.get("work_order_digest", "invalid") if isinstance(order_contract, dict) else "invalid"]
            for wu_id, wu in wu_by_id.items() for order_contract in [wu.get("work_order")]
        ]),
        "edges": [list(edge) for edge in sorted(campaign_edges)],
    }
    graph_digest = _sha(graph_payload)
    defects.sort(key=lambda item: (item["code"], item["work_unit_ids"], item["message"]))
    body = {
        "schema": SCHEMA, "status": "FAIL" if defects else "PASS", "campaign_id": campaign_id,
        "campaign_revision": campaign_revision, "expected_campaign_revision": expected_revision,
        "plan_revision_id": plan_revision_id,
        "reviewed_plan_record_id": reviewed.get("record_id") if isinstance(reviewed, dict) else None,
        "review_digest": review_receipt.get("review_digest") if isinstance(review_receipt, dict) else None,
        "graph_digest": graph_digest, "topological_order": order if len(order) == len(ids) else [],
        "critical_path": critical_path, "ready_work_units": ready, "human_gated_work_units": gated,
        "uncovered_backlog_item_ids": uncovered, "defects": defects,
        "authority": {"scope": "validation_only", "activate_plan": False, "repository_write": False, "commit": False, "push": False},
    }
    validation_digest = _sha(body)
    result = {**body, "validation_id": "dagval_" + validation_digest, "validation_digest": validation_digest}
    validate_result(result)
    return result
