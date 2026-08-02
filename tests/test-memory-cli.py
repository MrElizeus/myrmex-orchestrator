#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MEMORY = ROOT / "bin" / "myrmex-memory"


def run(*args: str, env: dict[str, str], ok: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run([str(MEMORY), *args], capture_output=True, text=True, env=env, timeout=20)
    if ok and result.returncode != 0:
        raise AssertionError(f"command failed: {args}\nstdout={result.stdout}\nstderr={result.stderr}")
    if not ok and result.returncode == 0:
        raise AssertionError(f"command unexpectedly succeeded: {args}\nstdout={result.stdout}")
    return result


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def evidence(path: str, value: str, **extra: str | None) -> str:
    payload: dict[str, str | None] = {"kind": "test-receipt", "path": path, "digest": value}
    payload.update(extra)
    return json.dumps([payload])


with tempfile.TemporaryDirectory(prefix="myrmex-memory-test-") as td:
    temp = Path(td)
    repo = temp / "repo"
    repo.mkdir()
    proof = repo / "proof.txt"
    proof.write_text("verified local evidence\n", encoding="utf-8")
    escaped = temp / "outside.txt"
    escaped.write_text("outside\n", encoding="utf-8")
    secret = repo / ".env"
    secret.write_text("API_KEY=not-a-real-secret\n", encoding="utf-8")
    env = dict(os.environ, MYRMEX_MEMORY_HOME=str(temp / "memory"), PYTHONDONTWRITEBYTECODE="1")
    proof_evidence = evidence(
        "proof.txt",
        digest(proof),
        run_id="run-1",
        work_unit_id="WU-1",
        commit_sha="a" * 40,
        verifier_request_id="verify-1",
        frontier_request_id="frontier-1",
        artifact_digest=digest(proof),
    )

    doctor = json.loads(run("doctor", env=env).stdout)
    assert doctor["ok"] is True and doctor["backend"] == "local-jsonl"

    # The stable project identity must survive ordinary SSH/HTTPS clone URL
    # differences without storing a raw remote URL in a record.
    identity_repo = temp / "identity-repo"
    identity_repo.mkdir()
    subprocess.run(["git", "init", "-q", str(identity_repo)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(identity_repo), "remote", "add", "origin", "git@github.com:Example/Memory-Repo.git"], check=True, capture_output=True, text=True)
    ssh_identity = run("project-id", "--repository-root", str(identity_repo), env=env).stdout.strip()
    subprocess.run(["git", "-C", str(identity_repo), "remote", "set-url", "origin", "https://github.com/Example/Memory-Repo.git"], check=True, capture_output=True, text=True)
    https_identity = run("project-id", "--repository-root", str(identity_repo), env=env).stdout.strip()
    assert ssh_identity == https_identity and ssh_identity.startswith("sha256:")

    # Candidate records are explicit and are not retrieved as trusted memory.
    candidate = json.loads(run(
        "candidate", "create", "--repository-root", str(repo), "--authority", "primary",
        "--kind", "architecture-invariant", "--claim", "All writes use the canonical transaction path",
        "--confidence", "0.96", "--evidence-json", proof_evidence,
        "--provenance-json", '{"source":"myrmex-verifier","run_id":"run-1","work_unit_id":"WU-1"}',
        "--freshness-json", '{"basis_commit_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}',
        "--request-id", "req-candidate-1", env=env,
    ).stdout)["memory"]
    assert candidate["schema"] == "myrmex.memory/v1"
    assert candidate["scope"] == "project" and candidate["status"] == "candidate"
    assert candidate["sensitivity"] == "project-private"
    assert candidate["evidence"][0]["verifier_request_id"] == "verify-1"
    assert candidate["freshness"]["last_confirmed_at"] is None

    hidden = json.loads(run(
        "search", "--repository-root", str(repo), "--query", "canonical transaction", env=env,
    ).stdout)
    assert hidden["records"] == []
    visible_candidate = json.loads(run(
        "search", "--repository-root", str(repo), "--query", "canonical transaction", "--include-candidates", env=env,
    ).stdout)
    assert len(visible_candidate["records"]) == 1
    assert visible_candidate["records"][0]["retrieval"]["scope_rank"] == 0
    assert visible_candidate["records"][0]["retrieval"]["staleness"] == "unverified"

    # The same request produces no duplicate candidate event.
    candidate_retry = json.loads(run(
        "candidate", "create", "--repository-root", str(repo), "--authority", "primary",
        "--kind", "architecture-invariant", "--claim", "All writes use the canonical transaction path",
        "--confidence", "0.96", "--evidence-json", proof_evidence,
        "--request-id", "req-candidate-1", env=env,
    ).stdout)
    assert candidate_retry["idempotent"] is True and candidate_retry["memory"]["memory_id"] == candidate["memory_id"]

    # Only the primary can persist any lifecycle record. A candidate cannot be
    # broadened or promoted without accessible evidence. Evidence paths/claims
    # are sanitized before write.
    wrong_candidate_authority = run(
        "candidate", "create", "--repository-root", str(repo), "--authority", "worker",
        "--kind", "warning", "--claim", "A worker must not persist memory", "--confidence", "0.5",
        "--request-id", "req-worker-candidate", env=env, ok=False,
    )
    assert "MEMORY_AUTHORITY_REQUIRED" in wrong_candidate_authority.stderr
    wrong_authority = run(
        "promote", candidate["memory_id"], "--repository-root", str(repo),
        "--authority", "worker", "--request-id", "req-promote-worker", "--expect-revision", "0", env=env, ok=False,
    )
    assert "MEMORY_AUTHORITY_REQUIRED" in wrong_authority.stderr
    missing_candidate = json.loads(run(
        "candidate", "create", "--repository-root", str(repo), "--authority", "primary", "--kind", "warning",
        "--claim", "Needs a verifiable receipt", "--confidence", "0.5", "--request-id", "req-missing", env=env,
    ).stdout)["memory"]
    missing = run(
        "promote", missing_candidate["memory_id"], "--repository-root", str(repo),
        "--authority", "primary", "--request-id", "req-promote-missing", "--expect-revision", "0", env=env, ok=False,
    )
    assert "MEMORY_EVIDENCE_REJECTED" in missing.stderr
    escaped_candidate = run(
        "candidate", "create", "--repository-root", str(repo), "--authority", "primary", "--kind", "warning",
        "--claim", "Outside evidence must be rejected", "--confidence", "0.5",
        "--evidence-json", evidence("../outside.txt", digest(escaped)), "--request-id", "req-escaped", env=env, ok=False,
    )
    assert "MEMORY_EVIDENCE_REJECTED" in escaped_candidate.stderr
    dotenv_candidate = run(
        "candidate", "create", "--repository-root", str(repo), "--authority", "primary", "--kind", "warning",
        "--claim", "Environment evidence must be rejected", "--confidence", "0.5",
        "--evidence-json", evidence(".env", digest(secret)), "--request-id", "req-dotenv", env=env, ok=False,
    )
    assert "MEMORY_SENSITIVITY_REJECTED" in dotenv_candidate.stderr
    secret_claim = run(
        "candidate", "create", "--repository-root", str(repo), "--authority", "primary", "--kind", "warning",
        "--claim", "api_key=abcdefghijklmnop", "--confidence", "0.5", "--request-id", "req-secret", env=env, ok=False,
    )
    assert "MEMORY_SENSITIVITY_REJECTED" in secret_claim.stderr

    # GitHub token detection requires a complete token shape, not merely a
    # prefix. Rejected values must not appear in output or the local store.
    github_tokens = [prefix + "A" * 36 for prefix in ("ghp_", "gho_", "ghu_", "ghs_", "ghr_")] + ["github_pat_" + "A" * 82]
    for index, token in enumerate(github_tokens):
        rejected = run(
            "candidate", "create", "--repository-root", str(repo), "--authority", "primary", "--kind", "warning",
            "--claim", "Synthetic secret " + token, "--confidence", "0.5",
            "--request-id", f"req-github-token-{index}", env=env, ok=False,
        )
        assert "MEMORY_SENSITIVITY_REJECTED" in rejected.stderr
        assert token not in rejected.stdout and token not in rejected.stderr
        assert all(token.encode("utf-8") not in stored.read_bytes() for stored in (temp / "memory").rglob("*") if stored.is_file())

    # Prefix-like prose and incomplete token-shaped values are ordinary text.
    for index, claim in enumerate((
        "The ghp_ prefix is documented but no token is present",
        "github_pat_example is a placeholder name",
        "ghp_" + "A" * 35,
    )):
        accepted = json.loads(run(
            "candidate", "create", "--repository-root", str(repo), "--authority", "primary", "--kind", "warning",
            "--claim", claim, "--confidence", "0.5", "--request-id", f"req-prefix-boundary-{index}", env=env,
        ).stdout)
        assert accepted["created"] is True

    secret_proof = repo / "secret-proof.txt"
    secret_proof.write_text("proof contains " + github_tokens[0] + "\n", encoding="utf-8")
    secret_evidence = evidence("secret-proof.txt", "sha256:" + "0" * 64)
    rejected_evidence = run(
        "candidate", "create", "--repository-root", str(repo), "--authority", "primary", "--kind", "warning",
        "--claim", "Evidence content must be scanned before persistence", "--confidence", "0.5",
        "--evidence-json", secret_evidence, "--request-id", "req-secret-evidence", env=env, ok=False,
    )
    assert "MEMORY_SENSITIVITY_REJECTED" in rejected_evidence.stderr
    assert github_tokens[0] not in rejected_evidence.stdout and github_tokens[0] not in rejected_evidence.stderr
    assert all(github_tokens[0].encode("utf-8") not in stored.read_bytes() for stored in (temp / "memory").rglob("*") if stored.is_file())
    unsanitized_installation = run(
        "candidate", "create", "--repository-root", str(repo), "--authority", "primary", "--scope", "installation", "--kind", "warning",
        "--claim", "Unsanitized installation lesson", "--confidence", "0.5", "--request-id", "req-installation", env=env, ok=False,
    )
    assert "MEMORY_SENSITIVITY_REJECTED" in unsanitized_installation.stderr
    invalid_freshness = run(
        "candidate", "create", "--repository-root", str(repo), "--authority", "primary", "--kind", "warning",
        "--claim", "Freshness must remain schema-compatible", "--confidence", "0.5",
        "--freshness-json", '{"valid_until":"not-a-timestamp"}', "--request-id", "req-bad-freshness", env=env, ok=False,
    )
    assert "freshness valid_until" in invalid_freshness.stderr

    promoted = json.loads(run(
        "promote", candidate["memory_id"], "--repository-root", str(repo),
        "--authority", "primary", "--request-id", "req-promote-1", "--expect-revision", "0", env=env,
    ).stdout)["memory"]
    assert promoted["status"] == "verified" and promoted["revision"] == 1
    assert promoted["freshness"]["last_confirmed_at"] is not None
    promote_retry = json.loads(run(
        "promote", candidate["memory_id"], "--repository-root", str(repo),
        "--authority", "primary", "--request-id", "req-promote-1", "--expect-revision", "0", env=env,
    ).stdout)
    assert promote_retry["idempotent"] is True and promote_retry["memory"]["revision"] == 1

    retrieved = json.loads(run(
        "search", "--repository-root", str(repo), "--query", "canonical transaction", env=env,
    ).stdout)
    assert len(retrieved["records"]) == 1
    retrieval = retrieved["records"][0]["retrieval"]
    assert retrieval["backend"] == "local-jsonl" and retrieval["scope_rank"] == 0 and retrieval["staleness"] == "fresh"
    unchanged = json.loads(run("show", candidate["memory_id"], "--repository-root", str(repo), env=env).stdout)
    assert unchanged == promoted, "retrieval must not reinforce or mutate a memory"

    # Installation promotion is a deliberate privacy boundary. Project-private
    # records cannot reuse their claim, and source paths/IDs cannot leak into
    # the installation event even when the raw proof is used transiently.
    unsanitized_promotion = run(
        "installation", "promote", candidate["memory_id"], "--repository-root", str(repo),
        "--source-expect-revision", "1", "--sanitized-claim", candidate["claim"],
        "--sanitization-reason", "A distinct generic lesson must be written", "--sanitized-evidence-json", proof_evidence,
        "--applicability-json", '{"tool_version_range":">=1.0,<2.0","model":"model-a"}',
        "--authority", "primary", "--request-id", "req-install-unsanitized", env=env, ok=False,
    )
    assert "MEMORY_SENSITIVITY_REJECTED" in unsanitized_promotion.stderr
    installation_memory = json.loads(run(
        "installation", "promote", candidate["memory_id"], "--repository-root", str(repo),
        "--source-expect-revision", "1", "--sanitized-claim", "Canonical transaction guards should be verified before promotion",
        "--sanitization-reason", "Removed repository names, local paths, and run identifiers from the reusable lesson",
        "--sanitized-evidence-json", proof_evidence,
        "--applicability-json", '{"tool_version_range":">=1.0,<2.0","model":"model-a"}',
        "--freshness-json", '{"ttl_seconds":0,"decay":"half-life:1"}',
        "--authority", "primary", "--request-id", "req-install-promote-1", env=env,
    ).stdout)["memory"]
    assert installation_memory["scope"] == "installation" and installation_memory["status"] == "verified"
    assert installation_memory["sensitivity"] == "sanitized" and installation_memory["project_identity"] is None
    assert installation_memory["provenance"]["source"] == "project-memory:" + candidate["memory_id"]
    assert installation_memory["provenance"]["run_id"] is None and installation_memory["provenance"]["work_unit_id"] is None
    assert installation_memory["evidence"] == [{
        "kind": "sanitized-receipt", "path": "sanitized/" + digest(proof).removeprefix("sha256:"), "digest": digest(proof),
        "run_id": None, "work_unit_id": None, "commit_sha": None, "verifier_request_id": None,
        "frontier_request_id": None, "artifact_digest": None,
    }]
    installation_history = json.loads(run(
        "history", installation_memory["memory_id"], "--scope", "installation", env=env,
    ).stdout)
    serialized_installation_history = json.dumps(installation_history, sort_keys=True)
    assert "proof.txt" not in serialized_installation_history and "run-1" not in serialized_installation_history
    assert "source_project_identity" not in serialized_installation_history

    # Auto retrieval keeps private project knowledge ahead of a reusable
    # installation lesson, while application mismatch is visible and lowers
    # only retrieval priority rather than deleting the lesson.
    auto_retrieved = json.loads(run(
        "search", "--repository-root", str(repo), "--scope", "auto", "--query", "canonical",
        "--tool-version", "1.5", "--model", "model-a", env=env,
    ).stdout)
    assert [item["retrieval"]["scope"] for item in auto_retrieved["records"][:2]] == ["project", "installation"]
    install_retrieval = next(item for item in auto_retrieved["records"] if item["memory"]["memory_id"] == installation_memory["memory_id"])
    assert install_retrieval["retrieval"]["applicability"]["status"] == "applicable"
    time.sleep(1.1)
    aged = json.loads(run(
        "search", "--scope", "installation", "--query", "canonical", "--tool-version", "1.5", "--model", "model-a", env=env,
    ).stdout)["records"]
    aged_installation = next(item for item in aged if item["memory"]["memory_id"] == installation_memory["memory_id"])
    assert aged_installation["retrieval"]["staleness"] == "ttl-expired"
    assert aged_installation["retrieval"]["freshness"]["decay_factor"] < 1, "TTL/decay lower priority without deletion"
    version_stale = json.loads(run(
        "search", "--scope", "installation", "--query", "canonical", "--tool-version", "2.5", "--model", "model-a", env=env,
    ).stdout)["records"]
    stale_installation = next(item for item in version_stale if item["memory"]["memory_id"] == installation_memory["memory_id"])
    assert stale_installation["retrieval"]["applicability"]["status"] == "stale"
    assert "version-stale" in stale_installation["retrieval"]["applicability"]["warnings"]

    # A read never reinforces memory. A reviewed confirmation has a distinct
    # usefulness statement and evidence, restores freshness, and is auditable
    # without increasing confidence automatically.
    confirmed_installation = json.loads(run(
        "confirm", installation_memory["memory_id"], "--scope", "installation", "--repository-root", str(repo),
        "--authority", "primary", "--request-id", "req-install-confirm-1", "--expect-revision", "0",
        "--reason", "Current verification still supports the reusable guard", "--usefulness", "The lesson prevented a repeated promotion mistake in a later work unit",
        "--evidence-json", proof_evidence, env=env,
    ).stdout)["memory"]
    assert confirmed_installation["revision"] == 1 and confirmed_installation["confidence"] == installation_memory["confidence"]
    refreshed = json.loads(run(
        "search", "--scope", "installation", "--query", "canonical", "--tool-version", "1.5", "--model", "model-a", env=env,
    ).stdout)["records"]
    refreshed_installation = next(item for item in refreshed if item["memory"]["memory_id"] == installation_memory["memory_id"])
    assert refreshed_installation["retrieval"]["freshness"]["status"] == "fresh"

    # Installation refutation/supersession is append-only and changes trusted
    # retrieval just like project-scoped records.
    installation_replacement = json.loads(run(
        "candidate", "create", "--scope", "installation", "--repository-root", str(repo), "--authority", "primary",
        "--kind", "architecture-invariant", "--claim", "Canonical transaction guards require current verification",
        "--confidence", "0.9", "--sensitivity", "sanitized", "--evidence-json", proof_evidence,
        "--applicability-json", '{"tool_version_range":">=1.0,<2.0","model":"model-a"}',
        "--request-id", "req-install-replacement-candidate", env=env,
    ).stdout)["memory"]
    installation_replacement = json.loads(run(
        "promote", installation_replacement["memory_id"], "--scope", "installation", "--repository-root", str(repo),
        "--authority", "primary", "--request-id", "req-install-replacement-promote", "--expect-revision", "0",
        "--evidence-json", proof_evidence, env=env,
    ).stdout)["memory"]
    installation_superseded = json.loads(run(
        "refute", installation_memory["memory_id"], "--scope", "installation", "--repository-root", str(repo), "--authority", "primary",
        "--request-id", "req-install-supersede", "--expect-revision", "1", "--resolution", "supersede",
        "--replacement-id", installation_replacement["memory_id"], "--replacement-expect-revision", "1",
        "--reason", "The revised generic verification lesson is more accurate", "--evidence-json", proof_evidence, env=env,
    ).stdout)
    assert installation_superseded["memory"]["status"] == "superseded"
    assert installation_superseded["replacement"]["supersedes"] == installation_memory["memory_id"]
    installation_history = json.loads(run(
        "history", installation_memory["memory_id"], "--scope", "installation", env=env,
    ).stdout)
    assert [event["type"] for event in installation_history] == ["memory.installation_promoted", "memory.confirmed", "memory.refuted"]
    assert installation_history[-1]["record"]["refutation"]["evidence"][0]["path"].startswith("sanitized/")
    installation_only = json.loads(run(
        "search", "--scope", "installation", "--query", "canonical", "--tool-version", "1.5", "--model", "model-a", env=env,
    ).stdout)["records"]
    assert [item["memory"]["memory_id"] for item in installation_only] == [installation_replacement["memory_id"]]

    # Work-unit metrics are a separate installation-local stream, not a memory
    # claim or state transition. They normalize all decision-quality signals
    # and retain only sanitized evidence handles.
    verification_json = json.dumps({"verdict": "pass", "request_id": "verify-metric-1", "evidence": json.loads(proof_evidence)})
    recovery_json = json.dumps({"events": ["reconcile", "receipt-replayed"], "recovered": True})
    tests_json = json.dumps([{
        "category": "unit", "outcome": "passed", "artifact_digest": digest(proof), "duration_seconds": 1.2,
    }])
    metric = json.loads(run(
        "metric", "record", "--repository-root", str(repo), "--work-unit-id", "WU-8", "--outcome", "partial",
        "--corrections-used", "2", "--defect-evidence-json", proof_evidence, "--verification-json", verification_json,
        "--recovery-json", recovery_json, "--tests-json", tests_json, "--run-id", "run-metric-1",
        "--tool-version", "1.5", "--model", "model-a", "--authority", "primary", "--request-id", "req-metric-1", env=env,
    ).stdout)["metric"]
    assert metric["schema"] == "myrmex.work-unit-metric/v1" and metric["scope"] == "installation"
    assert metric["outcome"] == "partial" and metric["corrections"]["used"] == 2
    assert metric["verification"]["verdict"] == "pass" and metric["recovery"]["recovered"] is True
    assert metric["tests"][0]["category"] == "unit" and metric["tests"][0]["outcome"] == "passed"
    assert metric["corrections"]["defect_evidence"][0]["path"].startswith("sanitized/")
    metrics = json.loads(run("metric", "list", "--repository-root", str(repo), env=env).stdout)["metrics"]
    assert [item["metric_id"] for item in metrics] == [metric["metric_id"]]
    serialized_metric = json.dumps(metric, sort_keys=True)
    for private_value in ("proof.txt", "WU-8", "run-metric-1", "verify-metric-1", "req-metric-1", "python3 tests/test-memory-cli.py"):
        assert private_value not in serialized_metric
    raw_metric_command = run(
        "metric", "record", "--repository-root", str(repo), "--work-unit-id", "WU-private", "--outcome", "success",
        "--corrections-used", "0", "--verification-json", verification_json, "--recovery-json", recovery_json,
        "--tests-json", '[{"name":"private path test","command":"python private/project/test.py","outcome":"passed","artifact_digest":null,"duration_seconds":1}]',
        "--authority", "primary", "--request-id", "req-metric-raw-command", env=env, ok=False,
    )
    assert "each metric test must contain category" in raw_metric_command.stderr

    # The key is created atomically in the installation store and is private.
    metric_key = temp / "memory" / "installation" / "metric-hmac.key"
    assert metric_key.is_file() and stat.S_IMODE(metric_key.stat().st_mode) == 0o600
    assert len(metric_key.read_bytes()) == 32

    stable_metric = json.loads(run(
        "metric", "record", "--repository-root", str(repo), "--work-unit-id", "WU-8", "--outcome", "success",
        "--corrections-used", "0", "--verification-json", json.dumps({"verdict": "pass", "request_id": "verify-stable", "evidence": []}),
        "--recovery-json", '{"events":[],"recovered":false}', "--tests-json", tests_json,
        "--authority", "primary", "--request-id", "req-metric-stable", env=env,
    ).stdout)["metric"]
    assert stable_metric["project_identity"] == metric["project_identity"]
    assert stable_metric["work_unit_id"] == metric["work_unit_id"]
    assert stable_metric["project_identity"].startswith("hmac-sha256:")

    other_env = dict(env, MYRMEX_MEMORY_HOME=str(temp / "other-memory"))
    other_metric = json.loads(run(
        "metric", "record", "--repository-root", str(repo), "--work-unit-id", "WU-8", "--outcome", "success",
        "--corrections-used", "0", "--verification-json", json.dumps({"verdict": "pass", "request_id": "verify-stable", "evidence": []}),
        "--recovery-json", '{"events":[],"recovered":false}', "--tests-json", tests_json,
        "--authority", "primary", "--request-id", "req-metric-stable", env=other_env,
    ).stdout)["metric"]
    assert other_metric["project_identity"] != metric["project_identity"]
    assert other_metric["work_unit_id"] != metric["work_unit_id"]
    assert other_metric["project_identity"].startswith("hmac-sha256:")

    metric_key.chmod(0o644)
    insecure_key = run("metric", "list", "--repository-root", str(repo), env=env, ok=False)
    assert "MEMORY_KEY_UNAVAILABLE" in insecure_key.stderr
    metric_key.chmod(0o600)

    # Legacy records with unkeyed handles remain readable and listable.
    project_id = run("project-id", "--repository-root", str(repo), env=env).stdout.strip()
    legacy_handle = lambda namespace, value: "sha256:" + hashlib.sha256(
        (namespace + "\0" + project_id + "\0" + value).encode("utf-8")
    ).hexdigest()
    legacy_metric = {
        "schema": "myrmex.work-unit-metric/v1", "metric_id": "metric_legacy01", "scope": "installation",
        "sensitivity": "sanitized", "project_identity": project_id,
        "work_unit_id": legacy_handle("work-unit", "legacy-WU"), "outcome": "success",
        "corrections": {"used": 0, "defect_evidence": []},
        "verification": {"verdict": "pass", "request_id": legacy_handle("verification-request", "legacy-verify"), "evidence": []},
        "recovery": {"events": [], "recovered": False},
        "tests": [{"category": "unit", "outcome": "passed", "artifact_digest": None, "duration_seconds": 0}],
        "applicability": {"tool_version": None, "model": None},
        "provenance": {"authority": "primary", "request_id": legacy_handle("metric-request", "legacy-request"), "run_id": None},
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    with (temp / "memory" / "installation" / "work-unit-metrics.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(legacy_metric) + "\n")
    listed_with_legacy = json.loads(run("metric", "list", "--repository-root", str(repo), env=env).stdout)["metrics"]
    assert any(item["metric_id"] == "metric_legacy01" for item in listed_with_legacy)

    # Canonical contracts and their packaged mirrors must remain byte-identical.
    for schema_name in ("memory-v1.schema.json", "work-unit-metric-v1.schema.json"):
        canonical = (ROOT / "contracts" / schema_name).read_bytes()
        mirror = (ROOT / "skills" / "myrmex-memory" / "assets" / "schemas" / schema_name).read_bytes()
        assert canonical == mirror

    # A corrupt derived index is never trusted over the append-only log.
    identity = json.loads(run(
        "search", "--repository-root", str(repo), "--query", "canonical transaction", env=env,
    ).stdout)["project_identity"]
    store = temp / "memory" / "projects" / hashlib.sha256(identity.encode()).hexdigest()
    index = store / "index.json"
    index.write_text("not JSON", encoding="utf-8")
    recovered_index = json.loads(run(
        "search", "--repository-root", str(repo), "--query", "canonical transaction", env=env,
    ).stdout)
    assert recovered_index["records"][0]["memory"]["memory_id"] == candidate["memory_id"]
    # A later durable write rebuilds the small index atomically.
    later = json.loads(run(
        "candidate", "create", "--repository-root", str(repo), "--authority", "primary", "--kind", "convention",
        "--claim", "Evidence lives beside the implementation", "--confidence", "0.7", "--request-id", "req-later", env=env,
    ).stdout)["memory"]
    assert json.loads(index.read_text())["schema"] == "myrmex.memory-index/v1"

    # Refutation preserves prior events and removes revoked memory from trusted retrieval.
    revoked = json.loads(run(
        "refute", candidate["memory_id"], "--repository-root", str(repo), "--authority", "primary",
        "--request-id", "req-revoke-1", "--expect-revision", "1", "--resolution", "revoke",
        "--reason", "The current proof refutes the prior invariant", "--evidence-json", proof_evidence, env=env,
    ).stdout)["memory"]
    assert revoked["status"] == "revoked" and revoked["refutation"]["resolution"] == "revoke"
    history = json.loads(run("history", candidate["memory_id"], "--repository-root", str(repo), env=env).stdout)
    assert [event["type"] for event in history] == ["memory.candidate_created", "memory.promoted", "memory.refuted"]
    assert json.loads(run(
        "search", "--scope", "project", "--repository-root", str(repo), "--query", "canonical transaction", env=env,
    ).stdout)["records"] == []

    # Supersession links both immutable records and leaves the newer verified one retrievable.
    old = json.loads(run(
        "candidate", "create", "--repository-root", str(repo), "--authority", "primary", "--kind", "decision",
        "--claim", "Use the old integration boundary", "--confidence", "0.8", "--evidence-json", proof_evidence,
        "--request-id", "req-old", env=env,
    ).stdout)["memory"]
    old = json.loads(run(
        "promote", old["memory_id"], "--repository-root", str(repo), "--authority", "primary",
        "--request-id", "req-old-promote", "--expect-revision", "0", env=env,
    ).stdout)["memory"]
    replacement = json.loads(run(
        "candidate", "create", "--repository-root", str(repo), "--authority", "primary", "--kind", "decision",
        "--claim", "Use the current integration boundary", "--confidence", "0.9", "--evidence-json", proof_evidence,
        "--request-id", "req-new", env=env,
    ).stdout)["memory"]
    replacement = json.loads(run(
        "promote", replacement["memory_id"], "--repository-root", str(repo), "--authority", "primary",
        "--request-id", "req-new-promote", "--expect-revision", "0", env=env,
    ).stdout)["memory"]
    superseded = json.loads(run(
        "refute", old["memory_id"], "--repository-root", str(repo), "--authority", "primary",
        "--request-id", "req-supersede", "--expect-revision", "1", "--resolution", "supersede",
        "--replacement-id", replacement["memory_id"], "--replacement-expect-revision", "1",
        "--reason", "The newer verified boundary supersedes this decision", "--evidence-json", proof_evidence, env=env,
    ).stdout)
    assert superseded["memory"]["status"] == "superseded"
    assert superseded["memory"]["superseded_by"] == replacement["memory_id"]
    assert superseded["replacement"]["supersedes"] == old["memory_id"]
    supersede_retry = json.loads(run(
        "refute", old["memory_id"], "--repository-root", str(repo), "--authority", "primary",
        "--request-id", "req-supersede", "--expect-revision", "1", "--resolution", "supersede",
        "--replacement-id", replacement["memory_id"], "--replacement-expect-revision", "1",
        "--reason", "The newer verified boundary supersedes this decision", "--evidence-json", proof_evidence, env=env,
    ).stdout)
    assert supersede_retry["idempotent"] is True
    reused_supersession_request = run(
        "refute", replacement["memory_id"], "--repository-root", str(repo), "--authority", "primary",
        "--request-id", "req-supersede", "--expect-revision", "2", "--resolution", "revoke",
        "--reason", "This is a distinct operation", "--evidence-json", proof_evidence, env=env, ok=False,
    )
    assert "MEMORY_REQUEST_ID_REUSED" in reused_supersession_request.stderr

    events_path = store / "events.jsonl"
    assert stat.S_IMODE(events_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.stat().st_mode) == 0o700

    schema = json.loads((ROOT / "contracts" / "memory-v1.schema.json").read_text(encoding="utf-8"))
    try:
        import jsonschema  # type: ignore
    except ImportError:
        pass
    else:
        jsonschema.Draft202012Validator(schema).validate(revoked)
        jsonschema.Draft202012Validator(schema).validate(superseded["memory"])
        jsonschema.Draft202012Validator(schema).validate(superseded["replacement"])
        jsonschema.Draft202012Validator(schema).validate(installation_superseded["memory"])
        jsonschema.Draft202012Validator(schema).validate(installation_superseded["replacement"])
        metric_schema = json.loads((ROOT / "contracts" / "work-unit-metric-v1.schema.json").read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(metric_schema).validate(metric)

    # A bad backend never returns a false success receipt.
    blocked_root = temp / "blocked-root"
    blocked_root.write_text("not a directory\n", encoding="utf-8")
    broken_env = dict(env, MYRMEX_MEMORY_HOME=str(blocked_root))
    broken = run("candidate", "create", "--repository-root", str(repo), "--authority", "primary", "--kind", "warning", "--claim", "Cannot persist", "--confidence", "0.5", "--request-id", "req-broken", env=broken_env, ok=False)
    assert "MEMORY_BACKEND_UNAVAILABLE" in broken.stderr and '"memory"' not in broken.stdout
    broken_installation = run(
        "candidate", "create", "--scope", "installation", "--repository-root", str(repo), "--authority", "primary",
        "--kind", "warning", "--claim", "Cannot persist installation memory", "--confidence", "0.5", "--sensitivity", "sanitized",
        "--applicability-json", '{"tool_version_range":"*"}', "--request-id", "req-broken-installation", env=broken_env, ok=False,
    )
    assert "MEMORY_BACKEND_UNAVAILABLE" in broken_installation.stderr and '"memory"' not in broken_installation.stdout

print("memory CLI test: PASS")
