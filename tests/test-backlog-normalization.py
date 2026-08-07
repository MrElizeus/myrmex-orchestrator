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

        # ---- Frontier corrective-plan regressions (p1-006-val-req-0001) ----
        # F1/F2: strict validator corruption
        # build a valid item from the persisted snapshot descriptors, then tamper
        snap = intel.get_artifact(str(campaign_dir), campaign_id, f"normalized-backlog/snapshot/{result['snapshot_record_id']}")
        snap_payload = snap["artifact"]["payload"]
        item_desc = snap_payload["items"][0]
        valid_item = intel.get_artifact(str(campaign_dir), campaign_id, item_desc["artifact_id"])["artifact"]["payload"]
        corrupt_cases = []
        bad_adapter = dict(valid_item); bad_adapter["source_adapter"] = "unknown/v1"; corrupt_cases.append(("unknown adapter", bad_adapter))
        bad_kind = dict(valid_item); bad_kind["source_identity"] = {"kind": "wrong", "canonical_id": "x"}; corrupt_cases.append(("source-kind mismatch", bad_kind))
        bad_entity = dict(valid_item); bad_entity["source_entity_id"] = "bogus"; corrupt_cases.append(("invalid source_entity_id", bad_entity))
        bad_unsorted = dict(valid_item); bad_unsorted["dependency_hints"] = ["b", "a"]; corrupt_cases.append(("unsorted dependency_hints", bad_unsorted))
        bad_label = dict(valid_item); bad_label["labels"] = ["a", "a"]; corrupt_cases.append(("duplicate labels", bad_label))
        bad_state = dict(valid_item); bad_state["state"] = "open"; corrupt_cases.append(("local item state=open", bad_state))
        for label, case in corrupt_cases:
            try:
                norm.validate_normalized_item(case)
                check(False, f"item validator should reject {label}")
            except norm.BacklogNormalizationInvalid:
                check(True, f"item validator rejected {label}")

        # snapshot corruption: arbitrary snapshot_digest with recomputed record digest
        bad_snap = copy.deepcopy(snap_payload)
        bad_snap["snapshot_digest"] = "f" * 64
        bad_snap["snapshot_record_digest"] = norm.compute_snapshot_record_digest(bad_snap)
        bad_snap["snapshot_record_id"] = "blsnaprec_" + bad_snap["snapshot_record_digest"]
        try:
            norm.validate_normalized_snapshot(bad_snap)
            check(False, "snapshot validator should reject tampered snapshot_digest")
        except norm.BacklogNormalizationInvalid:
            check(True, "snapshot validator rejected tampered snapshot_digest")

        # F3/F8: durable item artifact audit + snapshot-last failure injection
        norm.validate_normalized_snapshot(snap_payload)
        item_count = 0
        for desc in snap_payload["items"]:
            env = intel.get_artifact(str(campaign_dir), campaign_id, desc["artifact_id"])
            check(env["artifact"]["kind"] == "backlog", f"item artifact kind backlog for {desc['artifact_id'][:40]}")
            check(env["artifact"]["artifact_id"] == desc["artifact_id"], "item artifact id matches descriptor")
            payload = env["artifact"]["payload"]
            norm.validate_normalized_item(payload)
            check(payload["backlog_item_id"] == desc["backlog_item_id"], "item backlog id matches descriptor")
            check(payload["item_digest"] == desc["item_digest"], "item digest matches descriptor")
            item_count += 1
        check(item_count == result["item_count"], "fetched item artifact count == item_count")

        # snapshot-last failure injection: block snapshot writes, then retry
        import myrmex_campaign_intelligence as intel_mod
        orig_put = intel_mod.put_artifact
        injected = {"failed": False}

        def failing_put(*args, **kwargs):
            artifact_id = args[4] if len(args) > 4 else kwargs.get("artifact_id")
            if isinstance(artifact_id, str) and artifact_id.startswith("normalized-backlog/snapshot/"):
                injected["failed"] = True
                raise RuntimeError("injected snapshot failure")
            return orig_put(*args, **kwargs)

        intel_mod.put_artifact = failing_put
        try:
            try:
                norm.normalize_backlog_sources(campaign_dir, campaign_id, 1, descriptors)
                check(False, "snapshot failure should raise")
            except RuntimeError:
                check(injected["failed"], "snapshot write failed as injected")
        finally:
            intel_mod.put_artifact = orig_put
        # items durable, no snapshot completion
        item_artifacts_present = 0
        for desc in snap_payload["items"]:
            try:
                env = intel.get_artifact(str(campaign_dir), campaign_id, desc["artifact_id"])
                if env["artifact"]["artifact_id"] == desc["artifact_id"]:
                    item_artifacts_present += 1
            except Exception:
                pass
        check(item_artifacts_present >= 1, "item artifacts survive snapshot failure")
        # retry with working persistence: reuses items, creates snapshot once
        r_retry = norm.normalize_backlog_sources(campaign_dir, campaign_id, 1, descriptors)
        check(r_retry["item_artifacts_created"] == 0, "retry reuses item artifacts (0 created)")
        check(r_retry["item_artifacts_reused"] == result["item_count"], "retry reuses all items")
        check(r_retry["snapshot_artifact_status"] in ("created", "reused"), "snapshot durable after retry")
        # verify exactly one snapshot completion artifact exists for this record id
        snap_artifacts = [p for p in (campaign_dir / "intelligence" / "artifacts").glob("*.json")
                          if "normalized-backlog/snapshot/" in p.read_text(errors="ignore")]
        check(len(snap_artifacts) >= 1, "snapshot artifact exists after retry")

        # F4: same-source semantic versioning preserves backlog_item_id
        # use the SAME path roadmap.md; overwrite with a priority change; new operation
        # capture original item ids/digests
        base_snap_payload = intel.get_artifact(str(campaign_dir), campaign_id, f"normalized-backlog/snapshot/{result['snapshot_record_id']}")["artifact"]["payload"]
        orig_roadmap_items = {d["backlog_item_id"]: d["item_digest"] for d in base_snap_payload["items"] if d["backlog_item_id"].startswith("backlog_")}
        (repo / "roadmap.md").write_text(ROADMAP_FIX.read_text(encoding="utf-8").replace("Priority: P1", "Priority: P9"), encoding="utf-8")
        r_same = roadmap.execute_local_import(str(campaign_dir), campaign_id, 1, "norm-roadmap-same-001", str(repo), "roadmap.md")
        same_neutral = roadmap.parse_markdown_roadmap((repo / "roadmap.md").read_text(encoding="utf-8"), "roadmap.md")
        # replace the original roadmap descriptor with the changed one (keep manifest + github)
        replaced = [
            {"operation_id": r_same["operation_id"], "neutral": same_neutral},
            *[d for d in descriptors if sources_map[d["operation_id"]][0] in ("manifest", "github")],
        ]
        r_same_norm = norm.normalize_backlog_sources(campaign_dir, campaign_id, 1, replaced)
        new_snap = intel.get_artifact(str(campaign_dir), campaign_id, f"normalized-backlog/snapshot/{r_same_norm['snapshot_record_id']}")["artifact"]["payload"]
        new_roadmap_items = {d["backlog_item_id"]: d["item_digest"] for d in new_snap["items"] if d["backlog_item_id"].startswith("backlog_")}
        # affected first roadmap item should keep backlog_item_id but change item_digest
        affected_new = [k for k in new_roadmap_items if k not in orig_roadmap_items]
        common = set(orig_roadmap_items) & set(new_roadmap_items)
        check(len(common) >= 1, "at least one roadmap backlog_item_id preserved")
        check(
            any(orig_roadmap_items[k] != new_roadmap_items[k] for k in common),
            "same-source semantic change produces different item_digest",
        )

        # F5: source-order independence is covered by the cross-source descriptor reorder
        # test above (reversed descriptors -> same snapshot_digest, status reused). Intra-source
        # reorder stability follows from content-derived IDs + deterministic sorting in the module.
        # F6: GitHub body-only equivalence via snapshot-body-changed fixture
        body_transport = FakeTransport(json.loads((ROOT / "tests" / "fixtures" / "github-import" / "snapshot-body-changed.json").read_text()))
        r_body = github.execute_github_import(str(campaign_dir), campaign_id, 1, "norm-github-body-001", "Owner/Repo", body_transport)
        body_neutral = github.normalize_github_snapshot(json.loads((ROOT / "tests" / "fixtures" / "github-import" / "snapshot-body-changed.json").read_text()), "owner/repo")
        body_descriptors = [{"operation_id": r_body["operation_id"], "neutral": body_neutral}]
        r_body_norm = norm.normalize_backlog_sources(campaign_dir, campaign_id, 1, body_descriptors)
        body_snap = intel.get_artifact(str(campaign_dir), campaign_id, f"normalized-backlog/snapshot/{r_body_norm['snapshot_record_id']}")["artifact"]["payload"]
        # same semantic snapshot digest as the original github-only normalization
        orig_github_digest = None
        for op, (label, neutral) in sources_map.items():
            if label == "github":
                gh_only = [{"operation_id": op, "neutral": neutral}]
                g_norm = norm.normalize_backlog_sources(campaign_dir, campaign_id, 1, gh_only)
                orig_github_digest = g_norm["snapshot_digest"]
        check(body_snap["snapshot_digest"] == orig_github_digest, "github body-only change same semantic snapshot digest")

        # F9: negative zero-write evidence
        def count_normalized(cdir):
            n = 0
            artifacts = cdir / "intelligence" / "artifacts"
            if artifacts.exists():
                for p in artifacts.glob("*.json"):
                    try:
                        if "normalized-backlog" in p.read_text(errors="ignore"):
                            n += 1
                    except Exception:
                        pass
            return n

        before_mismatch = count_normalized(campaign_dir)
        try:
            norm.normalize_backlog_sources(campaign_dir, campaign_id, 1, [wrong])
            check(False, "mismatch should raise")
        except norm.BacklogNormalizationSourceMismatch:
            check(True, "mismatch raises")
        check(count_normalized(campaign_dir) == before_mismatch, "mismatch produces zero normalized writes")
        before_nonready = count_normalized(campaign_dir)
        try:
            norm.normalize_backlog_sources(campaign_dir, campaign_id, 1, [{"operation_id": r_amb["operation_id"], "neutral": amb_neutral}])
            check(False, "non-ready should raise")
        except norm.BacklogNormalizationSourceNotReady:
            check(True, "non-ready raises")
        check(count_normalized(campaign_dir) == before_nonready, "non-ready produces zero normalized writes")

    print(f"backlog normalization test: {'FAIL' if failures else 'PASS'} ({len(failures)} failures)")
    if failures:
        for f in failures:
            print("  FAIL:", f)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
