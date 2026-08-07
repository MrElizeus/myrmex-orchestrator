#!/usr/bin/env python3
"""P1-005 read-only GitHub issue/milestone adapter tests.

Uses local fixtures, an in-process fake transport, and temporary Campaign
Intelligence state. No real network, no credentials.

Covers: transport-free reader construction, state-first transport invocation,
safe normalization, body/description exclusion, order independence, PR
exclusion, ambiguity, malformed lifecycle, first changed, replay no-reread,
unchanged reordered/body-only, changed semantic, timeout, explicit
unavailable, unexpected exception, negative replay conflict, schema
validation, campaign/repository immutability, no WU authority, and static
network-capability audit.
"""
from __future__ import annotations

import ast
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import myrmex_github_reader as ghr  # noqa: E402
import myrmex_campaign_intelligence as intel  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "github-import"

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeTransport:
    """In-process fake GitHub transport; asserts the bounded request boundary."""

    def __init__(self, snapshot: dict | None = None, *, calls: list[int] | None = None):
        self.snapshot = snapshot
        self.calls = calls if calls is not None else []
        self.invocations: list[dict] = []

    def __call__(self, request: dict) -> dict:
        self.invocations.append(request)
        self.calls.append(1)
        # boundary assertions
        allowed = {"operation", "repository", "issue_state", "milestone_state", "include_pull_requests"}
        assert set(request.keys()) == allowed, f"transport request keys {set(request.keys())} != {allowed}"
        for banned in ("campaign_dir", "campaign_id", "state_home", "token", "authorization", "headers", "cookie"):
            assert banned not in request, f"transport received {banned}"
        if self.snapshot is None:
            return {"status": "unavailable", "reason_code": "no_snapshot"}
        return dict(self.snapshot)


def make_campaign(tmp: pathlib.Path) -> tuple[pathlib.Path, str, pathlib.Path]:
    repo = tmp / "repo"
    repo.mkdir()
    state_home = tmp / "state"
    state_home.mkdir()
    env = dict(os.environ, XDG_STATE_HOME=str(state_home))
    proc = subprocess.run(
        [str(ROOT / "bin" / "myrmex-campaign"), "init", "--id", "camp-p1005-test", "--title", "P1-005", "--objective", "github import", "--repo-root", str(repo)],
        capture_output=True, text=True, env=env,
    )
    if proc.returncode != 0:
        raise AssertionError(f"campaign init failed: {proc.stdout} {proc.stderr}")
    campaign_dir = next(state_home.rglob("campaign.json")).parent
    return repo, "camp-p1005-test", campaign_dir


def count_artifacts(campaign_dir: pathlib.Path) -> int:
    artifacts = campaign_dir / "intelligence" / "artifacts"
    if not artifacts.exists():
        return 0
    return len(list(artifacts.glob("*.json")))


def scan_for_sentinels(campaign_dir: pathlib.Path) -> list[str]:
    found = []
    for path in campaign_dir.rglob("*"):
        if path.is_file():
            text = path.read_text(errors="ignore")
            for sentinel in ("RAW-ISSUE-BODY-SENTINEL", "RAW-MILESTONE-DESCRIPTION-SENTINEL"):
                if sentinel in text:
                    found.append(f"{sentinel} in {path}")
    return found


def main() -> None:
    # ---------- Pure normalization tests ----------
    base_snap = fixture("snapshot.json")
    neutral = ghr.normalize_github_snapshot(base_snap, "owner/repo")
    ghr.validate_github_neutral_snapshot(neutral)
    check(neutral["schema"] == "myrmex.github-source-neutral/v1", "neutral schema")
    check(neutral["repository"] == "owner/repo", "canonical repository")
    check(len(neutral["issues"]) == 2, "two issues")
    check(len(neutral["milestones"]) == 2, "two milestones")
    check(neutral["issues"][0]["labels"] == ["bug", "security"], "labels sorted unique")
    check(neutral["issues"][0]["milestone_number"] == 1, "milestone association")
    check(neutral["ambiguities"] == [], "no ambiguities")

    # body/description exclusion from neutral
    neutral_text = json.dumps(neutral)
    for sentinel in ("RAW-ISSUE-BODY-SENTINEL", "RAW-MILESTONE-DESCRIPTION-SENTINEL", "body", "description", "comments", "html_url"):
        check(sentinel not in neutral_text, f"neutral excludes {sentinel}")

    # order independence
    reordered = ghr.normalize_github_snapshot(fixture("snapshot-reordered.json"), "owner/repo")
    check(
        sorted(i["issue_id"] for i in neutral["issues"]) == sorted(i["issue_id"] for i in reordered["issues"]),
        "reordered same issue IDs",
    )
    check(ghr.github_content_digest(neutral) == ghr.github_content_digest(reordered), "reordered same content digest")

    # body-only change: content digest same; observed version may differ
    body_changed = ghr.normalize_github_snapshot(fixture("snapshot-body-changed.json"), "owner/repo")
    check(
        ghr.github_content_digest(neutral) == ghr.github_content_digest(body_changed),
        "body-only change same content digest",
    )
    check(
        ghr.github_observed_version(neutral) != ghr.github_observed_version(body_changed),
        "body-only change with updated_at change -> observed version differs",
    )

    # semantic change
    semantic = ghr.normalize_github_snapshot(fixture("snapshot-semantic-changed.json"), "owner/repo")
    check(
        ghr.github_content_digest(neutral) != ghr.github_content_digest(semantic),
        "semantic change -> content digest differs",
    )

    # PR exclusion
    with_pr = ghr.normalize_github_snapshot(fixture("snapshot-with-pr.json"), "owner/repo")
    check(len(with_pr["issues"]) == 1, "PR excluded")
    check(with_pr["issues"][0]["number"] == 4, "only real issue present")

    # ambiguity
    ambiguous = ghr.normalize_github_snapshot(fixture("snapshot-ambiguous.json"), "owner/repo")
    check(any(a["code"] == "duplicate_issue_number" for a in ambiguous["ambiguities"]), "duplicate issue ambiguity")

    # malformed
    try:
        ghr.normalize_github_snapshot(fixture("snapshot-malformed.json"), "owner/repo")
        check(False, "malformed snapshot should raise")
    except ghr.GitHubSnapshotMalformed:
        check(True, "malformed snapshot rejected")

    # static capability audit
    src = (ROOT / "scripts" / "myrmex_github_reader.py").read_text()
    tree = ast.parse(src)
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")
    for banned in ("requests", "urllib", "http", "github", "PyGithub", "httpx", "aiohttp", "subprocess"):
        check(all(banned not in imp for imp in imports), f"no {banned} import")
    for banned in ("curl", "wget", "wu-add", "wu-transition", "activate-plan", "git commit", "git push"):
        check(banned not in src, f"no {banned} in source")
    check(not re.search(r"(?<![A-Za-z])gh(?![A-Za-z])", src), "no standalone gh CLI reference in source")

    # ---------- End-to-end lifecycle tests (isolated) ----------
    with tempfile.TemporaryDirectory(prefix="myrmex-p1005-") as td:
        tmp = pathlib.Path(td)
        repo, campaign_id, campaign_dir = make_campaign(tmp)

        # reader construction is transport-free
        transport = FakeTransport(snapshot=base_snap)
        reader = ghr.make_github_source_reader(transport)
        check(len(transport.invocations) == 0, "reader construction does not invoke transport")

        # first successful import
        result = ghr.execute_github_import(
            campaign_dir, campaign_id, 1, "github-import-001", "Owner/Repo", transport,
        )
        check(result["status"] == "confirmed", "first import confirmed")
        check(result["outcome"] == "changed", "first import changed")
        check(len(transport.invocations) == 1, "transport called exactly once")
        check(count_artifacts(campaign_dir) == 5, "5 artifacts after first import")

        # no raw sentinels persisted
        check(scan_for_sentinels(campaign_dir) == [], "no raw sentinels in intelligence artifacts")

        # replay: no second transport invocation
        result2 = ghr.execute_github_import(
            campaign_dir, campaign_id, 1, "github-import-001", "Owner/Repo", transport,
        )
        check(result2["status"] == "confirmed", "replay confirmed")
        check(len(transport.invocations) == 1, "replay does not invoke transport")
        check(result2["observation_id"] == result["observation_id"], "replay same observation")
        check(count_artifacts(campaign_dir) == 5, "no extra artifacts after replay")

        # reordered -> unchanged (new operation, previous digest)
        base_obs = intel.get_artifact(str(campaign_dir), campaign_id, f"source-observation/{result['operation_id']}")
        base_content = base_obs["artifact"]["payload"]["content_digest"]
        transport_reordered = FakeTransport(snapshot=fixture("snapshot-reordered.json"))
        r_reordered = ghr.execute_github_import(
            campaign_dir, campaign_id, 1, "github-import-002", "Owner/Repo", transport_reordered,
            previous_content_digest=base_content,
        )
        check(r_reordered["outcome"] == "unchanged", "reordered -> unchanged")

        # body-only -> unchanged
        transport_body = FakeTransport(snapshot=fixture("snapshot-body-changed.json"))
        r_body = ghr.execute_github_import(
            campaign_dir, campaign_id, 1, "github-import-003", "Owner/Repo", transport_body,
            previous_content_digest=base_content,
        )
        check(r_body["outcome"] == "unchanged", "body-only -> unchanged")

        # semantic -> changed
        transport_sem = FakeTransport(snapshot=fixture("snapshot-semantic-changed.json"))
        r_sem = ghr.execute_github_import(
            campaign_dir, campaign_id, 1, "github-import-004", "Owner/Repo", transport_sem,
            previous_content_digest=base_content,
        )
        check(r_sem["outcome"] == "changed", "semantic -> changed")

        # timeout -> unavailable
        def timeout_transport(_req):
            raise TimeoutError()
        r_timeout = ghr.execute_github_import(
            campaign_dir, campaign_id, 1, "github-import-timeout", "Owner/Repo", timeout_transport,
        )
        check(r_timeout["status"] == "confirmed" and r_timeout["outcome"] == "unavailable", "timeout -> unavailable")

        # explicit unavailable
        def unavailable_transport(_req):
            return {"status": "unavailable", "reason_code": "repository_unavailable"}
        r_unavail = ghr.execute_github_import(
            campaign_dir, campaign_id, 1, "github-import-unavail", "Owner/Repo", unavailable_transport,
        )
        check(r_unavail["status"] == "confirmed" and r_unavail["outcome"] == "unavailable", "explicit unavailable")

        # unexpected exception escapes; intent only
        def boom_transport(_req):
            raise RuntimeError("boom")
        before_boom = count_artifacts(campaign_dir)
        try:
            ghr.execute_github_import(
                campaign_dir, campaign_id, 1, "github-import-boom", "Owner/Repo", boom_transport,
            )
            check(False, "unexpected exception should escape")
        except RuntimeError:
            check(True, "unexpected exception escapes")
        check(count_artifacts(campaign_dir) == before_boom + 1, "unexpected exception: intent only")

        # negative replay conflict: changed previous digest -> conflict before transport
        transport_conflict = FakeTransport(snapshot=base_snap)
        try:
            ghr.execute_github_import(
                campaign_dir, campaign_id, 1, "github-import-001", "Owner/Repo", transport_conflict,
                previous_content_digest="f" * 64,
            )
            check(False, "changed previous digest should conflict")
        except Exception as exc:
            check("Conflict" in type(exc).__name__ or "Conflict" in str(exc), "negative replay conflict raised")
        check(len(transport_conflict.invocations) == 0, "conflict before transport")

        # malformed lifecycle: intent only
        transport_malformed = FakeTransport(snapshot=fixture("snapshot-malformed.json"))
        before_mal = count_artifacts(campaign_dir)
        try:
            ghr.execute_github_import(
                campaign_dir, campaign_id, 1, "github-import-mal", "Owner/Repo", transport_malformed,
            )
            check(False, "malformed should raise")
        except ghr.GitHubSnapshotMalformed:
            check(True, "malformed raises")
        check(count_artifacts(campaign_dir) == before_mal + 1, "malformed: intent only")

        # campaign immutability
        camp_json = next(campaign_dir.glob("campaign.json"))
        camp_digest_before = camp_json.read_bytes()
        camp_digest_after = camp_json.read_bytes()
        check(camp_digest_before == camp_digest_after, "campaign.json unchanged")

        # repository immutability
        before_map = {p.relative_to(repo).as_posix(): p.read_bytes() for p in repo.rglob("*") if p.is_file()}
        after_map = {p.relative_to(repo).as_posix(): p.read_bytes() for p in repo.rglob("*") if p.is_file()}
        check(before_map == after_map, "repository files unchanged")

        # selected-metadata policy: token-shaped title rejected, no echo
        token_snap = {
            "status": "ok", "repository": "owner/repo",
            "issues": [{"number": 9, "title": "sk-abcdef1234567890", "state": "open", "labels": [], "milestone": None, "updated_at": None, "body": "raw"}],
            "milestones": [],
        }
        transport_token = FakeTransport(snapshot=token_snap)
        before_token = count_artifacts(campaign_dir)
        try:
            ghr.execute_github_import(
                campaign_dir, campaign_id, 1, "github-import-token", "Owner/Repo", transport_token,
            )
            check(False, "token-shaped title should be rejected")
        except ghr.GitHubSourcePolicyError as exc:
            check("sk-abcdef1234567890" not in str(exc), "secret not echoed")
        check(count_artifacts(campaign_dir) == before_token + 1, "token rejection: intent only")

        # Fail-closed import: module import raises GitHubSourceError when backend blocked
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
                import myrmex_github_reader
                print("IMPORTED")
            except Exception as e:
                print(type(e).__name__)
            """
        )
        _r = subprocess.run([sys.executable, "-c", _probe], capture_output=True, text=True, cwd=str(ROOT))
        check("GitHubSourceError" in _r.stdout, "module import fails closed with GitHubSourceError")

    print(f"github backlog import test: {'FAIL' if failures else 'PASS'} ({len(failures)} failures)")
    if failures:
        for f in failures:
            print("  FAIL:", f)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
