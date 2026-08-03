#!/usr/bin/env python3
"""Focused contract tests for the read-only autonomous delivery-policy resolver."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RESOLVER = ROOT / "scripts" / "resolve-delivery-policy.py"


def write_json(path: Path, value: dict[str, Any]) -> Path:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    return path


def invoke(
    repository_root: Path,
    *,
    mode: str = "autonomous",
    profile: Path | None = None,
    run_authorization: Path | None = None,
    repository_config: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable, str(RESOLVER), "--repository-root", str(repository_root), "--mode", mode,
    ]
    if profile is not None:
        command += ["--installation-profile", str(profile)]
    if run_authorization is not None:
        command += ["--run-authorization-file", str(run_authorization)]
    if repository_config is not None:
        command += ["--repository-config", str(repository_config)]
    return subprocess.run(command, capture_output=True, text=True, timeout=20)


with tempfile.TemporaryDirectory(prefix="myrmex-delivery-policy-") as directory:
    root = Path(directory)
    repo = root / "repo"
    repo.mkdir()
    profile = write_json(
        root / "profile.json",
        {
            "$schema": "myrmex.config/v1",
            "delivery": {
                "tracking_issue": {
                    "required": True,
                    "reuse_matching_approved": True,
                    "create_when_missing": True,
                    "approval_marker": "status:approved",
                    "ask_on_ambiguous_match": True,
                }
            },
        },
    )

    # The installation policy permits the narrow autonomous metadata action
    # and reports exactly where every effective value came from.
    first = invoke(repo, profile=profile)
    assert first.returncode == 0, first.stderr
    resolved = json.loads(first.stdout)
    assert resolved["schema"] == "myrmex.delivery-policy/v1"
    assert len(resolved["policy_digest"]) == 64 and set(resolved["policy_digest"]) <= set("0123456789abcdef")
    assert resolved["decision"] == {
        "creation_policy": "authorized",
        "on_ambiguous_match": "ask",
        "on_missing_tracking_issue": "create",
    }
    assert all(source == "installation-profile" for source in resolved["provenance"].values())
    assert resolved["inputs"]["repository_config"] is None

    # Repository policy overrides only the values it names. It can turn off
    # automatic creation without losing the installed canonical marker.
    repository_policy = write_json(
        repo / "myrmex.json",
        {"delivery": {"tracking_issue": {"create_when_missing": False, "approval_marker": "workflow:approved"}}},
    )
    second = invoke(repo, profile=profile)
    assert second.returncode == 0, second.stderr
    resolved = json.loads(second.stdout)
    tracking = resolved["delivery"]["tracking_issue"]
    assert tracking["create_when_missing"] is False
    assert tracking["approval_marker"] == "workflow:approved"
    assert tracking["reuse_matching_approved"] is True
    assert resolved["decision"]["on_missing_tracking_issue"] == "ask"
    assert resolved["provenance"]["delivery.tracking_issue.create_when_missing"] == "repository-config"
    assert resolved["provenance"]["delivery.tracking_issue.approval_marker"] == "repository-config"
    assert resolved["inputs"]["repository_config"] == str(repository_policy.resolve())

    # A persisted run authorization is highest precedence and can disable the
    # gate completely. A disabled ambiguity prompt must block rather than
    # silently selecting an issue.
    run_authorization = write_json(
        root / "run-authorization.json",
        {"delivery": {"tracking_issue": {"required": False, "ask_on_ambiguous_match": False}}},
    )
    third = invoke(repo, profile=profile, run_authorization=run_authorization)
    assert third.returncode == 0, third.stderr
    resolved = json.loads(third.stdout)
    assert resolved["decision"]["on_missing_tracking_issue"] == "not-required"
    assert resolved["decision"]["creation_policy"] == "deny"
    assert resolved["decision"]["on_ambiguous_match"] == "block"
    assert resolved["provenance"]["delivery.tracking_issue.required"] == "run-authorization"
    assert resolved["provenance"]["delivery.tracking_issue.ask_on_ambiguous_match"] == "run-authorization"

    # Interactive delivery preserves a human choice on a missing issue even
    # when the installed autonomous policy authorizes creation.
    interactive_repo = root / "interactive-repo"
    interactive_repo.mkdir()
    interactive = invoke(interactive_repo, mode="interactive", profile=profile)
    assert interactive.returncode == 0, interactive.stderr
    assert json.loads(interactive.stdout)["decision"]["on_missing_tracking_issue"] == "ask"

    # Unknown or malformed policy keys fail closed instead of being ignored.
    invalid = write_json(
        root / "invalid.json",
        {"delivery": {"tracking_issue": {"create_when_missing": True, "invent_commitment": True}}},
    )
    rejected = invoke(interactive_repo, profile=invalid)
    assert rejected.returncode == 2
    error = json.loads(rejected.stderr)
    assert "unknown delivery.tracking_issue key" in error["error"]

    unsafe_marker = write_json(
        root / "unsafe-marker.json",
        {"delivery": {"tracking_issue": {"approval_marker": " status:approved "}}},
    )
    rejected_marker = invoke(interactive_repo, profile=unsafe_marker)
    assert rejected_marker.returncode == 2
    assert "approval_marker is invalid" in json.loads(rejected_marker.stderr)["error"]

    missing = invoke(interactive_repo, profile=root / "does-not-exist.json")
    assert missing.returncode == 2
    assert "installation profile does not exist" in json.loads(missing.stderr)["error"]

# The shipped profile provides the recommended autonomous default without
# requiring a repository to opt in to a hidden setting.
shipped = json.loads((ROOT / "profiles" / "myrmex-defaults.json").read_text(encoding="utf-8"))
assert shipped["delivery"]["tracking_issue"] == {
    "required": True,
    "reuse_matching_approved": True,
    "create_when_missing": True,
    "approval_marker": "status:approved",
    "ask_on_ambiguous_match": True,
}

print("delivery policy resolver test: PASS")
