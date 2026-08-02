#!/usr/bin/env python3
"""Resolve the safe tracking-issue delivery policy without side effects.

Configuration is merged per field, from low to high precedence:

    built-in fallback -> installation profile -> repository myrmex.json
    -> run-specific authorization

The command never calls GitHub and does not write run state.  Its normalized
JSON output is intended to be consumed later by the typed delivery operation
and by ``github-tracking-issue-recovery.py``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any


SCHEMA = "myrmex.delivery-policy/v1"
TRACKING_KEYS = (
    "required",
    "reuse_matching_approved",
    "create_when_missing",
    "approval_marker",
    "ask_on_ambiguous_match",
)
# These defaults fail closed for ambiguous matches and only permit the narrow
# metadata creation path in autonomous mode.  A repository or run can tighten
# any field; the installed profile makes the standing policy explicit.
BUILTIN_TRACKING_POLICY: dict[str, Any] = {
    "required": True,
    "reuse_matching_approved": True,
    "create_when_missing": True,
    "approval_marker": "status:approved",
    "ask_on_ambiguous_match": True,
}


class PolicyError(RuntimeError):
    """A policy source is malformed or cannot be safely resolved."""


def json_file(path: Path, label: str) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PolicyError(f"could not read {label}: {path}") from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PolicyError(f"invalid JSON in {label}: {path}") from exc
    if not isinstance(value, dict):
        raise PolicyError(f"{label} must contain a JSON object: {path}")
    return value


def tracking_override(document: dict[str, Any], label: str) -> dict[str, Any]:
    """Extract and validate the optional delivery.tracking_issue section."""
    delivery = document.get("delivery")
    if delivery is None:
        return {}
    if not isinstance(delivery, dict):
        raise PolicyError(f"delivery must be an object in {label}")
    tracking = delivery.get("tracking_issue")
    if tracking is None:
        return {}
    if not isinstance(tracking, dict):
        raise PolicyError(f"delivery.tracking_issue must be an object in {label}")
    unknown = sorted(set(tracking) - set(TRACKING_KEYS))
    if unknown:
        raise PolicyError(
            f"unknown delivery.tracking_issue key(s) in {label}: {', '.join(unknown)}"
        )

    result: dict[str, Any] = {}
    for key, value in tracking.items():
        if key == "approval_marker":
            if (
                not isinstance(value, str)
                or not value.strip()
                or value != value.strip()
                or len(value) > 128
                or any(char in value for char in "\r\n\0")
            ):
                raise PolicyError(f"delivery.tracking_issue.approval_marker is invalid in {label}")
        elif type(value) is not bool:
            raise PolicyError(f"delivery.tracking_issue.{key} must be boolean in {label}")
        result[key] = value
    return result


def default_installation_profile() -> Path | None:
    """Prefer a user's installed profile, with the packaged default as fallback."""
    config_dir = Path(os.environ.get("OPENCODE_CONFIG_DIR", "~/.config/opencode")).expanduser()
    installed = config_dir / "myrmex.json"
    if installed.is_file():
        return installed.resolve()
    packaged = Path(__file__).resolve().parents[1] / "profiles" / "myrmex-defaults.json"
    return packaged if packaged.is_file() else None


def load_optional(path: Path | None, label: str, *, explicit: bool) -> tuple[dict[str, Any], str | None]:
    if path is None:
        return {}, None
    path = path.expanduser().resolve()
    if not path.is_file():
        if explicit:
            raise PolicyError(f"{label} does not exist: {path}")
        return {}, None
    return tracking_override(json_file(path, label), label), str(path)


def resolve(
    *, mode: str, repository_root: Path, run_authorization: Path | None,
    repository_config: Path | None, installation_profile: Path | None,
    repository_config_explicit: bool, installation_profile_explicit: bool,
) -> dict[str, Any]:
    if not repository_root.is_dir():
        raise PolicyError(f"repository root does not exist: {repository_root}")

    effective = dict(BUILTIN_TRACKING_POLICY)
    provenance = {key: "built-in-default" for key in TRACKING_KEYS}
    inputs: dict[str, str | None] = {
        "run_authorization": None,
        "repository_config": None,
        "installation_profile": None,
    }

    if installation_profile is None and not installation_profile_explicit:
        installation_profile = default_installation_profile()
    profile, profile_path = load_optional(
        installation_profile, "installation profile", explicit=installation_profile_explicit,
    )
    if profile_path is not None:
        inputs["installation_profile"] = profile_path

    if repository_config is None and not repository_config_explicit:
        repository_config = repository_root / "myrmex.json"
    repo, repo_path = load_optional(
        repository_config, "repository Myrmex configuration", explicit=repository_config_explicit,
    )
    if repo_path is not None:
        inputs["repository_config"] = repo_path

    run, run_path = load_optional(run_authorization, "run authorization", explicit=run_authorization is not None)
    if run_path is not None:
        inputs["run_authorization"] = run_path

    for source, override in (
        ("installation-profile", profile),
        ("repository-config", repo),
        ("run-authorization", run),
    ):
        for key, value in override.items():
            effective[key] = value
            provenance[key] = source

    # The recovery helper accepts creation-policy values.  This resolver only
    # decides whether a standing policy permits the narrow action; a later
    # GitHub preflight still verifies that credentials and repository rights
    # actually allow it.
    if not effective["required"]:
        missing_action = "not-required"
        creation_policy = "deny"
    elif mode == "autonomous" and effective["create_when_missing"]:
        missing_action = "create"
        creation_policy = "authorized"
    else:
        missing_action = "ask"
        creation_policy = "ask"

    decision = {
        "on_missing_tracking_issue": missing_action,
        "on_ambiguous_match": "ask" if effective["ask_on_ambiguous_match"] else "block",
        "creation_policy": creation_policy,
    }
    resolved = {
        "schema": SCHEMA,
        "repository_root": str(repository_root),
        "mode": mode,
        "delivery": {"tracking_issue": effective},
        "decision": decision,
        "provenance": {f"delivery.tracking_issue.{key}": provenance[key] for key in TRACKING_KEYS},
        "inputs": inputs,
    }
    policy_digest = hashlib.sha256(json.dumps(
        resolved, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    return {**resolved, "policy_digest": policy_digest}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", required=True, help="repository whose myrmex.json may refine policy")
    parser.add_argument("--mode", choices=["autonomous", "interactive"], required=True)
    parser.add_argument(
        "--run-authorization-file",
        help="optional JSON authorization with delivery.tracking_issue overrides",
    )
    parser.add_argument(
        "--repository-config",
        help="explicit repository config path (defaults to <repository-root>/myrmex.json)",
    )
    parser.add_argument(
        "--installation-profile",
        help="explicit installed/default profile path (defaults to OPENCODE_CONFIG_DIR/myrmex.json)",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        resolved = resolve(
            mode=args.mode,
            repository_root=Path(args.repository_root).expanduser().resolve(),
            run_authorization=(Path(args.run_authorization_file) if args.run_authorization_file else None),
            repository_config=(Path(args.repository_config) if args.repository_config else None),
            installation_profile=(Path(args.installation_profile) if args.installation_profile else None),
            repository_config_explicit=args.repository_config is not None,
            installation_profile_explicit=args.installation_profile is not None,
        )
    except PolicyError as exc:
        print(json.dumps({"schema": SCHEMA, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(resolved, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
