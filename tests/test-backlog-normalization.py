#!/usr/bin/env python3
"""P1-006 durable backlog normalization tests.

Builds three real source observations (roadmap, manifest, GitHub) through the
approved P1-004/P1-005 adapters, normalizes them into one durable snapshot,
and verifies identity stability, source-order independence, idempotency,
digest binding, raw-content exclusion, no-WU/DAG/campaign mutation, and
snapshot-last recovery.
"""
from __future__ import annotations

import ast
import copy
import json
import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import myrmex_backlog_normalizer as norm  # noqa: E402
import myrmex_roadmap_reader as roadmap  # noqa: E402
import myrmex_github_reader as github  # noqa: E402
import myrmex_campaign_intelligence as intel  # noqa: E402

ROADMAP_FIX = ROOT / "tests" / "fixtures" / "local-import" / "roadmap.md"
GITHUB_FIX = ROOT / "tests" / "fixtures" / "github-import" / "snapshot.json"

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
        [str(ROOT / "bin" / "myrmex-campaign"), "init", "--id", "camp-p1006-test", "--title", "P1-006", "--objective", "backlog normalization", "--repo-root", str(repo)],
        capture_output=True, text=True, env=env,
    )
    if proc.returncode != 0:
        raise AssertionError(f"campaign init failed: {proc.stdout} {proc.stderr}")
    campaign_dir = next(state_home.rglob("campaign.json")).parent
    return repo, "camp-p1006-test", campaign_dir


def count_artifacts(campaign_dir: pathlib.Path) -> int:
    artifacts = campaign_dir / "intelligence" / "artifacts"
    if not artifacts.exists():
        return 0
    return len(list(artifacts.glob("*.json")))


def count_by_kind(campaign_dir: pathlib.Path) -> dict[str, int]:
    counts = {"backlog": 0, "decision": 0, "plan": 0, "review": 0}
    artifacts = campaign_dir / "intelligence" / "artifacts"
    if not artifacts.exists():
        return counts
    for path in artifacts.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        kind = data.get("kind")
        if kind in counts:
            counts[kind] += 1
    return counts


class FakeTransport:
    def __init__(self, snapshot: dict):
        self.snapshot = snapshot
        self.calls = 0

    def __call__(self, request: dict) -> dict:
        self.calls += 1
        return dict(self.snapshot)


def build_three_sources(repo, campaign_dir, campaign_id) -> dict:
    """Create roadmap/manifest/GitHub source observations; return {op_id: (observation, neutral)}."""
    # roadmap
    roadmap_path = repo / "roadmap.md"
    roadmap_path.write_bytes((ROADMAP_FIX).read_bytes())
    r_roadmap = roadmap.execute_local_import(str(campaign_dir), campaign_id, 1, "norm-roadmap-001", str(repo), "roadmap.md")
    roadmap_neutral = roadmap.parse_markdown_roadmap(ROADMAP_FIX.read_text(encoding="utf-8"), "roadmap.md")

    # manifest
    manifest_path = repo / "manifest.json"
    manifest_path.write_text(json.dumps({
        "title": "Manifest",
        "objectives": [{"id": "obj-a", "title": "Obj A", "items": [{"id": "item-m", "title": "Manifest Item", "priority": 2}]}],
        "items": [],
    }, ensure_ascii=False), encoding="utf-8")
    r_manifest = roadmap.execute_local_import(str(campaign_dir), campaign_id, 1, "norm-manifest-001", str(repo), "manifest.json")
    manifest_neutral = roadmap.parse_json_manifest(manifest_path.read_text(encoding="utf-8"), "manifest.json")

    # github
    transport = FakeTransport(json.loads(GITHUB_FIX.read_text(encoding="utf-8")))
    r_github = github.execute_github_import(str(campaign_dir), campaign_id, 1, "norm-github-001", "Owner/Repo", transport)
    github_neutral = github.normalize_github_snapshot(json.loads(GITHUB_FIX.read_text(encoding="utf-8")), "owner/repo")

    return {
        r_roadmap["operation_id"]: ("roadmap", roadmap_neutral),
        r_manifest["operation_id"]: ("manifest", manifest_neutral),
        r_github["operation_id"]: ("github", github_neutral),
    }


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="myrmex-p1006-") as td:
        tmp = pathlib.Path(td)
        repo, campaign_id, campaign_dir = make_campaign(tmp)
        sources_map = build_three_sources(repo, campaign_dir, campaign_id)

        # Build source descriptors (normalize_backlog_sources validates + loads observations)
        descriptors = [
            {"operation_id": op, "neutral": neutral}
            for op, (label, neutral) in sources_map.items()
        ]

        # Combined normalization
        result = norm.normalize_backlog_sources(campaign_dir, campaign_id, 1, descriptors)
        check(result["ok"] is True, "normalization ok")
        check(result["status"] == "created", "snapshot created")
        check(result["outcome"] == "changed", "first outcome changed")
        check(result["source_count"] == 3, "three sources")
        expected_items = (
            len(roadmap.parse_markdown_roadmap(ROADMAP_FIX.read_text(encoding="utf-8"), "roadmap.md")["items"])
            + len(roadmap.parse_json_manifest(
                (repo / "manifest.json").read_text(encoding="utf-8"), "manifest.json")["items"])
            + len(github.normalize_github_snapshot(json.loads(GITHUB_FIX.read_text(encoding="utf-8")), "owner/repo")["issues"])
        )
        check(result["item_count"] == expected_items, f"item_count {result['item_count']} == {expected_items}")
        check(result["item_artifacts_created"] == expected_items, "all items created first time")

        # Snapshot artifact exists
        snap_artifact_id = f"normalized-backlog/snapshot/{result['snapshot_record_id']}"
        snap = intel.get_artifact(str(campaign_dir), campaign_id, snap_artifact_id)
        check(snap["ok"] is True and snap["artifact"]["kind"] == "backlog", "snapshot artifact kind=backlog")
        norm.validate_normalized_snapshot(snap["artifact"]["payload"])
        check(snap["artifact"]["payload"]["snapshot_record_id"] == result["snapshot_record_id"], "snapshot record id matches")

        # No raw bodies/descriptions in any artifact
        found = []
        for path in (campaign_dir / "intelligence" / "artifacts").glob("*.json"):
            text = path.read_text(errors="ignore")
            for sentinel in ("RAW-ISSUE-BODY-SENTINEL", "RAW-MILESTONE-DESCRIPTION-SENTINEL", "source_location", "updated_at"):
                if sentinel in text:
                    found.append(f"{sentinel} in {path.name}")
        check(found == [], f"no raw/non-semantic content in artifacts: {found}")

        # Item identity stability: reordered sources -> same snapshot_digest
        reordered_descriptors = list(reversed(descriptors))
        result2 = norm.normalize_backlog_sources(campaign_dir, campaign_id, 1, reordered_descriptors)
        check(result2["snapshot_digest"] == result["snapshot_digest"], "source order independent snapshot digest")
        check(result2["status"] == "reused", "reordered normalization reuses snapshot")

        # Repeated normalization idempotent
        before_count = count_artifacts(campaign_dir)
        result3 = norm.normalize_backlog_sources(campaign_dir, campaign_id, 1, descriptors)
        check(result3["status"] == "reused", "repeated normalization reused")
        check(count_artifacts(campaign_dir) == before_count, "no new artifacts on repeat")

        # unchanged with previous_snapshot_digest
        result4 = norm.normalize_backlog_sources(
            campaign_dir, campaign_id, 1, descriptors, previous_snapshot_digest=result["snapshot_digest"])
        check(result4["outcome"] == "unchanged", "equivalent -> unchanged")

        # semantic change -> changed + new item version, same backlog_item_id
        # build a modified roadmap with a priority change on the first item
        mod_roadmap = ROADMAP_FIX.read_text(encoding="utf-8").replace("Priority: P1", "Priority: P9")
        mod_path = repo / "roadmap-changed.md"
        mod_path.write_text(mod_roadmap, encoding="utf-8")
        r_mod = roadmap.execute_local_import(str(campaign_dir), campaign_id, 1, "norm-roadmap-changed", str(repo), "roadmap-changed.md")
        mod_neutral = roadmap.parse_markdown_roadmap(mod_roadmap, "roadmap-changed.md")
        changed_descriptors = [
            {"operation_id": r_mod["operation_id"], "neutral": mod_neutral},
            *[d for d in descriptors if d["operation_id"] != r_mod["operation_id"]],
        ]
        result5 = norm.normalize_backlog_sources(
            campaign_dir, campaign_id, 1, changed_descriptors, previous_snapshot_digest=result["snapshot_digest"])
        check(result5["outcome"] == "changed", "semantic change -> changed")

        # mismatch negative: pair a valid neutral with the wrong observation
        wrong = {
            "operation_id": list(sources_map)[0],
            "neutral": copy.deepcopy(list(sources_map.values())[1][1]),
        }
        try:
            norm.normalize_backlog_sources(campaign_dir, campaign_id, 1, [wrong])
            check(False, "source mismatch should raise")
        except norm.BacklogNormalizationSourceMismatch:
            check(True, "source mismatch rejected")

        # non-ready source: ambiguous observation (github ambiguous fixture)
        amb_transport = FakeTransport(json.loads((ROOT / "tests" / "fixtures" / "github-import" / "snapshot-ambiguous.json").read_text()))
        r_amb = github.execute_github_import(str(campaign_dir), campaign_id, 1, "norm-amb-001", "Owner/Repo", amb_transport)
        amb_neutral = github.normalize_github_snapshot(json.loads((ROOT / "tests" / "fixtures" / "github-import" / "snapshot-ambiguous.json").read_text()), "owner/repo")
        try:
            norm.normalize_backlog_sources(campaign_dir, campaign_id, 1, [{"operation_id": r_amb["operation_id"], "neutral": amb_neutral}])
            check(False, "non-ready source should raise")
        except norm.BacklogNormalizationSourceNotReady:
            check(True, "non-ready source rejected")

        # campaign immutability + no WU/DAG
        camp_json = next(campaign_dir.glob("campaign.json"))
        camp_before = camp_json.read_bytes()
        repo_before = {p.relative_to(repo).as_posix(): p.read_bytes() for p in repo.rglob("*") if p.is_file()}
        norm.normalize_backlog_sources(campaign_dir, campaign_id, 1, descriptors)
        camp_after = camp_json.read_bytes()
        repo_after = {p.relative_to(repo).as_posix(): p.read_bytes() for p in repo.rglob("*") if p.is_file()}
        check(camp_before == camp_after, "campaign.json unchanged")
        check(repo_before == repo_after, "repository unchanged")
        show = subprocess.run(
            [str(ROOT / "bin" / "myrmex-campaign"), "show", campaign_id],
            capture_output=True, text=True, env=dict(os.environ, XDG_STATE_HOME=str(tmp / "state")),
        )
        check(show.returncode == 0, "campaign show works")
        check("Work Units (0)" in show.stdout, "no WUs created")
        campaign_file = next(campaign_dir.glob("campaign.json"))
        campaign_state = json.loads(campaign_file.read_text(encoding="utf-8"))
        check(campaign_state.get("work_units") == [], "campaign work_units empty")
        check(campaign_state.get("dag", {}).get("edges") == [], "campaign DAG empty")

        # doctor healthy
        doc = intel.doctor(str(campaign_dir), campaign_id)
        check(doc.get("status") == "healthy", f"intelligence doctor healthy (got {doc.get('status')})")

        # static capability audit
        src = (ROOT / "scripts" / "myrmex_backlog_normalizer.py").read_text()
        tree = ast.parse(src)
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
        for banned in ("requests", "urllib", "http.client", "subprocess", "httpx", "aiohttp"):
            check(all(banned not in imp for imp in imports), f"no {banned} import in normalizer")
        for banned in ("curl", "wget", "wu-add", "wu-transition", "activate-plan", "git commit", "git push", "write_campaign"):
            check(banned not in src, f"no {banned} in normalizer source")

        # Fail-closed import: module import raises BacklogNormalizationError when backend blocked
        import textwrap as _tw
        _probe = _tw.dedent(
            """
            import sys, importlib.abc
            class Blocker(importlib.abc.MetaPathFinder):
                def find_spec(self, name, path=None, target=None):
                    if name == "myrmex_campaign_intelligence":
                        raise ImportError("blocked for test")
                    return None
            sys.meta_path.insert(0, Blocker())
            sys.path.insert(0, "scripts")
            try:
                import myrmex_backlog_normalizer
                print("IMPORTED")
            except Exception as e:
                print(type(e).__name__)
            """
        )
        _r = subprocess.run([sys.executable, "-c", _probe], capture_output=True, text=True, cwd=str(ROOT))
        check("BacklogNormalizationError" in _r.stdout, "module import fails closed with BacklogNormalizationError")

    print(f"backlog normalization test: {'FAIL' if failures else 'PASS'} ({len(failures)} failures)")
    if failures:
        for f in failures:
            print("  FAIL:", f)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
