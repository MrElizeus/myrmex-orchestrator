#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MEMORY = ROOT / "bin" / "myrmex-memory"
ZERO = "sha256:" + "a" * 64
RESTORE = "sha256:" + "b" * 64
METRICS = "sha256:" + "c" * 64


def run(*args: str, env: dict[str, str], ok: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run([str(MEMORY), *args], capture_output=True, text=True, env=env, timeout=20)
    if ok and result.returncode != 0:
        raise AssertionError(f"command failed: {args}\nstdout={result.stdout}\nstderr={result.stderr}")
    if not ok and result.returncode == 0:
        raise AssertionError(f"command unexpectedly succeeded: {args}\nstdout={result.stdout}")
    return result


def payload(result: subprocess.CompletedProcess[str]) -> dict:
    return json.loads(result.stdout)


def proposal_args(
    repo: Path,
    *,
    request: str,
    authority: str = "primary",
    authority_request: str | None = None,
    scope: str = "project",
    risk: str = "procedural",
    target: str = "bin/myrmex-memory",
    evidence: str | None = None,
    weakness: str = "retries are difficult to compare",
) -> list[str]:
    return [
        "procedural", "propose", "--scope", scope, "--authority-kind", authority,
        "--authority-request-id", authority_request or f"auth-{request}",
        "--request-id", request, "--expect-revision", "0", "--kind", "heuristic",
        "--version", "v1", "--weakness", weakness,
        "--expected-benefit", "bounded evidence makes retries comparable",
        "--target-path", target, "--evidence-json", evidence or '[{"artifact_sha256":"' + ZERO + '"}]',
        "--rollback-description", "restore the prior verified artifact",
        "--restore-artifact-sha256", RESTORE, "--risk-class", risk,
    ] + ([] if scope == "installation" else ["--repository-root", str(repo)])


def propose(repo: Path, env: dict[str, str], *, request: str, **kwargs: str) -> dict:
    return payload(run(*proposal_args(repo, request=request, **kwargs), env=env))["experiment"]


def candidate(repo: Path, env: dict[str, str], experiment: str, *, request: str, revision: int, **kwargs: str) -> dict:
    args = [
        "procedural", "candidate", experiment, "--repository-root", str(repo),
        "--authority-kind", "primary", "--authority-request-id", f"auth-{request}",
        "--request-id", request, "--expect-revision", str(revision),
        "--artifact-sha256", ZERO, "--base-identity", "base-identity",
        "--changed-path", "bin/myrmex-memory", "--isolation-mode", "disposable",
    ]
    args.extend(sum(([f"--{key.replace('_', '-')}", value] for key, value in kwargs.items()), []))
    return payload(run(*args, env=env))["experiment"]


def prepare(repo: Path, env: dict[str, str], *, request: str, risk: str = "procedural", authority: str = "primary", authority_request: str | None = None, evidence: str | None = None) -> tuple[dict, int]:
    experiment = propose(repo, env, request=request, risk=risk, authority=authority, authority_request=authority_request, evidence=evidence)
    experiment = candidate(repo, env, experiment["experiment_id"], request=request + "-candidate", revision=0)
    experiment_id = experiment["experiment_id"]
    tests = payload(run(
        "procedural", "tests", experiment_id, "--repository-root", str(repo),
        "--authority-kind", "primary", "--authority-request-id", "auth-tests-" + request,
        "--request-id", request + "-tests", "--expect-revision", "1", "--result", "pass",
        "--artifact-sha256", ZERO, "--command-summary", "focused procedural tests",
        env=env,
    ))["experiment"]
    assert tests["status"] == "tests_passed"
    verified = payload(run(
        "procedural", "verify", experiment_id, "--repository-root", str(repo),
        "--authority-kind", "primary", "--authority-request-id", "auth-verify-" + request,
        "--request-id", request + "-verify", "--expect-revision", "2", "--result", "pass",
        "--verifier-request-id", "verifier-" + request, "--verifier-task-id", "verifier-task",
        "--proposal-author-task-id", "author-task", "--artifact-sha256", ZERO, "--independent",
        env=env,
    ))["experiment"]
    assert verified["status"] == "verifier_passed"
    return verified, 3


def trial_start(repo: Path, env: dict[str, str], experiment: str, *, request: str, revision: int, authority: str = "frontier", elevated: bool = False, frontier_request: str | None = None, bounds: str | None = None) -> dict:
    args = [
        "procedural", "trial-start", experiment, "--repository-root", str(repo),
        "--authority-kind", authority, "--authority-request-id", "auth-trial-" + request,
        "--request-id", request, "--expect-revision", str(revision), "--trial-id", "trial-" + request,
        "--fixture-identity", "fixture-" + request, "--candidate-artifact-sha256", ZERO,
        "--baseline-metrics-artifact-sha256", METRICS, "--rollback-artifact-sha256", RESTORE,
    ]
    if elevated:
        args.append("--elevated")
    if frontier_request:
        args.extend(["--frontier-request-id", frontier_request])
    if bounds:
        args.extend(["--bounds-json", bounds])
    else:
        args.extend(["--max-work-units", "1"])
    return payload(run(*args, env=env))["experiment"]


with tempfile.TemporaryDirectory(prefix="myrmex-procedural-test-") as td:
    temp = Path(td)
    repo = temp / "repo"
    repo.mkdir()
    memory_home = temp / "memory"
    env = dict(os.environ, MYRMEX_MEMORY_HOME=str(memory_home), PYTHONDONTWRITEBYTECODE="1")

    # The canonical schema and packaged mirror are byte-identical, while the
    # existing semantic-memory schemas remain untouched.
    assert (ROOT / "contracts/memory-v1.schema.json").read_bytes() == (ROOT / "skills/myrmex-memory/assets/schemas/memory-v1.schema.json").read_bytes()
    assert (ROOT / "contracts/procedural-experiment-v1.schema.json").read_bytes() == (ROOT / "skills/myrmex-memory/assets/schemas/procedural-experiment-v1.schema.json").read_bytes()
    schema = json.loads((ROOT / "contracts/procedural-experiment-v1.schema.json").read_text(encoding="utf-8"))
    assert schema["$id"] == "urn:myrmex:schema:procedural-experiment:v1"
    bounds_schema = schema["$defs"]["bounds"]
    assert all("properties" in branch and branch["properties"] for branch in bounds_schema["anyOf"])

    # Evidence, scope, version, expected benefit, authority, and rollback are
    # required before the append-only namespace is created.
    missing_evidence = run(*proposal_args(repo, request="missing-evidence", evidence="[]"), env=env, ok=False)
    assert "procedural evidence" in missing_evidence.stderr
    missing_rollback = run(
        "procedural", "propose", "--repository-root", str(repo), "--authority-kind", "primary",
        "--authority-request-id", "auth-missing-rollback", "--request-id", "missing-rollback",
        "--expect-revision", "0", "--kind", "heuristic", "--version", "v1", "--weakness", "weakness",
        "--expected-benefit", "benefit", "--target-path", "bin/myrmex-memory",
        "--evidence-json", '[{"run_id":"run-1"}]', "--risk-class", "procedural", env=env, ok=False,
    )
    assert "rollback" in missing_rollback.stderr
    assert payload(run("procedural", "list", "--repository-root", str(repo), env=env))["experiments"] == []

    first = propose(repo, env, request="first")
    experiment_id = first["experiment_id"]
    first_stdout = run(*proposal_args(repo, request="replay"), env=env).stdout
    replay_stdout = run(*proposal_args(repo, request="replay"), env=env).stdout
    assert first_stdout == replay_stdout
    conflicting = proposal_args(repo, request="replay")
    conflicting[conflicting.index("bounded evidence makes retries comparable")] = "different benefit"
    assert "MEMORY_REQUEST_ID_REUSED" in run(*conflicting, env=env, ok=False).stderr
    stale = candidate(repo, env, experiment_id, request="stale", revision=0)
    assert stale["status"] == "isolated_candidate"
    assert "revision conflict" in run(
        "procedural", "tests", experiment_id, "--repository-root", str(repo),
        "--authority-kind", "primary", "--authority-request-id", "auth-stale-tests",
        "--request-id", "stale-tests", "--expect-revision", "0", "--result", "pass",
        "--artifact-sha256", ZERO, "--command-summary", "stale", env=env, ok=False,
    ).stderr

    out_of_scope = propose(repo, env, request="out-of-scope")
    assert "PROCEDURAL_TARGET_REJECTED" in run(
        "procedural", "candidate", out_of_scope["experiment_id"], "--repository-root", str(repo),
        "--authority-kind", "primary", "--authority-request-id", "auth-out-of-scope",
        "--request-id", "out-of-scope-candidate", "--expect-revision", "0", "--artifact-sha256", ZERO,
        "--base-identity", "base", "--changed-path", "docs/other.md", env=env, ok=False,
    ).stderr
    non_disposable = propose(repo, env, request="non-disposable")
    assert "PROCEDURAL_ISOLATION_REJECTED" in run(
        "procedural", "candidate", non_disposable["experiment_id"], "--repository-root", str(repo),
        "--authority-kind", "primary", "--authority-request-id", "auth-non-disposable",
        "--request-id", "non-disposable-candidate", "--expect-revision", "0", "--artifact-sha256", ZERO,
        "--base-identity", "base", "--changed-path", "bin/myrmex-memory", "--isolation-mode", "shared",
        env=env, ok=False,
    ).stderr
    child_only = propose(repo, env, request="child-only")
    assert "only child-agent" in run(
        "procedural", "candidate", child_only["experiment_id"], "--repository-root", str(repo),
        "--authority-kind", "primary", "--authority-request-id", "auth-child-only",
        "--request-id", "child-only-candidate", "--expect-revision", "0", "--artifact-sha256", ZERO,
        "--base-identity", "base", "--changed-path", "bin/myrmex-memory", "--candidate-source", "primary",
        env=env, ok=False,
    ).stderr

    # Failed tests and failed/non-independent verification become rejected
    # records and cannot reach the trial gate.
    failed_tests = propose(repo, env, request="failed-tests")
    candidate(repo, env, failed_tests["experiment_id"], request="failed-tests-candidate", revision=0)
    failed = payload(run(
        "procedural", "tests", failed_tests["experiment_id"], "--repository-root", str(repo),
        "--authority-kind", "primary", "--authority-request-id", "auth-failed-tests",
        "--request-id", "failed-tests-tests", "--expect-revision", "1", "--result", "fail",
        "--command-summary", "failed focused test", "--failure-reason", "assertion failed", env=env,
    ))["experiment"]
    assert failed["status"] == "rejected"
    assert "lifecycle" in run(
        "procedural", "trial-start", failed_tests["experiment_id"], "--repository-root", str(repo),
        "--authority-kind", "frontier", "--authority-request-id", "auth-failed-trial",
        "--request-id", "failed-tests-trial", "--expect-revision", "2", "--trial-id", "x",
        "--fixture-identity", "x", "--candidate-artifact-sha256", ZERO,
        "--max-work-units", "1", "--baseline-metrics-artifact-sha256", METRICS,
        "--rollback-artifact-sha256", RESTORE, env=env, ok=False,
    ).stderr.lower()
    failed_verify = propose(repo, env, request="failed-verify")
    candidate(repo, env, failed_verify["experiment_id"], request="failed-verify-candidate", revision=0)
    payload(run(
        "procedural", "tests", failed_verify["experiment_id"], "--repository-root", str(repo),
        "--authority-kind", "primary", "--authority-request-id", "auth-failed-verify-tests",
        "--request-id", "failed-verify-tests", "--expect-revision", "1", "--result", "pass",
        "--artifact-sha256", ZERO, "--command-summary", "focused tests", env=env,
    ))
    failed_verification = payload(run(
        "procedural", "verify", failed_verify["experiment_id"], "--repository-root", str(repo),
        "--authority-kind", "primary", "--authority-request-id", "auth-failed-verify",
        "--request-id", "failed-verify-again", "--expect-revision", "2", "--result", "fail",
        "--verifier-request-id", "verifier-fail", "--verifier-task-id", "author-task",
        "--proposal-author-task-id", "author-task", env=env,
    ))["experiment"]
    assert failed_verification["status"] == "rejected"

    # A valid procedural experiment cannot start an unbounded trial.
    unbounded, revision = prepare(repo, env, request="unbounded")
    unbounded_result = run(
        "procedural", "trial-start", unbounded["experiment_id"], "--repository-root", str(repo),
        "--authority-kind", "frontier", "--authority-request-id", "auth-unbounded",
        "--request-id", "unbounded-trial", "--expect-revision", str(revision), "--trial-id", "trial-unbounded",
        "--fixture-identity", "fixture", "--candidate-artifact-sha256", ZERO,
        "--bounds-json", '{"max_runs":null,"max_work_units":null,"expires_at":null}',
        "--baseline-metrics-artifact-sha256", METRICS, "--rollback-artifact-sha256", RESTORE,
        env=env, ok=False,
    )
    assert "unbounded" in unbounded_result.stderr or "bound" in unbounded_result.stderr
    for invalid_bounds in ('{"max_runs":0,"max_work_units":null,"expires_at":null}', '{"max_runs":null,"max_work_units":-1,"expires_at":null}'):
        invalid_result = run(
            "procedural", "trial-start", unbounded["experiment_id"], "--repository-root", str(repo),
            "--authority-kind", "frontier", "--authority-request-id", "auth-invalid-bound",
            "--request-id", "invalid-bound", "--expect-revision", str(revision), "--trial-id", "trial-invalid-bound",
            "--fixture-identity", "fixture", "--candidate-artifact-sha256", ZERO,
            "--bounds-json", invalid_bounds, "--baseline-metrics-artifact-sha256", METRICS,
            "--rollback-artifact-sha256", RESTORE, env=env, ok=False,
        )
        assert "positive integer" in invalid_result.stderr

    max_runs, revision = prepare(repo, env, request="max-runs")
    bounded_by_runs = trial_start(
        repo, env, max_runs["experiment_id"], request="max-runs-trial", revision=revision,
        bounds='{"max_runs":2,"max_work_units":null,"expires_at":null}',
    )
    assert bounded_by_runs["trial"]["bounds"] == {"max_runs": 2, "max_work_units": None, "expires_at": None}

    expires, revision = prepare(repo, env, request="expires")
    future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)).isoformat()
    bounded_by_expiry = trial_start(
        repo, env, expires["experiment_id"], request="expires-trial", revision=revision,
        bounds=json.dumps({"max_runs": None, "max_work_units": None, "expires_at": future}),
    )
    assert bounded_by_expiry["trial"]["bounds"]["expires_at"] == future

    # Valid Frontier-authorized procedural trial starts once, reaches promote,
    # and terminal replay is byte-stable.
    valid, revision = prepare(repo, env, request="valid")
    active = trial_start(repo, env, valid["experiment_id"], request="valid-trial", revision=revision)
    assert active["status"] == "bounded_trial_active"
    trial_log = memory_home / "procedural" / "projects"
    trial_events = next(path for path in trial_log.rglob("experiments.jsonl") if valid["experiment_id"].encode() in path.read_bytes())
    trial_event_count = len(trial_events.read_bytes().splitlines())
    trial_retry = run(
        "procedural", "trial-start", valid["experiment_id"], "--repository-root", str(repo),
        "--authority-kind", "frontier", "--authority-request-id", "auth-trial-valid-trial",
        "--request-id", "valid-trial", "--expect-revision", str(revision), "--trial-id", "trial-valid-trial",
        "--fixture-identity", "fixture-valid-trial", "--candidate-artifact-sha256", ZERO,
        "--max-work-units", "1", "--baseline-metrics-artifact-sha256", METRICS,
        "--rollback-artifact-sha256", RESTORE, env=env,
    )
    assert payload(trial_retry)["experiment"] == active
    assert len(trial_events.read_bytes().splitlines()) == trial_event_count
    success = payload(run(
        "procedural", "trial-outcome", valid["experiment_id"], "--repository-root", str(repo),
        "--authority-kind", "frontier", "--authority-request-id", "auth-success-outcome",
        "--request-id", "valid-outcome", "--expect-revision", str(revision + 1), "--result", "success",
        "--observed-metrics-artifact-sha256", METRICS, "--effectiveness-summary", "bounded trial succeeded", env=env,
    ))["experiment"]
    promoted = payload(run(
        "procedural", "promote", valid["experiment_id"], "--repository-root", str(repo),
        "--authority-kind", "frontier", "--authority-request-id", "auth-promote",
        "--request-id", "valid-promote", "--expect-revision", str(revision + 2), "--reason", "Frontier approved",
        "--evidence-json", '[{"frontier_request_id":"frontier-promote"}]', env=env,
    ))["experiment"]
    assert promoted["status"] == "promoted"
    promote_events_before_conflicts = trial_events.read_bytes()
    conflicting_reason = run(
        "procedural", "promote", valid["experiment_id"], "--repository-root", str(repo),
        "--authority-kind", "frontier", "--authority-request-id", "auth-promote",
        "--request-id", "valid-promote", "--expect-revision", str(revision + 2), "--reason", "Different reason",
        "--evidence-json", '[{"frontier_request_id":"frontier-promote"}]', env=env, ok=False,
    )
    assert "MEMORY_REQUEST_ID_REUSED" in conflicting_reason.stderr
    assert trial_events.read_bytes() == promote_events_before_conflicts
    conflicting_evidence = run(
        "procedural", "promote", valid["experiment_id"], "--repository-root", str(repo),
        "--authority-kind", "frontier", "--authority-request-id", "auth-promote",
        "--request-id", "valid-promote", "--expect-revision", str(revision + 2), "--reason", "Frontier approved",
        "--evidence-json", '[{"frontier_request_id":"different-frontier"}]', env=env, ok=False,
    )
    assert "MEMORY_REQUEST_ID_REUSED" in conflicting_evidence.stderr
    assert trial_events.read_bytes() == promote_events_before_conflicts

    # Core-control cannot be activated by a non-elevated human and accepts only
    # an elevated human with a matching Frontier evidence identity.
    core, revision = prepare(repo, env, request="core", risk="core_control")
    assert "elevated human" in run(
        "procedural", "trial-start", core["experiment_id"], "--repository-root", str(repo),
        "--authority-kind", "human", "--authority-request-id", "human-core",
        "--request-id", "core-trial-no-elevation", "--expect-revision", str(revision), "--trial-id", "core-trial",
        "--fixture-identity", "core-fixture", "--candidate-artifact-sha256", ZERO, "--max-work-units", "1",
        "--baseline-metrics-artifact-sha256", METRICS, "--rollback-artifact-sha256", RESTORE,
        env=env, ok=False,
    ).stderr
    # The proposal evidence for this experiment is replaced with a matching
    # Frontier handle by creating a distinct core proposal for the acceptance.
    core_matching, revision = prepare(
        repo, env, request="core-matching", risk="core_control", authority="frontier",
        authority_request="frontier-core", evidence='[{"artifact_sha256":"' + ZERO + '","frontier_request_id":"frontier-core"}]',
    )
    core_active = trial_start(repo, env, core_matching["experiment_id"], request="core-matching-trial", revision=revision, authority="human", elevated=True, frontier_request="frontier-core")
    assert core_active["status"] == "bounded_trial_active"

    # Regression and inconclusive outcomes revert and replay without duplicate
    # effects; a promoted experiment can later revert only with evidence.
    regression, revision = prepare(repo, env, request="regression")
    trial_start(repo, env, regression["experiment_id"], request="regression-trial", revision=revision)
    regression_args = [
        "procedural", "trial-outcome", regression["experiment_id"], "--repository-root", str(repo),
        "--authority-kind", "frontier", "--authority-request-id", "auth-regression-outcome",
        "--request-id", "regression-outcome", "--expect-revision", str(revision + 1), "--result", "regression",
        "--observed-metrics-artifact-sha256", METRICS, "--effectiveness-summary", "regression observed",
        "--rollback-artifact-sha256", RESTORE, "--evidence-json", '[{"artifact_sha256":"' + RESTORE + '"}]',
    ]
    reverted_stdout = run(*regression_args, env=env).stdout
    reverted_retry = run(*regression_args, env=env).stdout
    assert reverted_stdout == reverted_retry and payload(subprocess.CompletedProcess([], 0, stdout=reverted_retry, stderr=""))["experiment"]["status"] == "reverted"
    later, revision = prepare(repo, env, request="later-revert")
    trial_start(repo, env, later["experiment_id"], request="later-trial", revision=revision)
    payload(run(
        "procedural", "trial-outcome", later["experiment_id"], "--repository-root", str(repo),
        "--authority-kind", "frontier", "--authority-request-id", "auth-later-outcome",
        "--request-id", "later-outcome", "--expect-revision", str(revision + 1), "--result", "success",
        "--observed-metrics-artifact-sha256", METRICS, "--effectiveness-summary", "successful trial", env=env,
    ))
    payload(run(
        "procedural", "promote", later["experiment_id"], "--repository-root", str(repo),
        "--authority-kind", "human", "--authority-request-id", "human-promote",
        "--request-id", "later-promote", "--expect-revision", str(revision + 2), "--reason", "approved",
        "--evidence-json", '[{"artifact_sha256":"' + ZERO + '"}]', env=env,
    ))
    later_revert = run(
        "procedural", "revert", later["experiment_id"], "--repository-root", str(repo),
        "--authority-kind", "human", "--authority-request-id", "human-revert",
        "--request-id", "later-revert-decision", "--expect-revision", str(revision + 3), "--result", "inconclusive",
        "--observed-metrics-artifact-sha256", METRICS, "--effectiveness-summary", "later evidence inconclusive",
        "--rollback-artifact-sha256", RESTORE, "--evidence-json", '[{"run_id":"regression-run"}]', env=env,
    )
    assert payload(later_revert)["experiment"]["status"] == "reverted"

    # Installation is isolated and sanitized; collective scope and active
    # installation targets are rejected without creating project records.
    installation = propose(repo, env, request="installation", scope="installation", authority="frontier", authority_request="frontier-install")
    assert installation["scope"] == "installation"
    assert "installation proposals require" in run(
        *proposal_args(repo, request="installation-primary", scope="installation", authority="primary"), env=env, ok=False,
    ).stderr
    active_target = run(*proposal_args(repo, request="active-target", target="~/.config/opencode/agents"), env=env, ok=False)
    assert "active-installation" in active_target.stderr
    state_target = run(
        *proposal_args(repo, request="installation-state-target", scope="installation", authority="frontier", authority_request="frontier-state-target", target="bin/myrmex-state"),
        env=env, ok=False,
    )
    assert "bin/myrmex-state" in state_target.stderr
    assert payload(run("procedural", "list", "--scope", "installation", env=env))["experiments"] == [installation]
    collective = run(
        "procedural", "propose", "--scope", "collective", "--request-id", "collective", "--expect-revision", "0",
        env=env, ok=False,
    )
    assert collective.returncode != 0
    listed_installation = payload(run("procedural", "list", "--scope", "installation", env=env))
    assert [item["experiment_id"] for item in listed_installation["experiments"]] == [installation["experiment_id"]]
    assert not (memory_home / "installation" / "events.jsonl").exists()
    assert (memory_home / "procedural" / "installation" / "experiments.jsonl").is_file()

print("procedural learning test: PASS")
