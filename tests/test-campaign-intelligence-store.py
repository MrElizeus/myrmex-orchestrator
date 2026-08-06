#!/usr/bin/env python3
"""Tests for the Myrmex campaign-intelligence sidecar store (WU-P1-002).

Covers: creation/reuse semantics, byte-for-byte immutability, cross-campaign
isolation, all four artifact kinds, authoritative show, deterministic
descriptor lists, doctor health, projection delete/rebuild recovery, campaign
byte/revision stability, conflict rejection, invalid input rejection,
secret/raw-source rejection with no persistence, concurrency (identical and
conflicting writers), crash-after-artifact recovery, corruption detection,
and campaign-v1 compatibility.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BIN_CAMPAIGN = ROOT / "bin/myrmex-campaign"
SCRIPTS_DIR = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
import myrmex_campaign_intelligence as intel  # noqa: E402

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_CONFLICT = 2
EXIT_BACKEND = 3
EXIT_NOT_FOUND = 4

TEST_COUNT = 0


def ok(label: str) -> None:
    global TEST_COUNT
    TEST_COUNT += 1
    print(f"  ok {label}")


def run_campaign(args: list[str], state_home: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, XDG_STATE_HOME=state_home, PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run(
        [sys.executable, str(BIN_CAMPAIGN), *args],
        capture_output=True,
        text=True,
        env=env,
        input=stdin,
    )


def campaign_dir(state_home: str, cid: str = "camp-intel") -> Path:
    return Path(state_home) / "myrmex" / "campaigns" / cid


def init_campaign(state_home: str, cid: str = "camp-intel", repo_root: Path | None = None) -> None:
    proc = run_campaign(
        ["init", "--id", cid, "--title", "Intel Test", "--objective", "Test objective",
         "--repo-root", str(repo_root)],
        state_home,
    )
    assert proc.returncode == EXIT_OK, f"init failed: {proc.stderr}"


def write_payload(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def put(state_home: str, cid: str, kind: str, artifact_id: str, payload_path: Path, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return run_campaign(
        ["intelligence-put", cid, "--kind", kind, "--artifact-id", artifact_id,
         "--input-json", "-" if stdin is not None else str(payload_path)],
        state_home,
        stdin=stdin,
    )


def artifacts_dir(state_home: str, cid: str = "camp-intel") -> Path:
    return campaign_dir(state_home, cid) / "intelligence" / "artifacts"


# --------------------------------------------------------------------------
# Positive semantics
# --------------------------------------------------------------------------

def test_first_artifact_created_and_reused_byte_identical() -> None:
    with tempfile.TemporaryDirectory(prefix="intel-pos-") as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        init_campaign(td, "camp-intel", repo)
        payload_path = write_payload(Path(td) / "inputs" / "plan.json", {"title": "Plan", "steps": [1, 2]})

        first = put(td, "camp-intel", "plan", "plans/2026-q1", payload_path)
        assert first.returncode == EXIT_OK, first.stderr
        r1 = json.loads(first.stdout)
        assert r1["status"] == "created", r1
        assert r1["artifact_id"] == "plans/2026-q1"
        assert r1["kind"] == "plan"
        assert r1["ok"] is True
        assert len(r1["artifact_digest"]) == 64
        assert len(r1["payload_digest"]) == 64
        assert len(r1["projection_source_digest"]) == 64
        assert r1["observed_campaign_revision"] == 1
        ok("first put returns created with digests")

        storage_key = intel.artifact_storage_key("plans/2026-q1")
        artifact_file = artifacts_dir(td) / (storage_key + ".json")
        assert artifact_file.is_file(), "artifact filename must be sha256(artifact_id).json"
        assert artifact_file.name == hashlib.sha256(b"plans/2026-q1").hexdigest() + ".json"
        bytes_before = artifact_file.read_bytes()
        ok("artifact filename equals sha256 of artifact id")

        second = put(td, "camp-intel", "plan", "plans/2026-q1", payload_path)
        assert second.returncode == EXIT_OK, second.stderr
        r2 = json.loads(second.stdout)
        assert r2["status"] == "reused", r2
        assert r2["artifact_digest"] == r1["artifact_digest"]
        assert artifact_file.read_bytes() == bytes_before, "reuse must leave file byte-for-byte unchanged"
        ok("repeated identical put returns reused and file is byte-for-byte unchanged")


def test_same_artifact_id_two_campaigns_isolated() -> None:
    with tempfile.TemporaryDirectory(prefix="intel-isol-") as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        init_campaign(td, "camp-alphatest", repo)
        init_campaign(td, "camp-betatest", repo)
        pa = write_payload(Path(td) / "inputs" / "a.json", {"campaign": "alpha"})
        pb = write_payload(Path(td) / "inputs" / "b.json", {"campaign": "beta"})

        ra = put(td, "camp-alphatest", "plan", "shared-id", pa)
        rb = put(td, "camp-betatest", "plan", "shared-id", pb)
        assert json.loads(ra.stdout)["status"] == "created"
        assert json.loads(rb.stdout)["status"] == "created"
        ok("same artifact id in two campaigns is isolated")

        show_a = run_campaign(["intelligence-show", "camp-alphatest", "--artifact-id", "shared-id"], td)
        show_b = run_campaign(["intelligence-show", "camp-betatest", "--artifact-id", "shared-id"], td)
        assert json.loads(show_a.stdout)["artifact"]["payload"] == {"campaign": "alpha"}
        assert json.loads(show_b.stdout)["artifact"]["payload"] == {"campaign": "beta"}
        assert show_a.returncode == EXIT_OK and show_b.returncode == EXIT_OK
        ok("show resolves the correct payload per campaign")


def test_all_four_kinds_storable() -> None:
    with tempfile.TemporaryDirectory(prefix="intel-kinds-") as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        init_campaign(td, "camp-intel", repo)
        for idx, kind in enumerate(("backlog", "plan", "review", "decision")):
            p = write_payload(Path(td) / "inputs" / f"{kind}.json", {"kind": kind, "n": idx})
            proc = put(td, "camp-intel", kind, f"id-{kind}", p)
            assert proc.returncode == EXIT_OK, proc.stderr
            assert json.loads(proc.stdout)["status"] == "created"
        listing = run_campaign(["intelligence-list", "camp-intel"], td)
        data = json.loads(listing.stdout)
        assert data["status"] == "healthy"
        assert data["artifact_count"] == 4
        for kind in ("backlog", "plan", "review", "decision"):
            assert len(data["artifacts"][kind]) == 1, f"missing kind {kind}"
        ok("all four artifact kinds stored and listed")


def test_show_validates_authoritative_artifact() -> None:
    with tempfile.TemporaryDirectory(prefix="intel-show-") as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        init_campaign(td, "camp-intel", repo)
        payload = {"title": "Authoritative", "tags": ["a", "b"]}
        p = write_payload(Path(td) / "inputs" / "p.json", payload)
        put(td, "camp-intel", "review", "rev-1", p)

        proc = run_campaign(["intelligence-show", "camp-intel", "--artifact-id", "rev-1"], td)
        assert proc.returncode == EXIT_OK, proc.stderr
        data = json.loads(proc.stdout)
        env = data["artifact"]
        assert env["schema"] == intel.ARTIFACT_SCHEMA
        assert env["campaign_id"] == "camp-intel"
        assert env["kind"] == "review"
        assert env["payload"] == payload
        assert env["payload_digest"] == intel.compute_payload_digest(payload)
        assert env["artifact_digest"] == intel.compute_artifact_digest("camp-intel", "review", "rev-1", payload)
        assert isinstance(env["created_at"], str) and env["created_at"]
        assert isinstance(env["observed_campaign_revision"], int)
        ok("show returns a validated authoritative envelope with recomputable digests")

        # Show must never trust the projection: corrupt it, show still works.
        proj = campaign_dir(td) / "intelligence" / "projection.json"
        proj.write_text('{"corrupt": true}', encoding="utf-8")
        proc2 = run_campaign(["intelligence-show", "camp-intel", "--artifact-id", "rev-1"], td)
        assert proc2.returncode == EXIT_OK, proc2.stderr
        assert json.loads(proc2.stdout)["artifact"]["payload"] == payload
        ok("show does not trust an invalid projection over the artifact")


def test_list_deterministic_descriptors_no_payloads() -> None:
    with tempfile.TemporaryDirectory(prefix="intel-list-") as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        init_campaign(td, "camp-intel", repo)
        for idx in (1, 2):
            p = write_payload(Path(td) / "inputs" / f"p{idx}.json", {"n": idx})
            put(td, "camp-intel", "plan", f"plan-{idx}", p)

        first = run_campaign(["intelligence-list", "camp-intel"], td)
        second = run_campaign(["intelligence-list", "camp-intel"], td)
        d1 = json.loads(first.stdout)
        d2 = json.loads(second.stdout)
        assert d1["status"] == "healthy" and d1["artifact_count"] == 2
        assert d1["artifacts"] == d2["artifacts"], "descriptor list must be deterministic"
        text = first.stdout
        assert '"payload"' not in text, "projection must not contain payloads"
        for descriptor in d1["artifacts"]["plan"]:
            assert set(descriptor.keys()) == {
                "artifact_id", "artifact_digest", "payload_digest", "created_at", "storage_key"
            }
        ok("list returns deterministic descriptors without payloads")

        kind_only = run_campaign(["intelligence-list", "camp-intel", "--kind", "plan"], td)
        ko = json.loads(kind_only.stdout)
        assert list(ko["artifacts"].keys()) == ["plan"]
        ok("list supports kind filtering")


def test_doctor_healthy_and_private_modes() -> None:
    with tempfile.TemporaryDirectory(prefix="intel-doctor-") as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        init_campaign(td, "camp-intel", repo)
        p = write_payload(Path(td) / "inputs" / "p.json", {"x": 1})
        put(td, "camp-intel", "plan", "plan-1", p)

        proc = run_campaign(["intelligence-doctor", "camp-intel"], td)
        assert proc.returncode == EXIT_OK, proc.stderr
        data = json.loads(proc.stdout)
        assert data["status"] == "healthy" and data["ok"] is True
        ok("doctor reports healthy")

        iroot = campaign_dir(td) / "intelligence"
        arts = iroot / "artifacts"
        assert (iroot.stat().st_mode & 0o777) == 0o700
        assert (arts.stat().st_mode & 0o777) == 0o700
        assert (iroot / "lock").stat().st_mode & 0o777 == 0o600
        assert (iroot / "projection.json").stat().st_mode & 0o777 == 0o600
        artifact_file = next(arts.glob("*.json"))
        assert artifact_file.stat().st_mode & 0o777 == 0o600
        ok("directories 0700 and files 0600")


def test_projection_delete_rebuild_restores() -> None:
    with tempfile.TemporaryDirectory(prefix="intel-rebuild-") as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        init_campaign(td, "camp-intel", repo)
        for idx in (1, 2):
            p = write_payload(Path(td) / "inputs" / f"p{idx}.json", {"n": idx})
            put(td, "camp-intel", "plan", f"plan-{idx}", p)

        before = json.loads(run_campaign(["intelligence-list", "camp-intel"], td).stdout)
        assert before["status"] == "healthy"
        proj = campaign_dir(td) / "intelligence" / "projection.json"
        proj.unlink()

        missing = json.loads(run_campaign(["intelligence-list", "camp-intel"], td).stdout)
        assert missing["status"] == "projection_missing"
        doc = json.loads(run_campaign(["intelligence-doctor", "camp-intel"], td).stdout)
        assert doc["status"] == "projection_missing"
        assert doc["ok"] is False
        assert run_campaign(["intelligence-doctor", "camp-intel"], td).returncode == 1
        ok("deleting the projection does not delete artifacts; doctor reports projection_missing")

        rebuilt = run_campaign(["intelligence-rebuild", "camp-intel"], td)
        assert rebuilt.returncode == EXIT_OK, rebuilt.stderr
        after = json.loads(run_campaign(["intelligence-list", "camp-intel"], td).stdout)
        assert after["status"] == "healthy"
        assert after["source_digest"] == before["source_digest"]
        assert after["artifacts"] == before["artifacts"]
        ok("rebuild restores identical descriptors and source_digest")


def test_campaign_file_and_revision_unchanged() -> None:
    with tempfile.TemporaryDirectory(prefix="intel-campfile-") as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        init_campaign(td, "camp-intel", repo)
        cfile = campaign_dir(td) / "campaign.json"
        before_bytes = cfile.read_bytes()
        before_data = json.loads(before_bytes)
        assert before_data["revision"] == 1

        p = write_payload(Path(td) / "inputs" / "p.json", {"x": 1})
        put(td, "camp-intel", "plan", "plan-1", p)
        run_campaign(["intelligence-show", "camp-intel", "--artifact-id", "plan-1"], td)
        run_campaign(["intelligence-list", "camp-intel"], td)
        run_campaign(["intelligence-rebuild", "camp-intel"], td)
        run_campaign(["intelligence-doctor", "camp-intel"], td)

        after_bytes = cfile.read_bytes()
        after_data = json.loads(after_bytes)
        assert after_bytes == before_bytes, "campaign.json must be byte-for-byte unchanged"
        assert after_data["revision"] == 1, "campaign revision must not change"
        ok("campaign.json bytes and revision remain unchanged after sidecar operations")


def test_stdin_input_allowed() -> None:
    with tempfile.TemporaryDirectory(prefix="intel-stdin-") as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        init_campaign(td, "camp-intel", repo)
        proc = put(td, "camp-intel", "decision", "dec-1", Path("unused"), stdin='{"accepted": true}')
        assert proc.returncode == EXIT_OK, proc.stderr
        assert json.loads(proc.stdout)["status"] == "created"
        ok("stdin JSON object input is accepted")


# --------------------------------------------------------------------------
# Conflict and invalid-input semantics
# --------------------------------------------------------------------------

def test_conflicts_rejected_and_original_preserved() -> None:
    with tempfile.TemporaryDirectory(prefix="intel-conflict-") as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        init_campaign(td, "camp-intel", repo)
        pa = write_payload(Path(td) / "inputs" / "a.json", {"title": "Original"})
        pb = write_payload(Path(td) / "inputs" / "b.json", {"title": "Different"})
        put(td, "camp-intel", "plan", "same-id", pa)
        artifact_file = next(artifacts_dir(td).glob("*.json"))
        bytes_before = artifact_file.read_bytes()

        proc = put(td, "camp-intel", "plan", "same-id", pb)
        assert proc.returncode == EXIT_CONFLICT, proc.stdout
        conflict = json.loads(proc.stdout)
        assert conflict["status"] == "conflict"
        assert artifact_file.read_bytes() == bytes_before, "conflict must never alter the original artifact"
        ok("same ID with different payload is rejected; original unchanged")

        proc2 = put(td, "camp-intel", "review", "same-id", pa)
        assert proc2.returncode == EXIT_CONFLICT
        assert json.loads(proc2.stdout)["status"] == "conflict"
        assert artifact_file.read_bytes() == bytes_before
        ok("same ID with different kind is rejected; original unchanged")

        listing = json.loads(run_campaign(["intelligence-list", "camp-intel"], td).stdout)
        assert listing["artifact_count"] == 1
        assert listing["artifacts"]["plan"][0]["artifact_id"] == "same-id"
        ok("after conflicts the projection reflects only the original artifact")


def test_invalid_campaign_and_artifact_ids() -> None:
    with tempfile.TemporaryDirectory(prefix="intel-ids-") as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        init_campaign(td, "camp-intel", repo)
        p = write_payload(Path(td) / "inputs" / "p.json", {"x": 1})

        bad_cid = put(td, "camp-BAD!", "plan", "id-1", p)
        assert bad_cid.returncode == EXIT_INVALID
        ok("invalid campaign id rejected")

        missing = run_campaign(
            ["intelligence-put", "camp-missing", "--kind", "plan", "--artifact-id", "id-1",
             "--input-json", str(p)],
            td,
        )
        assert missing.returncode == EXIT_INVALID
        ok("missing campaign rejected")

        for bad_id in ("bad id!", "a/b\\c", "../traversal", ".hidden", ""):
            if bad_id == "":
                proc = run_campaign(
                    ["intelligence-put", "camp-intel", "--kind", "plan", "--artifact-id", "", "--input-json", str(p)],
                    td,
                )
            else:
                proc = put(td, "camp-intel", "plan", bad_id, p)
            assert proc.returncode == EXIT_INVALID, f"artifact id {bad_id!r} must be rejected: {proc.stdout}"
        ok("invalid and traversal-like artifact ids rejected")

        valid_slash = put(td, "camp-intel", "plan", "plans/2026/q1/rev-2", p)
        assert valid_slash.returncode == EXIT_OK
        assert json.loads(valid_slash.stdout)["status"] == "created"
        ok("slash-containing logical artifact id accepted and stored under a hashed filename")


def test_malformed_and_non_object_input_rejected() -> None:
    with tempfile.TemporaryDirectory(prefix="intel-json-") as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        init_campaign(td, "camp-intel", repo)
        bad = Path(td) / "inputs"
        bad.mkdir(parents=True, exist_ok=True)

        (bad / "malformed.json").write_text('{"broken": ', encoding="utf-8")
        proc = run_campaign(
            ["intelligence-put", "camp-intel", "--kind", "plan", "--artifact-id", "id-1", "--input-json", str(bad / "malformed.json")],
            td,
        )
        assert proc.returncode == EXIT_INVALID
        ok("malformed JSON rejected")

        for name, content in (("array.json", "[1,2,3]"), ("scalar.json", "42"), ("null.json", "null"), ("nan.json", "NaN")):
            (bad / name).write_text(content, encoding="utf-8")
            proc = run_campaign(
                ["intelligence-put", "camp-intel", "--kind", "plan", "--artifact-id", "id-1", "--input-json", str(bad / name)],
                td,
            )
            assert proc.returncode == EXIT_INVALID, f"{name} must be rejected"
        ok("non-object and non-standard JSON rejected")

        assert not (campaign_dir(td) / "intelligence").exists()
        ok("rejected input created no artifact or projection files")


def test_input_file_policy_rejected() -> None:
    with tempfile.TemporaryDirectory(prefix="intel-input-") as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        init_campaign(td, "camp-intel", repo)
        inputs = Path(td) / "inputs"
        inputs.mkdir()

        inside = write_payload(repo / "payload.json", {"x": 1})
        proc = run_campaign(
            ["intelligence-put", "camp-intel", "--kind", "plan", "--artifact-id", "id-1", "--input-json", str(inside)],
            td,
        )
        assert proc.returncode == EXIT_INVALID
        assert "repository root" in json.loads(proc.stdout)["error"]
        ok("input file inside the repository root rejected")

        outside = write_payload(inputs / "ok.json", {"x": 1})
        link = inputs / "link.json"
        os.symlink(str(outside), str(link))
        proc2 = run_campaign(
            ["intelligence-put", "camp-intel", "--kind", "plan", "--artifact-id", "id-1", "--input-json", str(link)],
            td,
        )
        assert proc2.returncode == EXIT_INVALID
        assert "symbolic link" in json.loads(proc2.stdout)["error"].lower()
        ok("symlink input rejected")

        proc3 = run_campaign(
            ["intelligence-put", "camp-intel", "--kind", "plan", "--artifact-id", "id-1", "--input-json", str(inputs)],
            td,
        )
        assert proc3.returncode == EXIT_INVALID
        ok("non-regular input rejected")

        assert not (campaign_dir(td) / "intelligence").exists()
        ok("input-policy rejections created no artifact or projection files")


def test_secret_and_raw_content_rejected() -> None:
    with tempfile.TemporaryDirectory(prefix="intel-secret-") as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        init_campaign(td, "camp-intel", repo)
        inputs = Path(td) / "inputs"
        inputs.mkdir()

        pem_fixture = "-----BEGIN " + "PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFA\n-----END " + "PRIVATE KEY-----"
        token_fixture = "sk-" + "abcdefghijklmno"
        ghp_fixture = "ghp_" + "a" * 36
        bearer_fixture = "Bearer abcdefghijklmnopqrstuvwxyz"

        cases = {
            "raw_top.json": {"source_code": "def f(): pass"},
            "raw_nested.json": {"meta": {"file_content": "print(1)"}},
            "raw_patch.json": {"patch": "diff --git a/x b/y"},
            "secret_key.json": {"password": "hunter2"},
            "secret_nested.json": {"meta": {"nested_api_key": "zzz"}},
            "secret_suffix.json": {"user_access_token": "tok123"},
            "pem.json": {"cert": pem_fixture},
            "token.json": {"value": token_fixture},
            "ghp.json": {"value": ghp_fixture},
            "bearer.json": {"auth": bearer_fixture},
            "assignment.json": {"text": "api_key = abcdefghij"},
            "nul.json": {"note": "a\u0000b"},
        }
        for name, data in cases.items():
            path = inputs / name
            path.write_text(json.dumps(data), encoding="utf-8")
            proc = run_campaign(
                ["intelligence-put", "camp-intel", "--kind", "plan", "--artifact-id", "id-1", "--input-json", str(path)],
                td,
            )
            assert proc.returncode == EXIT_INVALID, f"{name} must be rejected: {proc.stdout} {proc.stderr}"
            out = proc.stdout + proc.stderr
            assert "hunter2" not in out and "abcdefghijklmno" not in out and "MIIEvQIB" not in out, (
                f"rejection must not echo secret values for {name}"
            )
        ok("raw-content, secret, PEM, token, bearer, assignment, and NUL inputs rejected without echoing secrets")

        assert not (campaign_dir(td) / "intelligence").exists()
        ok("secret/raw rejections created no artifact or projection files")

        metadata = write_payload(inputs / "metadata.json", {
            "source_identity": "scout", "source_path": "docs/x.md",
            "repository_path": "repo", "diff_digest": "abc123",
            "file_digest": "def456", "credential_required": True,
        })
        proc_meta = run_campaign(
            ["intelligence-put", "camp-intel", "--kind", "plan", "--artifact-id", "meta-1", "--input-json", str(metadata)],
            td,
        )
        assert proc_meta.returncode == EXIT_OK, proc_meta.stderr
        ok("safe metadata keys are allowed")


# --------------------------------------------------------------------------
# Concurrency
# --------------------------------------------------------------------------

def test_concurrent_identical_writers_exactly_one_created() -> None:
    with tempfile.TemporaryDirectory(prefix="intel-conc-") as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        init_campaign(td, "camp-intel", repo)
        payload = write_payload(Path(td) / "inputs" / "same.json", {"title": "Concurrent", "n": 1})

        env = dict(os.environ, XDG_STATE_HOME=td, PYTHONDONTWRITEBYTECODE="1")
        procs = []
        count = 8
        for _ in range(count):
            procs.append(subprocess.Popen(
                [sys.executable, str(BIN_CAMPAIGN), "intelligence-put", "camp-intel",
                 "--kind", "plan", "--artifact-id", "conc-id", "--input-json", str(payload)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
            ))
        results = []
        for proc in procs:
            out, err = proc.communicate()
            results.append((proc.returncode, out, err))

        created = 0
        reused = 0
        digests = set()
        for rc, out, err in results:
            assert rc == EXIT_OK, err
            res = json.loads(out)
            assert res["ok"] is True
            if res["status"] == "created":
                created += 1
            elif res["status"] == "reused":
                reused += 1
            else:
                raise AssertionError(f"unexpected status {res['status']}")
            digests.add((res["artifact_digest"], res["payload_digest"], res["projection_source_digest"]))
        assert created == 1, f"expected exactly one created, got {created}"
        assert reused == count - 1, f"expected {count - 1} reused, got {reused}"
        assert len(digests) == 1, "all successful receipts must carry identical digests"
        assert len(list(artifacts_dir(td).glob("*.json"))) == 1, "exactly one artifact file expected"
        ok(f"concurrent identical writes: {count} processes -> created={created} reused={reused}")

        listing = json.loads(run_campaign(["intelligence-list", "camp-intel"], td).stdout)
        assert listing["status"] == "healthy" and listing["artifact_count"] == 1
        ok("concurrent identical writes leave one valid projection")


def test_concurrent_conflicting_writers_one_winner_one_conflict() -> None:
    with tempfile.TemporaryDirectory(prefix="intel-conc2-") as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        init_campaign(td, "camp-intel", repo)
        payload_a = write_payload(Path(td) / "inputs" / "a.json", {"side": "A", "value": 111})
        payload_b = write_payload(Path(td) / "inputs" / "b.json", {"side": "B", "value": 222})

        env = dict(os.environ, XDG_STATE_HOME=td, PYTHONDONTWRITEBYTECODE="1")
        procs = []
        for payload_path in (payload_a, payload_b):
            procs.append(subprocess.Popen(
                [sys.executable, str(BIN_CAMPAIGN), "intelligence-put", "camp-intel",
                 "--kind", "plan", "--artifact-id", "race-id", "--input-json", str(payload_path)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
            ))
        results = []
        for proc in procs:
            out, err = proc.communicate()
            results.append((proc.returncode, out, err))

        created = [r for r in results if r[0] == EXIT_OK]
        conflicted = [r for r in results if r[0] == EXIT_CONFLICT]
        assert len(created) == 1, f"expected exactly one winner, got {len(created)}"
        assert len(conflicted) == 1, f"expected exactly one conflict, got {len(conflicted)}"
        winner = json.loads(created[0][1])
        assert winner["status"] == "created"
        conflict = json.loads(conflicted[0][1])
        assert conflict["status"] == "conflict"
        ok(f"concurrent conflicting writes: winner created, one conflict (rc={conflicted[0][0]})")

        assert len(list(artifacts_dir(td).glob("*.json"))) == 1
        show = json.loads(run_campaign(["intelligence-show", "camp-intel", "--artifact-id", "race-id"], td).stdout)
        stored_payload = show["artifact"]["payload"]
        candidates = ({"side": "A", "value": 111}, {"side": "B", "value": 222})
        assert stored_payload in candidates, f"stored payload {stored_payload} must be one complete winner payload"
        assert show["artifact"]["payload_digest"] == winner["payload_digest"], (
            "stored digest must match the winning receipt"
        )
        listing = json.loads(run_campaign(["intelligence-list", "camp-intel"], td).stdout)
        assert listing["artifact_count"] == 1 and listing["status"] == "healthy"
        assert listing["artifacts"]["plan"][0]["payload_digest"] == winner["payload_digest"]
        ok("final artifact and projection match the winner; no mixed payload")


# --------------------------------------------------------------------------
# Crash / recovery
# --------------------------------------------------------------------------

def test_crash_after_artifact_before_projection_recovery() -> None:
    with tempfile.TemporaryDirectory(prefix="intel-crash-") as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        init_campaign(td, "camp-intel", repo)
        cdir = campaign_dir(td)
        payload = {"title": "Crash Survivor", "n": 7}

        child = (
            "import os, sys\n"
            "sys.path.insert(0, {scripts!r})\n"
            "import myrmex_campaign_intelligence as intel\n"
            "def _crash(*args, **kwargs):\n"
            "    os._exit(0)\n"
            "intel._rebuild_projection_locked = _crash\n"
            "intel.put_artifact(\n"
            "    campaign_dir={cdir!r}, campaign_id='camp-intel', campaign_revision=1,\n"
            "    kind='plan', artifact_id='crash-id', payload={payload!r},\n"
            ")\n"
        ).format(scripts=str(SCRIPTS_DIR), cdir=str(cdir), payload=payload)
        env = dict(os.environ, XDG_STATE_HOME=td, PYTHONDONTWRITEBYTECODE="1")
        proc = subprocess.run([sys.executable, "-c", child], capture_output=True, text=True, env=env)
        assert proc.returncode == 0, f"crash child failed: {proc.stderr}"

        files = list(artifacts_dir(td).glob("*.json"))
        assert len(files) == 1, f"artifact must be durable, found {files}"
        envelope = json.loads(files[0].read_text(encoding="utf-8"))
        assert envelope["artifact_id"] == "crash-id"
        assert envelope["payload_digest"] == intel.compute_payload_digest(payload)
        assert not (cdir / "intelligence" / "projection.json").exists(), "projection must be missing after crash"
        ok("crash injected after artifact persistence, before projection rebuild")

        doc = json.loads(run_campaign(["intelligence-doctor", "camp-intel"], td).stdout)
        assert doc["status"] in ("projection_missing", "projection_stale"), doc
        assert doc["ok"] is False
        ok(f"doctor reports recoverable condition: {doc['status']}")

        rebuilt = run_campaign(["intelligence-rebuild", "camp-intel"], td)
        assert rebuilt.returncode == EXIT_OK, rebuilt.stderr
        doc2 = json.loads(run_campaign(["intelligence-doctor", "camp-intel"], td).stdout)
        assert doc2["status"] == "healthy"
        ok("explicit rebuild restores a healthy projection")

        retry = put(td, "camp-intel", "plan", "crash-id", write_payload(Path(td) / "inputs" / "crash.json", payload))
        assert retry.returncode == EXIT_OK, retry.stderr
        assert json.loads(retry.stdout)["status"] == "reused"
        assert len(list(artifacts_dir(td).glob("*.json"))) == 1, "retry must not duplicate the artifact"
        listing = json.loads(run_campaign(["intelligence-list", "camp-intel"], td).stdout)
        assert listing["status"] == "healthy" and listing["artifact_count"] == 1
        ok("retry returns reused with no duplicate artifact and a healthy projection")


# --------------------------------------------------------------------------
# Corruption
# --------------------------------------------------------------------------

def test_corrupt_artifact_reported_and_rebuild_fails_explicitly() -> None:
    with tempfile.TemporaryDirectory(prefix="intel-corrupt-") as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        init_campaign(td, "camp-intel", repo)
        p = write_payload(Path(td) / "inputs" / "p.json", {"title": "Clean"})
        put(td, "camp-intel", "plan", "corrupt-me", p)
        artifact_file = next(artifacts_dir(td).glob("*.json"))
        doc_clean = json.loads(run_campaign(["intelligence-doctor", "camp-intel"], td).stdout)
        assert doc_clean["status"] == "healthy"

        # Corrupt the envelope payload; digests no longer recompute.
        envelope = json.loads(artifact_file.read_text(encoding="utf-8"))
        envelope["payload"] = {"title": "Tampered"}
        artifact_file.write_text(json.dumps(envelope, indent=2), encoding="utf-8")

        doc = json.loads(run_campaign(["intelligence-doctor", "camp-intel"], td).stdout)
        assert doc["status"] == "artifact_corrupt", doc
        assert run_campaign(["intelligence-doctor", "camp-intel"], td).returncode == 1
        ok("doctor reports artifact_corrupt after envelope tampering")

        rebuild = run_campaign(["intelligence-rebuild", "camp-intel"], td)
        assert rebuild.returncode == EXIT_INVALID
        assert "corrupt" in json.loads(rebuild.stdout)["error"].lower() or "digest" in json.loads(rebuild.stdout)["error"].lower()
        ok("rebuild fails explicitly on the corrupt artifact instead of skipping it")

        # Corrupt the stored digest field itself.
        artifact_file.write_text(json.dumps(envelope, indent=2).replace(envelope["artifact_digest"], "0" * 64), encoding="utf-8")
        doc2 = json.loads(run_campaign(["intelligence-doctor", "camp-intel"], td).stdout)
        assert doc2["status"] == "artifact_corrupt", doc2
        ok("doctor detects a corrupted artifact_digest field")


def test_projection_digest_tamper_reported_stale() -> None:
    with tempfile.TemporaryDirectory(prefix="intel-stale-") as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        init_campaign(td, "camp-intel", repo)
        p = write_payload(Path(td) / "inputs" / "p.json", {"title": "X"})
        put(td, "camp-intel", "plan", "plan-1", p)
        proj = campaign_dir(td) / "intelligence" / "projection.json"
        data = json.loads(proj.read_text(encoding="utf-8"))
        data["source_digest"] = "0" * 64
        proj.write_text(json.dumps(data, indent=2), encoding="utf-8")

        listing = json.loads(run_campaign(["intelligence-list", "camp-intel"], td).stdout)
        assert listing["status"] == "projection_stale", listing
        doc = json.loads(run_campaign(["intelligence-doctor", "camp-intel"], td).stdout)
        assert doc["status"] == "projection_stale", doc
        ok("tampered projection source_digest is reported stale without being silently rewritten")

        show = run_campaign(["intelligence-show", "camp-intel", "--artifact-id", "plan-1"], td)
        assert show.returncode == EXIT_OK
        assert json.loads(show.stdout)["artifact"]["payload"] == {"title": "X"}
        ok("show still serves the authoritative artifact while the projection is stale")


# --------------------------------------------------------------------------
# Frontier corrective-plan regression tests (WU-P1-002-C)
# --------------------------------------------------------------------------

def test_unknown_envelope_field_rejected_everywhere() -> None:
    with tempfile.TemporaryDirectory(prefix="intel-corr-env-") as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        init_campaign(td, "camp-intel", repo)
        p = write_payload(Path(td) / "inputs" / "p.json", {"title": "Clean"})
        put(td, "camp-intel", "plan", "env-id", p)
        artifact_file = next(artifacts_dir(td).glob("*.json"))
        envelope = json.loads(artifact_file.read_text(encoding="utf-8"))
        envelope["unexpected_extra"] = {"nested": "forbidden-value"}
        artifact_file.write_text(json.dumps(envelope, indent=2), encoding="utf-8")

        show = run_campaign(["intelligence-show", "camp-intel", "--artifact-id", "env-id"], td)
        assert show.returncode == EXIT_INVALID, show.stdout
        show_out = show.stdout + show.stderr
        assert "forbidden-value" not in show_out, "unknown field values must never be echoed"
        assert json.loads(show.stdout)["status"] == "artifact_invalid", show.stdout
        ok("show rejects an artifact with one unknown envelope field without echoing its value")

        doc = json.loads(run_campaign(["intelligence-doctor", "camp-intel"], td).stdout)
        assert doc["status"] == "artifact_corrupt", doc
        assert doc["ok"] is False
        assert "forbidden-value" not in json.dumps(doc)
        assert run_campaign(["intelligence-doctor", "camp-intel"], td).returncode == 1
        ok("doctor reports artifact_corrupt for an unknown envelope field")

        rebuild = run_campaign(["intelligence-rebuild", "camp-intel"], td)
        assert rebuild.returncode == EXIT_INVALID, rebuild.stdout
        assert json.loads(rebuild.stdout)["status"] == "rebuild_failed", rebuild.stdout
        assert "forbidden-value" not in (rebuild.stdout + rebuild.stderr)
        ok("rebuild fails explicitly on an unknown envelope field")


def test_prohibited_payload_content_detected_on_read() -> None:
    with tempfile.TemporaryDirectory(prefix="intel-corr-sec-") as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        init_campaign(td, "camp-intel", repo)
        p = write_payload(Path(td) / "inputs" / "p.json", {"title": "Clean"})
        put(td, "camp-intel", "plan", "sec-id", p)
        artifact_file = next(artifacts_dir(td).glob("*.json"))

        # Tamper the payload with a prohibited secret key/value, then recompute
        # both digests so the artifact is digest-consistent but policy-invalid.
        tampered = {"title": "Clean", "password": "hunter2"}
        envelope = json.loads(artifact_file.read_text(encoding="utf-8"))
        envelope["payload"] = tampered
        envelope["payload_digest"] = intel.compute_payload_digest(tampered)
        envelope["artifact_digest"] = intel.compute_artifact_digest(
            envelope["campaign_id"], envelope["kind"], envelope["artifact_id"], tampered
        )
        artifact_file.write_text(json.dumps(envelope, indent=2), encoding="utf-8")

        show = run_campaign(["intelligence-show", "camp-intel", "--artifact-id", "sec-id"], td)
        assert show.returncode == EXIT_INVALID, show.stdout
        assert "hunter2" not in (show.stdout + show.stderr), "prohibited value must not be echoed"
        assert json.loads(show.stdout)["status"] == "artifact_invalid", show.stdout
        ok("show rejects a digest-consistent artifact with prohibited payload content")

        doc = json.loads(run_campaign(["intelligence-doctor", "camp-intel"], td).stdout)
        assert doc["status"] == "artifact_corrupt", doc
        assert doc["ok"] is False
        assert "hunter2" not in json.dumps(doc), "prohibited value must not be echoed by doctor"
        assert run_campaign(["intelligence-doctor", "camp-intel"], td).returncode == 1
        ok("doctor reports artifact_corrupt for prohibited payload content without echoing it")

        rebuild = run_campaign(["intelligence-rebuild", "camp-intel"], td)
        assert rebuild.returncode == EXIT_INVALID, rebuild.stdout
        assert json.loads(rebuild.stdout)["status"] == "rebuild_failed", rebuild.stdout
        assert "hunter2" not in (rebuild.stdout + rebuild.stderr)
        ok("rebuild fails explicitly on prohibited payload content without echoing it")

        listing = json.loads(run_campaign(["intelligence-list", "camp-intel"], td).stdout)
        assert "hunter2" not in json.dumps(listing), "corrupt artifact must never be silently indexed"
        ok("the corrupt artifact is never silently indexed")


def test_projection_campaign_mismatch_is_stale_not_repaired() -> None:
    with tempfile.TemporaryDirectory(prefix="intel-corr-proj-") as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        init_campaign(td, "camp-intel", repo)
        p = write_payload(Path(td) / "inputs" / "p.json", {"title": "X"})
        put(td, "camp-intel", "plan", "plan-1", p)
        proj = campaign_dir(td) / "intelligence" / "projection.json"
        data = json.loads(proj.read_text(encoding="utf-8"))
        data["campaign_id"] = "camp-other"
        proj.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tampered_bytes = proj.read_bytes()

        listing = json.loads(run_campaign(["intelligence-list", "camp-intel"], td).stdout)
        assert listing["status"] == "projection_stale", listing
        assert proj.read_bytes() == tampered_bytes, "list must not rewrite the foreign projection"
        ok("intelligence-list returns projection_stale for a different campaign_id without rewriting it")

        doc = json.loads(run_campaign(["intelligence-doctor", "camp-intel"], td).stdout)
        assert doc["status"] == "projection_stale", doc
        assert doc["ok"] is False
        assert proj.read_bytes() == tampered_bytes, "doctor must not repair the foreign projection"
        ok("intelligence-doctor returns projection_stale for a different campaign_id without repairing it")


def test_private_mode_violations_fail_doctor_and_restore_heals() -> None:
    with tempfile.TemporaryDirectory(prefix="intel-corr-mode-") as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        init_campaign(td, "camp-intel", repo)
        p = write_payload(Path(td) / "inputs" / "p.json", {"x": 1})
        put(td, "camp-intel", "plan", "mode-plan", p)

        iroot = campaign_dir(td) / "intelligence"
        arts = iroot / "artifacts"
        lock = iroot / "lock"
        proj = iroot / "projection.json"
        artifact_file = next(arts.glob("*.json"))

        targets = [
            ("intelligence dir", iroot, 0o700, 0o755),
            ("artifacts dir", arts, 0o700, 0o755),
            ("lock file", lock, 0o600, 0o644),
            ("projection file", proj, 0o600, 0o644),
            ("artifact file", artifact_file, 0o600, 0o644),
        ]
        for label, path, _required, bad_mode in targets:
            os.chmod(path, bad_mode)
            proc = run_campaign(["intelligence-doctor", "camp-intel"], td)
            doc = json.loads(proc.stdout)
            assert proc.returncode == 1, f"{label}: doctor must be non-healthy ({proc.stdout})"
            assert doc["status"] == "artifact_corrupt", f"{label}: {doc}"
            assert doc["ok"] is False
            assert "0o755" in json.dumps(doc) or "0o644" in json.dumps(doc), (
                f"{label}: details must carry the non-sensitive mode"
            )
            ok(f"doctor non-healthy after chmod of {label}")
            os.chmod(path, 0o700 if path.is_dir() else 0o600)

        final = json.loads(run_campaign(["intelligence-doctor", "camp-intel"], td).stdout)
        assert final["status"] == "healthy" and final["ok"] is True, final
        ok("restoring required modes makes doctor healthy again")


# --------------------------------------------------------------------------
# Compatibility
# --------------------------------------------------------------------------

def test_campaign_v1_schema_and_existing_commands_unchanged() -> None:
    schema = ROOT / "contracts" / "campaign-v1.schema.json"
    before = schema.read_bytes()
    with tempfile.TemporaryDirectory(prefix="intel-compat-") as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        init_campaign(td, "camp-compat", repo)
        p = write_payload(Path(td) / "inputs" / "p.json", {"title": "Compat"})
        put(td, "camp-compat", "plan", "plan-1", p)
        after = schema.read_bytes()
        assert before == after, "campaign-v1.schema.json must remain byte-for-byte unchanged"
        ok("campaign-v1.schema.json digest unchanged after intelligence operations")

        init2 = run_campaign(["init", "--id", "camp-compat2", "--title", "Compat 2", "--repo-root", str(repo)], td)
        assert init2.returncode == EXIT_OK
        wu_add = run_campaign(["wu-add", "camp-compat2", "--wu-id", "WU-001", "--objective", "Obj"], td)
        assert wu_add.returncode == EXIT_OK, wu_add.stderr
        dag = run_campaign(["dag", "camp-compat2"], td)
        assert dag.returncode == EXIT_OK
        listing = run_campaign(["list", "--json"], td)
        assert listing.returncode == EXIT_OK
        ok("existing campaign init/wu-add/dag/list commands remain behaviorally unchanged")


def test_no_update_or_delete_command_exists() -> None:
    proc = run_campaign(["intelligence-put", "--help"], "unused-state-home")
    assert proc.returncode == 0
    help_text = run_campaign(["intelligence-put", "--help"], "unused-state-home").stdout
    assert "--kind" in help_text and "--artifact-id" in help_text and "--input-json" in help_text
    for cmd in ("intelligence-delete", "intelligence-update", "intelligence-activate", "intelligence-create-wu"):
        probe = run_campaign([cmd, "camp-intel"], "unused-state-home")
        assert probe.returncode != 0, f"{cmd} must not exist"
        assert "invalid choice" in probe.stderr.lower()
    ok("no update/delete/activate/create-wu intelligence command exists")


def main() -> int:
    tests = [
        test_first_artifact_created_and_reused_byte_identical,
        test_same_artifact_id_two_campaigns_isolated,
        test_all_four_kinds_storable,
        test_show_validates_authoritative_artifact,
        test_list_deterministic_descriptors_no_payloads,
        test_doctor_healthy_and_private_modes,
        test_projection_delete_rebuild_restores,
        test_campaign_file_and_revision_unchanged,
        test_stdin_input_allowed,
        test_conflicts_rejected_and_original_preserved,
        test_invalid_campaign_and_artifact_ids,
        test_malformed_and_non_object_input_rejected,
        test_input_file_policy_rejected,
        test_secret_and_raw_content_rejected,
        test_concurrent_identical_writers_exactly_one_created,
        test_concurrent_conflicting_writers_one_winner_one_conflict,
        test_crash_after_artifact_before_projection_recovery,
        test_corrupt_artifact_reported_and_rebuild_fails_explicitly,
        test_projection_digest_tamper_reported_stale,
        test_unknown_envelope_field_rejected_everywhere,
        test_prohibited_payload_content_detected_on_read,
        test_projection_campaign_mismatch_is_stale_not_repaired,
        test_private_mode_violations_fail_doctor_and_restore_heals,
        test_campaign_v1_schema_and_existing_commands_unchanged,
        test_no_update_or_delete_command_exists,
    ]
    for test in tests:
        print(f"[{test.__name__}]")
        test()
    print(f"campaign intelligence store test: PASS ({TEST_COUNT} assertions)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
