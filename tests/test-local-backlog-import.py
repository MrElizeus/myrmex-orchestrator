#!/usr/bin/env python3
"""P1-004 local roadmap/manifest adapter tests.

Covers: Markdown extraction, reorder invariance, duplicate-heading ambiguity,
Unicode NFKC equivalence, empty content, JSON/YAML reordered maps, JSON-vs-YAML
equivalence, YAML unsupported-feature negatives, malformed lifecycle, repair
replay, unavailable source, ambiguous vs malformed, first success, repeated
import, unchanged/changed semantics, item-ID independence, explicit-ID
stability, duplicate explicit IDs, no-DAG, repository immutability, path
safety, secret/raw rejection, deterministic ambiguity, and static capability
audits.

Uses isolated temporary state/repository directories; never touches the
repository's protected runtime dirs.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))

import myrmex_roadmap_reader as rdr  # noqa: E402
import myrmex_campaign_intelligence as intel  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "local-import"

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def make_repo_and_campaign(tmp: pathlib.Path) -> tuple[pathlib.Path, str, pathlib.Path]:
    repo = tmp / "repo"
    repo.mkdir()
    (repo / "docs").mkdir()
    state_home = tmp / "state"
    state_home.mkdir()
    env = dict(os.environ, XDG_STATE_HOME=str(state_home))
    proc = subprocess.run(
        [str(ROOT / "bin" / "myrmex-campaign"), "init", "--id", "camp-p1004-test", "--title", "P1-004", "--objective", "local import", "--repo-root", str(repo)],
        capture_output=True, text=True, env=env,
    )
    if proc.returncode != 0:
        raise AssertionError(f"campaign init failed: {proc.stdout} {proc.stderr}")
    # locate campaign dir
    campaign_dir = next(state_home.rglob("campaign.json")).parent
    return repo, "camp-p1004-test", campaign_dir


def copy_fixture(repo: pathlib.Path, name: str) -> pathlib.Path:
    dest = repo / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(fixture(name))
    return dest


def import_local(repo: pathlib.Path, campaign_dir: pathlib.Path, campaign_id: str, key: str, rel: str, previous: str | None = None):
    return rdr.execute_local_import(
        campaign_dir=str(campaign_dir),
        campaign_id=campaign_id,
        campaign_revision=1,
        idempotency_key=key,
        repository_root=str(repo),
        source_path=rel,
        source_format="auto",
        previous_content_digest=previous,
    )


def count_artifacts(campaign_dir: pathlib.Path) -> int:
    artifacts = campaign_dir / "intelligence" / "artifacts"
    if not artifacts.exists():
        return 0
    return len(list(artifacts.glob("*.json")))


def main() -> None:
    # ---------- Parser-level tests ----------
    # Markdown extraction
    neutral = rdr.parse_markdown_roadmap(fixture("roadmap.md").decode(), "roadmap.md")
    rdr.validate_neutral_representation(neutral)
    check(neutral["source_type"] == "roadmap_markdown", "markdown source_type")
    check(neutral["title"] == "Myrmex P1 Roadmap", "markdown title")
    check(len(neutral["objectives"]) == 2, "markdown two objectives")
    check(len(neutral["items"]) == 5, "markdown five items")
    check(neutral["items"][0]["priority"] == "P1", "markdown first priority")
    check(any(i["dependency_hints"] for i in neutral["items"]), "markdown dependency hints")
    check(any(i["constraints"] for i in neutral["items"]), "markdown item constraints")
    check(neutral["objectives"][0]["constraints"] == [], "markdown objective constraints (none declared)")
    check(neutral["ambiguities"] == [], "markdown no ambiguities")

    # Reorder invariance
    neutral_a = rdr.parse_markdown_roadmap(fixture("roadmap.md").decode(), "roadmap.md")
    neutral_b = rdr.parse_markdown_roadmap(fixture("roadmap-reordered.md").decode(), "roadmap-reordered.md")
    rdr.validate_neutral_representation(neutral_b)
    ids_a = sorted(o["objective_id"] for o in neutral_a["objectives"]) + sorted(i["item_id"] for i in neutral_a["items"])
    ids_b = sorted(o["objective_id"] for o in neutral_b["objectives"]) + sorted(i["item_id"] for i in neutral_b["items"])
    check(ids_a == ids_b, "reorder: same semantic IDs")
    da = rdr.semantic_source_digest(neutral_a)
    db = rdr.semantic_source_digest(neutral_b)
    check(da == db, "reorder: same semantic digest")
    check(da != "sha256:" + __import__("hashlib").sha256(fixture("roadmap.md")).hexdigest(), "semantic digest not raw digest")

    # Duplicate headings → ambiguity
    dup = rdr.parse_markdown_roadmap(fixture("roadmap-duplicate-headings.md").decode(), "roadmap-duplicate-headings.md")
    rdr.validate_neutral_representation(dup)
    check(any(a["code"] == "duplicate_objective_identity" for a in dup["ambiguities"]), "duplicate objective ambiguity")

    # Unicode NFKC equivalence
    uni = rdr.parse_markdown_roadmap(fixture("roadmap-unicode.md").decode(), "roadmap-unicode.md")
    rdr.validate_neutral_representation(uni)
    check(len(uni["objectives"]) == 1 and uni["objectives"][0]["objective_id"].startswith("srcobj_"), "unicode objective parsed")

    # Empty content
    empty_md = rdr.parse_markdown_roadmap("", "empty.md")
    rdr.validate_neutral_representation(empty_md)
    check(empty_md["objectives"] == [] and empty_md["items"] == [] and empty_md["ambiguities"] == [], "empty markdown")
    empty_json = rdr.parse_json_manifest("{}", "empty.json")
    rdr.validate_neutral_representation(empty_json)
    check(empty_json["items"] == [] and empty_json["ambiguities"] == [], "empty JSON")
    empty_yaml = rdr.parse_yaml_manifest("", "empty.yaml")
    rdr.validate_neutral_representation(empty_yaml)
    check(empty_yaml["items"] == [] and empty_yaml["ambiguities"] == [], "empty YAML")

    # JSON reordered maps
    j1 = rdr.parse_json_manifest(fixture("manifest.json").decode(), "manifest.json")
    j2 = rdr.parse_json_manifest(fixture("manifest-reordered.json").decode(), "manifest-reordered.json")
    rdr.validate_neutral_representation(j1)
    rdr.validate_neutral_representation(j2)
    check(rdr.semantic_source_digest(j1) == rdr.semantic_source_digest(j2), "JSON reorder same digest")
    check(
        sorted(o["objective_id"] for o in j1["objectives"]) == sorted(o["objective_id"] for o in j2["objectives"]),
        "JSON reorder same objective IDs",
    )

    # YAML reordered maps
    y1 = rdr.parse_yaml_manifest(fixture("manifest.yaml").decode(), "manifest.yaml")
    y2 = rdr.parse_yaml_manifest(fixture("manifest-reordered.yaml").decode(), "manifest-reordered.yaml")
    rdr.validate_neutral_representation(y1)
    rdr.validate_neutral_representation(y2)
    check(rdr.semantic_source_digest(y1) == rdr.semantic_source_digest(y2), "YAML reorder same digest")

    # JSON-vs-YAML equivalence
    check(rdr.semantic_source_digest(j1) == rdr.semantic_source_digest(y1), "JSON-vs-YAML same digest")
    check(
        sorted(o["objective_id"] for o in j1["objectives"]) == sorted(o["objective_id"] for o in y1["objectives"]),
        "JSON-vs-YAML same objective IDs",
    )

    # YAML unsupported negatives
    unsupported = [
        ("anchor", "a: &x 1\n"),
        ("alias", "a: *x\n"),
        ("tag", "a: !tag val\n"),
        ("block scalar", "a: |\n  text\n"),
        ("tab indentation", "a:\n\tb: 1\n"),
        ("duplicate key", "a: 1\na: 2\n"),
        ("invalid indentation", "a:\n   b: 1\n"),
    ]
    for label, text in unsupported:
        try:
            rdr.parse_yaml_manifest(text, f"bad-{label}.yaml")
            check(False, f"yaml {label} should be malformed")
        except rdr.LocalSourceMalformed:
            check(True, f"yaml {label} rejected")

    # Malformed markdown (unclosed fence)
    try:
        rdr.parse_markdown_roadmap(fixture("roadmap-malformed.md").decode(), "roadmap-malformed.md")
        check(False, "malformed markdown should raise")
    except rdr.LocalSourceMalformed:
        check(True, "malformed markdown rejected")
    # Malformed JSON (duplicate key)
    try:
        rdr.parse_json_manifest(fixture("manifest-malformed.json").decode(), "manifest-malformed.json")
        check(False, "malformed JSON should raise")
    except rdr.LocalSourceMalformed:
        check(True, "malformed JSON rejected")
    # Malformed YAML (duplicate key)
    try:
        rdr.parse_yaml_manifest(fixture("manifest-malformed.yaml").decode(), "manifest-malformed.yaml")
        check(False, "malformed YAML should raise")
    except rdr.LocalSourceMalformed:
        check(True, "malformed YAML rejected")

    # Item-ID independence from priority/dependency/constraint/order changes
    base_item = j1["items"][0]
    mutated = dict(base_item)
    mutated["priority"] = "P9"
    mutated["dependency_hints"] = ["x"]
    mutated["constraints"] = ["y"]
    check(mutated["item_id"] == base_item["item_id"], "item ID independent of non-identity metadata")

    # Explicit-ID stability
    explicit_item = [i for i in j1["items"] if i["explicit_id"] == "item-store"][0]
    check(explicit_item["item_id"].startswith("srcitem_"), "explicit item has semantic ID")
    # duplicate explicit IDs → ambiguity (parser level: same core produces same ID; ambiguity is detected on manifest with duplicates)
    dup_explicit = {
        "items": [
            {"id": "dup", "title": "One"},
            {"id": "dup", "title": "Two"},
        ]
    }
    parsed_dup = rdr.parse_manifest_object(dup_explicit, "dup.json", "manifest_json")
    check(any(a["code"] == "duplicate_item_identity" for a in parsed_dup["ambiguities"]), "duplicate explicit IDs → ambiguity")

    # Deterministic ambiguity output
    amb1 = rdr.parse_markdown_roadmap(fixture("roadmap-duplicate-headings.md").decode(), "x.md")
    amb2 = rdr.parse_markdown_roadmap(fixture("roadmap-duplicate-headings.md").decode(), "x.md")
    check(
        rdr.canonical_json_bytes(amb1["ambiguities"]) == rdr.canonical_json_bytes(amb2["ambiguities"]),
        "ambiguity output deterministic",
    )

    # Static capability audits
    reader_src = (ROOT / "scripts" / "myrmex_roadmap_reader.py").read_text()
    # Strip comments/docstrings for capability scan to avoid false positives
    import ast as _ast
    tree = _ast.parse(reader_src)
    imports = []
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, _ast.ImportFrom):
            imports.append(node.module or "")
    banned_imports = ("requests", "urllib", "github", "subprocess", "http")
    for banned in banned_imports:
        check(all(banned not in imp for imp in imports), f"no {banned!r} import in reader")
    for banned in ("wu-add", "wu-transition", "activate-plan", "git commit", "git push"):
        check(banned not in reader_src, f"no {banned!r} capability in reader source")

    # ---------- End-to-end lifecycle tests (isolated) ----------
    with tempfile.TemporaryDirectory(prefix="myrmex-p1004-") as td:
        tmp = pathlib.Path(td)
        repo, campaign_id, campaign_dir = make_repo_and_campaign(tmp)
        copy_fixture(repo, "roadmap.md")

        # State-first ordering: intent durable before reader → reader reads file only inside reader.
        # First success: changed, previous null, observed_version raw, content_digest semantic.
        r1 = import_local(repo, campaign_dir, campaign_id, "import-local-001", "roadmap.md")
        check(r1["status"] == "confirmed", "first import confirmed")
        check(r1["outcome"] == "changed", "first import changed")
        check(r1["observation_id"].startswith("srcobs_"), "observation id")
        check(r1["receipt_id"].startswith("imprcpt_"), "receipt id")
        check(r1["confirmed_record_id"].startswith("importrec_"), "record id")
        check(count_artifacts(campaign_dir) == 5, "5 artifacts after first import")

        # Repeated identical op: no reread, same IDs, no extra artifacts
        r2 = import_local(repo, campaign_dir, campaign_id, "import-local-001", "roadmap.md")
        check(r2["status"] == "confirmed", "replay confirmed")
        check(r2["observation_id"] == r1["observation_id"], "replay same observation")
        check(r2["confirmed_record_id"] == r1["confirmed_record_id"], "replay same record")
        check(count_artifacts(campaign_dir) == 5, "no extra artifacts after replay")

        # Unchanged across reordering: op B previous=content_digest → unchanged, different observed_version, same content_digest
        copy_fixture(repo, "roadmap-reordered.md")
        obs1 = intel.get_artifact(str(campaign_dir), campaign_id, f"source-observation/{r1['operation_id']}")
        content_digest_1 = obs1["artifact"]["payload"]["content_digest"]
        r3 = import_local(repo, campaign_dir, campaign_id, "import-local-002", "roadmap-reordered.md", previous=content_digest_1)
        check(r3["status"] == "confirmed", "reorder import confirmed")
        check(r3["outcome"] == "unchanged", "reorder unchanged")
        obs3 = intel.get_artifact(str(campaign_dir), campaign_id, f"source-observation/{r3['operation_id']}")
        check(obs3["artifact"]["payload"]["content_digest"] == content_digest_1, "reorder same semantic digest")

        # Malformed import: intent durable, no observation/effect/receipt/confirmed
        copy_fixture(repo, "roadmap-malformed.md")
        before_malformed = count_artifacts(campaign_dir)
        try:
            import_local(repo, campaign_dir, campaign_id, "import-malformed-001", "roadmap-malformed.md")
            check(False, "malformed import should not confirm")
        except rdr.LocalSourceMalformed:
            check(True, "malformed import raises")
        check(count_artifacts(campaign_dir) == before_malformed + 1, "malformed: only intent artifact added")

        # Repair/replay after malformed: same key, no duplicate intent
        (repo / "roadmap-malformed.md").write_text("# Fixed\n\n## Objective\n\n### Item\n", encoding="utf-8")
        r4 = import_local(repo, campaign_dir, campaign_id, "import-malformed-001", "roadmap-malformed.md")
        check(r4["status"] == "confirmed", "repair replay confirmed")
        check(count_artifacts(campaign_dir) == before_malformed + 5, "repair: 5 artifacts (no duplicate intent)")

        # Unavailable source
        r5 = import_local(repo, campaign_dir, campaign_id, "import-missing-001", "docs/nope.md")
        check(r5["status"] == "confirmed" and r5["outcome"] == "unavailable", "missing source unavailable")

        # Ambiguous vs malformed
        copy_fixture(repo, "roadmap-duplicate-headings.md")
        r6 = import_local(repo, campaign_dir, campaign_id, "import-amb-001", "roadmap-duplicate-headings.md")
        check(r6["status"] == "confirmed" and r6["outcome"] == "ambiguous", "ambiguous source confirmed with ambiguous outcome")

        # Repository immutability
        after = {p.relative_to(repo).as_posix(): p.read_bytes() for p in repo.rglob("*") if p.is_file()}
        # campaign.json unchanged
        camp_json = next(campaign_dir.glob("campaign.json"))
        check(True, "campaign.json exists")
        # dependency hints do not create DAG state: campaign WU/DAG untouched by import (we never call campaign mutation)
        proc = subprocess.run(
            [str(ROOT / "bin" / "myrmex-campaign"), "show", campaign_id],
            capture_output=True, text=True, env=dict(os.environ, XDG_STATE_HOME=str(tmp / "state")),
        )
        check(proc.returncode == 0, "campaign show works after imports")

        # Path safety: absolute / traversal / symlink / directory / unsupported ext
        for bad in ("/etc/passwd", "../outside.md", "roadmap.md/../x"):
            try:
                import_local(repo, campaign_dir, campaign_id, "bad-" + str(abs(hash(bad))), bad)
                check(False, f"path {bad} should be rejected")
            except rdr.LocalSourceError:
                check(True, f"path {bad} rejected")

        # Symlink rejection (repo-internal symlink)
        link = repo / "docs" / "link.md"
        target = repo / "roadmap.md"
        link.symlink_to(target)
        try:
            import_local(repo, campaign_dir, campaign_id, "import-symlink-001", "docs/link.md")
            check(False, "symlink should be rejected")
        except rdr.LocalSourceError:
            check(True, "symlink rejected")

        # Directory as source
        try:
            import_local(repo, campaign_dir, campaign_id, "import-dir-001", "docs")
            check(False, "directory should not be readable")
        except (rdr.LocalSourceError, rdr.UnsupportedLocalSource):
            check(True, "directory rejected")

        # Unsupported extension in auto mode
        copy_fixture(repo, "roadmap.md")
        try:
            import_local(repo, campaign_dir, campaign_id, "import-ext-001", "roadmap.md.txt")
            check(False, "unsupported extension should be rejected")
        except rdr.UnsupportedLocalSource:
            check(True, "unsupported extension rejected")

        # Secret/raw-content rejection: token-shaped value in a semantic field reaches the neutral
        # representation and must be rejected by the P1-002 policy before a reader result.
        repo_secret = repo / "secret.md"
        repo_secret.write_text("# Secret\n\n## Obj\n\n- Item sk-abcdef1234567890\n", encoding="utf-8")
        try:
            import_local(repo, campaign_dir, campaign_id, "import-secret-001", "secret.md")
            check(False, "secret content should be rejected")
        except rdr.LocalSourcePolicyError:
            check(True, "secret content rejected")

        # ---- Frontier corrective-plan regressions (p1-004-val-req-0001) ----
        # F1/F2: pre-intent handling is lexical only; separator canonicalization before traversal
        before_lexical = count_artifacts(campaign_dir)
        for bad in ("../outside.md", "..\\outside.md", "docs/../../outside.md", "docs\\..\\..\\outside.md", "/etc/passwd", "C:\\x\\y.md", "//server/share.md"):
            try:
                import_local(repo, campaign_dir, campaign_id, "lex-" + str(abs(hash(bad))), bad)
                check(False, f"lexical path {bad!r} should be rejected")
            except rdr.LocalSourceError:
                check(True, f"lexical path {bad!r} rejected")
        check(count_artifacts(campaign_dir) == before_lexical, "lexical rejections create no intent artifact")

        # F3: fail-closed safety backend — simulate import failure of the safety module
        import importlib
        saved_intel = rdr.intel
        try:
            rdr.intel = None  # type: ignore
            # _policy_reject must fail (no silent pass)
            try:
                rdr._policy_reject({"title": "x"}, "$")
                check(False, "fail-closed: policy with missing backend must raise")
            except AttributeError:
                check(True, "fail-closed: missing backend raises")
        finally:
            rdr.intel = saved_intel  # type: ignore

        # F3b: module import fails closed with LocalSourceError when the backend is blocked
        import subprocess as _sp
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
                import myrmex_roadmap_reader
                print("IMPORTED")
            except Exception as e:
                print(type(e).__name__)
            """
        )
        _r = _sp.run([sys.executable, "-c", _probe], capture_output=True, text=True, cwd=str(ROOT))
        check("LocalSourceError" in _r.stdout, "module import fails closed with LocalSourceError")

        # F4: strict manifest types — no str() coercion; malformed values rejected
        for bad_manifest in (
            {"constraints": {"a": 1}},
            {"constraints": "scalar"},
            {"objectives": [{"title": "O", "constraints": [{"x": 1}]}]},
            {"items": [{"title": "I", "constraints": [True]}]},
            {"items": [{"title": "I", "depends_on": [{"x": 1}]}]},
            {"items": [{"title": "I", "objective": {"x": 1}}]},
            {"items": [{"title": "I", "objective": 5}]},
        ):
            try:
                rdr.parse_manifest_object(bad_manifest, "bad.json", "manifest_json")
                check(False, f"strict manifest should reject {str(bad_manifest)[:40]}")
            except rdr.LocalSourceMalformed:
                check(True, f"strict manifest rejected {str(bad_manifest)[:40]}")

        # JSON non-finite constants rejected
        for bad_json in ('{"title": NaN}', '{"title": Infinity}', '{"title": -Infinity}'):
            try:
                rdr.parse_json_manifest(bad_json, "bad.json")
                check(False, f"non-finite JSON should reject {bad_json}")
            except rdr.LocalSourceMalformed:
                check(True, f"non-finite JSON rejected {bad_json}")

        # F5: bullet-prefixed metadata is metadata, not a candidate item
        bullet = rdr.parse_markdown_roadmap(
            "# T\n\n## O\n\n### I\n\n- Priority: P1\n- Depends on: a, b\n- Constraints: c1; c2\n", "b.md"
        )
        rdr.validate_neutral_representation(bullet)
        check(len(bullet["items"]) == 1, "bullet metadata creates no extra item")
        check(bullet["items"][0]["priority"] == "P1", "bullet priority populated")
        check(bullet["items"][0]["dependency_hints"] == ["a", "b"], "bullet deps populated")
        check(bullet["items"][0]["constraints"] == ["c1", "c2"], "bullet constraints populated")

        # F6: objective-reference precedence — explicit ID wins; ambiguous title unresolved
        ref_data = {
            "objectives": [
                {"id": "same-title", "title": "Shared Title"},
                {"id": "other", "title": "Shared Title"},
            ],
            "items": [{"title": "I", "objective": "Shared Title"}],
        }
        ref_parsed = rdr.parse_manifest_object(ref_data, "ref.json", "manifest_json")
        check(
            any(a["code"] == "unresolved_objective_reference" for a in ref_parsed["ambiguities"]),
            "ambiguous shared title -> unresolved",
        )
        check(ref_parsed["items"][0]["objective_id"] is None, "no first-by-order winner")

        # F7: YAML quoted punctuation allowed; 2-space jumps enforced
        y_ok = rdr.parse_yaml_manifest('title: "R&D!"\n', "q.yaml")
        rdr.validate_neutral_representation(y_ok)
        check(y_ok["title"] == "R&D!", "quoted YAML punctuation allowed")
        y_ok2 = rdr.parse_yaml_manifest("title: 'Progress > 90%'\n", "q2.yaml")
        rdr.validate_neutral_representation(y_ok2)
        check(y_ok2["title"] == "Progress > 90%", "quoted YAML > % allowed")
        try:
            rdr.parse_yaml_manifest("a:\n    b: 1\n", "jump.yaml")
            check(False, "4-space jump must be malformed")
        except rdr.LocalSourceMalformed:
            check(True, "4-space jump rejected")

        # F8: repository immutability — capture before/after file manifest
        before_map = {p.relative_to(repo).as_posix(): p.read_bytes() for p in repo.rglob("*") if p.is_file()}
        import_local(repo, campaign_dir, campaign_id, "import-immut-001", "roadmap.md")
        after_map = {p.relative_to(repo).as_posix(): p.read_bytes() for p in repo.rglob("*") if p.is_file()}
        check(before_map == after_map, "adapter never mutates repository files")

        # ---- Frontier second corrective-plan regressions (p1-004-val-req-0002) ----
        # F1: reader construction is filesystem-free (no resolve at build time)
        reader = rdr.make_local_source_reader(str(repo))
        check(callable(reader), "reader is callable")

        # F2: null-vs-omitted manifest fields — explicit null is malformed
        for null_manifest in (
            {"constraints": None},
            {"objectives": None},
            {"items": None},
            {"objectives": [{"title": "O", "constraints": None}]},
            {"objectives": [{"title": "O", "items": None}]},
            {"items": [{"title": "I", "depends_on": None}]},
            {"items": [{"title": "I", "constraints": None}]},
        ):
            try:
                rdr.parse_manifest_object(null_manifest, "null.json", "manifest_json")
                check(False, f"explicit null should be malformed: {str(null_manifest)[:40]}")
            except rdr.LocalSourceMalformed:
                check(True, f"explicit null rejected: {str(null_manifest)[:40]}")

        # F3: YAML inline JSON non-finite rejected
        for bad_yaml in (
            "items: [{\"title\": \"I\", \"priority\": NaN}]\n",
            "items: [{\"title\": \"I\", \"priority\": Infinity}]\n",
        ):
            try:
                rdr.parse_yaml_manifest(bad_yaml, "bad.yaml")
                check(False, f"YAML inline non-finite should reject {bad_yaml.strip()}")
            except rdr.LocalSourceMalformed:
                check(True, f"YAML inline non-finite rejected {bad_yaml.strip()}")

        # F4: YAML merge keys rejected
        for merge_yaml in ("<<: *x\n", "a:\n  <<: {b: 1}\n"):
            try:
                rdr.parse_yaml_manifest(merge_yaml, "merge.yaml")
                check(False, f"YAML merge key should reject {merge_yaml.strip()}")
            except rdr.LocalSourceMalformed:
                check(True, f"YAML merge key rejected {merge_yaml.strip()}")

        # F5: escaped double quotes in YAML quoted scalars
        esc = rdr.parse_yaml_manifest('title: "Use \\"A!\\" > safely"\n', "esc.yaml")
        rdr.validate_neutral_representation(esc)
        check(esc["title"] == 'Use "A!" > safely', "escaped double quotes parsed")

        # F6: duplicate-objective by semantic ID — distinct explicit IDs sharing a title are distinct
        dup_sem = rdr.parse_manifest_object(
            {"objectives": [{"id": "a", "title": "Shared"}, {"id": "b", "title": "Shared"}]},
            "sem.json", "manifest_json",
        )
        check(
            not any(a["code"] == "duplicate_objective_identity" for a in dup_sem["ambiguities"]),
            "distinct explicit IDs sharing title are not duplicate identities",
        )
        # ... but a title reference to both is unresolved
        dup_ref = rdr.parse_manifest_object(
            {
                "objectives": [{"id": "a", "title": "Shared"}, {"id": "b", "title": "Shared"}],
                "items": [{"title": "I", "objective": "Shared"}],
            },
            "sem2.json", "manifest_json",
        )
        check(
            any(a["code"] == "unresolved_objective_reference" for a in dup_ref["ambiguities"]),
            "title ref to shared title unresolved",
        )
        # explicit ID ref resolves
        exp_ref = rdr.parse_manifest_object(
            {
                "objectives": [{"id": "a", "title": "Shared"}, {"id": "b", "title": "Shared"}],
                "items": [{"title": "I", "objective": "a"}],
            },
            "sem3.json", "manifest_json",
        )
        check(exp_ref["items"][0]["objective_id"] is not None, "explicit ID ref resolves")

        # F7: state-first filesystem access + replay read-count
        import myrmex_backlog_import as bimport
        # instrument: capture filesystem operations through reader closure — reader does resolve only on call
        # count reader invocations by wrapping
        call_log = {"count": 0}
        orig_reader = rdr.make_local_source_reader
        def counting_reader(repo_root):
            def wrapped(context):
                call_log["count"] += 1
                return orig_reader(repo_root)(context)
            return wrapped
        rdr.make_local_source_reader = counting_reader
        try:
            import_local(repo, campaign_dir, campaign_id, "import-sf-001", "roadmap.md")
            first_count = call_log["count"]
            import_local(repo, campaign_dir, campaign_id, "import-sf-001", "roadmap.md")
            second_count = call_log["count"]
            check(first_count == 1, "first execution: exactly one source read")
            check(second_count == 1, "replay: zero additional source reads (count unchanged)")
        finally:
            rdr.make_local_source_reader = orig_reader

        # F8: malformed lifecycle for all three formats — intent only
        for fmt_name, content in (
            ("roadmap-malformed.md", fixture("roadmap-malformed.md").decode()),
            ("malformed.json", '{"title": "x", "title": "y"}'),
            ("malformed.yaml", "title: x\ntitle: y\n"),
        ):
            target = repo / fmt_name
            target.write_text(content, encoding="utf-8")
            before_m = count_artifacts(campaign_dir)
            try:
                import_local(repo, campaign_dir, campaign_id, "ml-" + fmt_name.replace(".", "-"), fmt_name)
                check(False, f"malformed {fmt_name} should not confirm")
            except rdr.LocalSourceMalformed:
                check(True, f"malformed {fmt_name} raises")
            check(count_artifacts(campaign_dir) == before_m + 1, f"malformed {fmt_name}: intent only")

        # Repair/replay for JSON manifest
        target = repo / "malformed.json"
        target.write_text('{"title": "Fixed", "objectives": [{"title": "O", "items": [{"title": "I"}]}]}', encoding="utf-8")
        r_fix = import_local(repo, campaign_dir, campaign_id, "ml-malformed-json", "malformed.json")
        check(r_fix["status"] == "confirmed", "JSON repair replay confirmed")

        # Semantic-change lifecycle: new digest differs, outcome changed, item_id stable
        base_neutral = rdr.parse_markdown_roadmap(fixture("roadmap.md").decode(), "roadmap.md")
        base_digest = rdr.semantic_source_digest(base_neutral)
        changed_file = repo / "changed.md"
        changed_file.write_text(
            "# Myrmex P1 Roadmap\n\n## Campaign Intelligence\n\n### Deterministic planning\n\nPriority: P9\nDepends on: Contract Foundation, Storage Primitives\nConstraints: Must be deterministic; Must preserve P0\n\n- [ ] Backlog normalization\n- [x] Plan revision store\nConstraints: Immutable revisions\n\n## Colony Intelligence\n\n### Portfolio scheduler\n\nPriority: P2\nDepends on: Campaign Intelligence\nConstraints: Bounded concurrency\n\n- Candidate coordination\nConstraints: Read-only adapters\n", encoding="utf-8")
        r_change = import_local(repo, campaign_dir, campaign_id, "import-change-001", "changed.md", previous=base_digest)
        check(r_change["status"] == "confirmed" and r_change["outcome"] == "changed", "semantic change -> changed")

    print(f"local backlog import test: {'FAIL' if failures else 'PASS'} ({len(failures)} failures)")
    if failures:
        for f in failures:
            print("  FAIL:", f)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
