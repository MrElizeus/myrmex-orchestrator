#!/usr/bin/env python3
"""Execute one explicitly authorized, local-only Git commit.

The helper owns the narrow side effect.  It never accepts arbitrary Git
arguments, never pushes, and treats the persisted operation ledger as the
recovery boundary for a crash after Git has created the commit.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE = ROOT / "bin" / "myrmex-state"


class LocalCommitError(Exception):
    pass


class PostEffectRecovery(LocalCommitError):
    def __init__(self, commit: dict[str, Any], reason: str, *, record_receipt: bool = True) -> None:
        super().__init__(reason)
        self.commit = commit
        self.record_receipt = record_receipt


DEFAULT_PROTECTED_PATHS = [".atl", ".playwright-mcp"]
GIT_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
SAFE_CONFIG = (
    ("core.fsmonitor", "false"),
    ("core.hooksPath", "/dev/null"),
    ("core.attributesFile", "/dev/null"),
    ("commit.gpgSign", "false"),
    ("tag.gpgSign", "false"),
    ("gpg.program", "/bin/false"),
    ("diff.external", ""),
    ("credential.helper", ""),
    ("core.sshCommand", "/bin/false"),
)


def safe_git_environment(repo: Path, index_path: Path | None = None) -> dict[str, str]:
    environment = dict(os.environ)
    for key in (
        "GIT_DIR", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS",
        "GIT_EXTERNAL_DIFF", "GIT_DIFF_OPTS", "GIT_ASKPASS", "SSH_ASKPASS", "GIT_SSH_COMMAND",
        "GIT_PROXY_COMMAND",
    ):
        environment.pop(key, None)
    environment.update({
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ALLOW_PROTOCOL": "__myrmex_no_protocol__",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
        "GIT_EDITOR": "true",
        "GIT_SEQUENCE_EDITOR": "true",
        "GIT_WORK_TREE": str(repo),
    })
    if index_path is not None:
        environment["GIT_INDEX_FILE"] = str(index_path)
    environment["GIT_CONFIG_COUNT"] = str(len(SAFE_CONFIG))
    for index, (key, value) in enumerate(SAFE_CONFIG):
        environment[f"GIT_CONFIG_KEY_{index}"] = key
        environment[f"GIT_CONFIG_VALUE_{index}"] = value
    return environment


def safe_git_command(
    repo: Path, *args: str, environment: dict[str, str] | None = None,
    input_bytes: bytes | None = None, text: bool = True, timeout: int = 30,
) -> subprocess.CompletedProcess[Any]:
    real_git = shutil.which("git")
    if not real_git:
        fail("Git executable is unavailable")
    command = [real_git, "--no-pager", "-C", str(repo)]
    for key, value in SAFE_CONFIG:
        command.extend(("-c", f"{key}={value}"))
    command.extend(args)
    try:
        stdin = subprocess.DEVNULL if input_bytes is None else None
        return subprocess.run(
            command, capture_output=True, input=input_bytes, text=text,
            stdin=stdin, timeout=timeout,
            env=environment or safe_git_environment(repo),
        )
    except subprocess.TimeoutExpired as exc:
        raise LocalCommitError(f"Git command timed out: {args[0] if args else 'git'}") from exc


def git(repo: Path, *args: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return safe_git_command(repo, *args, environment=safe_git_environment(repo), timeout=timeout)


def require_git(repo: Path, *args: str) -> str:
    result = git(repo, *args)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise LocalCommitError(detail or f"Git command failed: {' '.join(args)}")
    return result.stdout


def require_git_with_environment(repo: Path, environment: dict[str, str], *args: str) -> str:
    result = safe_git_command(repo, *args, environment=environment, timeout=30)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise LocalCommitError(detail or f"Git command failed: {' '.join(args)}")
    return result.stdout


def state_call(state_bin: Path, env: dict[str, str], *args: str) -> dict[str, Any]:
    result = subprocess.run([str(state_bin), *args], capture_output=True, text=True, env=env, timeout=30)
    if result.returncode != 0:
        raise LocalCommitError(result.stderr.strip() or result.stdout.strip() or "state command failed")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise LocalCommitError(f"state command returned invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise LocalCommitError("state command returned a non-object")
    return value


def state_show(state_bin: Path, env: dict[str, str], run_id: str) -> dict[str, Any]:
    return state_call(state_bin, env, "show", run_id)


def state_mutate(state_bin: Path, env: dict[str, str], run_id: str, revision: int, *args: str) -> dict[str, Any]:
    if len(args) < 2 or args[0] not in {"operation", "authorization"}:
        raise LocalCommitError("unsupported state mutation")
    command, action, *rest = args
    return state_call(state_bin, env, command, run_id, action, *rest, "--expect-revision", str(revision))


def fail(message: str) -> None:
    raise LocalCommitError(message)


def names_from_z(data: str) -> set[str]:
    return {item for item in data.split("\0") if item}


def git_snapshot(repo: Path, *args: str) -> str:
    return hashlib.sha256(require_git(repo, *args).encode()).hexdigest()


def path_is_under(path: str, parent: str) -> bool:
    parent = parent.rstrip("/")
    return path == parent or path.startswith(parent + "/")


def protected(path: str, protected_paths: list[str]) -> bool:
    return any(path_is_under(path, item.rstrip("/")) for item in protected_paths)


def authorization_matches(state: dict[str, Any], authorization_id: str) -> dict[str, Any]:
    for item in state.get("authorizations", []):
        if isinstance(item, dict) and item.get("authorization_id") == authorization_id:
            return item
    fail(f"authorization not found: {authorization_id}")


def candidate_diff_digest(repo: Path, auth: dict[str, Any]) -> str:
    """Hash the exact binary patch selected by the authorization."""
    output = require_git(
        repo, "diff", "--no-ext-diff", "--no-textconv", "--binary", "--no-color",
        auth["expected_head"], "--", *auth["allowed_paths"],
    )
    untracked: list[str] = []
    for relative_path in auth["allowed_paths"]:
        tracked = git(repo, "ls-files", "--error-unmatch", "--", relative_path).returncode == 0
        if tracked:
            continue
        path = safe_worktree_path(repo, relative_path)
        try:
            file_stat = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(file_stat.st_mode):
            content = os.fsencode(os.readlink(path))
            mode = "120000"
        elif stat.S_ISREG(file_stat.st_mode):
            content = path.read_bytes()
            mode = "100755" if file_stat.st_mode & 0o111 else "100644"
        else:
            fail(f"special or directory path is not allowed: {relative_path}")
        untracked.append(
            f"UNTRACKED\0{relative_path}\0{mode}\0{hashlib.sha256(content).hexdigest()}\n"
        )
    return hashlib.sha256((output + "".join(untracked)).encode()).hexdigest()


GOVERNED_SOURCE_ALIAS_KEYS = {
    "type", "status", "transport", "decision", "response", "response_message_id", "response_id",
    "plan", "proposed_plan", "next_work_unit", "next_work_unit_id", "completed_work_unit_id",
    "parent_gate", "parent_gate_intent", "parent_gate_operation", "parent_gate_continuation",
    "continuation", "continuation_of", "begin_wu", "BEGIN_WU",
}


def require_governed_source_field(source: dict[str, Any], field: str, expected: Any, label: str) -> Any:
    if field not in source:
        fail(f"GOVERNED_AUTHORIZATION_SOURCE_EVIDENCE_INVALID: {label} is missing")
    value = source[field]
    if field in {"operation_id", "request_id", "message_id", "work_unit_id"}:
        if not isinstance(value, str) or not value:
            fail(f"GOVERNED_AUTHORIZATION_SOURCE_EVIDENCE_INVALID: {label} is invalid")
    elif field == "response_type" and value != "sub_objective_complete":
        fail(f"GOVERNED_AUTHORIZATION_SOURCE_EVIDENCE_INVALID: {label} conflicts")
    elif field == "frontier_decision" and value != "ACCEPT":
        fail(f"GOVERNED_AUTHORIZATION_SOURCE_NOT_ACCEPTED: {label} conflicts")
    elif field == "transport_status" and value != "success":
        fail(f"GOVERNED_AUTHORIZATION_SOURCE_NOT_ACCEPTED: {label} is not canonical success")
    if value != expected:
        fail(f"GOVERNED_AUTHORIZATION_SOURCE_EVIDENCE_INVALID: {label} conflicts")
    return value


def reject_governed_source_markers(state: dict[str, Any], operation: dict[str, Any], effect: dict[str, Any], receipt: dict[str, Any]) -> None:
    intent = operation.get("intent")
    if operation.get("kind") != "frontier_exchange" or (
        isinstance(intent, dict) and (intent.get("parent_gate") is True or intent.get("purpose") == "parent_gate")
    ):
        fail("GOVERNED_AUTHORIZATION_SOURCE_NOT_ACCEPTED")
    operation_id_value = operation.get("operation_id")
    for gate in [state.get("parent_gate"), *state.get("parent_gate_history", [])]:
        if isinstance(gate, dict) and gate.get("operation_id") == operation_id_value:
            fail("GOVERNED_AUTHORIZATION_SOURCE_NOT_ACCEPTED")
    next_work_unit = state.get("next_work_unit")
    if isinstance(next_work_unit, dict):
        provenance = next_work_unit.get("provenance")
        if isinstance(provenance, dict) and provenance.get("operation_id") == operation_id_value:
            fail("GOVERNED_AUTHORIZATION_SOURCE_NOT_ACCEPTED")
    operation_markers = {
        "proposed_plan", "plan", "next_work_unit", "next_work_unit_id", "completed_work_unit_id",
        "parent_gate", "parent_gate_intent", "parent_gate_operation", "parent_gate_continuation",
        "continuation", "continuation_of", "begin_wu", "BEGIN_WU",
    }
    if any(key in operation for key in operation_markers):
        fail("GOVERNED_AUTHORIZATION_SOURCE_NOT_ACCEPTED")
    for source in (intent, effect, receipt):
        if isinstance(source, dict) and any(key in source for key in GOVERNED_SOURCE_ALIAS_KEYS):
            fail("GOVERNED_AUTHORIZATION_SOURCE_NOT_ACCEPTED")


def governed_standing_matches(state: dict[str, Any], auth: dict[str, Any]) -> None:
    standing = state.get("commit_policy_authorization")
    if not isinstance(standing, dict) or standing.get("status") != "active":
        fail("governed local_commit requires an active standing authorization")
    accepted = auth.get("accepted_work_unit")
    if not isinstance(accepted, dict):
        fail("GOVERNED_AUTHORIZATION_ACCEPTED_WU_MISMATCH")
    source = standing.get("source")
    if not isinstance(source, dict):
        fail("GOVERNED_AUTHORIZATION_SOURCE_IDENTITY_MISMATCH")
    if (
        auth.get("source_operation_id") != source.get("operation_id")
        or auth.get("source_request_id") != standing.get("source_request_id")
    ):
        fail("GOVERNED_AUTHORIZATION_SOURCE_IDENTITY_MISMATCH")
    operation = next(
        (item for item in state.get("pending_operations", [])
         if isinstance(item, dict) and item.get("operation_id") == source.get("operation_id")),
        None,
    )
    if not isinstance(operation, dict) or operation.get("kind") != "frontier_exchange":
        fail("GOVERNED_AUTHORIZATION_SOURCE_IDENTITY_MISMATCH")
    if operation.get("status") != "confirmed" and operation.get("effective_status") != "confirmed":
        fail("GOVERNED_AUTHORIZATION_SOURCE_NOT_CONFIRMED")
    effective_outcome = operation.get("effective_outcome")
    recovered = isinstance(effective_outcome, dict)
    if recovered:
        effect = effective_outcome.get("effect")
        receipt = effective_outcome.get("receipt")
    else:
        effect = operation.get("effect")
        receipt = operation.get("receipt")
    if not isinstance(effect, dict) or not isinstance(receipt, dict):
        fail("GOVERNED_AUTHORIZATION_SOURCE_EVIDENCE_MISSING")
    reject_governed_source_markers(state, operation, effect, receipt)
    operation_fields = {
        "operation_id": operation.get("operation_id"),
        "request_id": operation.get("effective_request_id" if recovered else "request_id"),
        "message_id": operation.get("effective_message_id" if recovered else "message_id"),
        "response_type": operation.get("effective_response_type" if recovered else "response_type"),
        "frontier_decision": operation.get("effective_frontier_decision" if recovered else "frontier_decision"),
        "transport_status": operation.get("effective_transport_status" if recovered else "transport_status"),
        "work_unit_id": operation.get("effective_work_unit_id" if recovered else "work_unit_id"),
    }
    source_request = require_governed_source_field(operation_fields, "request_id", operation_fields["request_id"], "operation request_id")
    source_message = require_governed_source_field(operation_fields, "message_id", operation_fields["message_id"], "operation message_id")
    require_governed_source_field(operation_fields, "operation_id", operation.get("operation_id"), "operation_id")
    response_type = require_governed_source_field(operation_fields, "response_type", "sub_objective_complete", "operation response_type")
    decision = require_governed_source_field(operation_fields, "frontier_decision", "ACCEPT", "operation frontier_decision")
    transport = require_governed_source_field(operation_fields, "transport_status", "success", "operation transport_status")
    work_unit_id = require_governed_source_field(operation_fields, "work_unit_id", operation_fields["work_unit_id"], "operation work_unit_id")
    if recovered:
        for field in ("request_id", "message_id", "frontier_decision", "transport_status"):
            require_governed_source_field(effective_outcome, field, operation_fields[field], f"effective outcome {field}")
    intent = operation.get("intent")
    if not isinstance(intent, dict):
        fail("GOVERNED_AUTHORIZATION_SOURCE_EVIDENCE_INVALID: operation intent is missing")
    require_governed_source_field(intent, "request_id", source_request, "intent request_id")
    require_governed_source_field(intent, "message_id", source_message, "intent message_id")
    for label, evidence in (("effect", effect), ("receipt", receipt)):
        require_governed_source_field(evidence, "operation_id", operation["operation_id"], f"{label} operation_id")
        require_governed_source_field(evidence, "request_id", source_request, f"{label} request_id")
        require_governed_source_field(evidence, "message_id", source_message, f"{label} message_id")
        require_governed_source_field(evidence, "response_type", response_type, f"{label} response_type")
        require_governed_source_field(evidence, "frontier_decision", decision, f"{label} frontier_decision")
        require_governed_source_field(evidence, "transport_status", transport, f"{label} transport_status")
        require_governed_source_field(evidence, "work_unit_id", work_unit_id, f"{label} work_unit_id")
    source_scope = auth.get("source_scope")
    if not isinstance(source_scope, dict):
        fail("GOVERNED_AUTHORIZATION_SOURCE_SCOPE_MISSING")
    for key in ("repository_root", "branch", "expected_head", "candidate_diff_sha", "allowed_paths", "commit_message"):
        if key not in effect or key not in receipt or effect.get(key) != receipt.get(key) or source_scope.get(key) != effect.get(key):
            fail("GOVERNED_AUTHORIZATION_SOURCE_SCOPE_MISMATCH")
    if source_request != standing.get("source_request_id") or source_message != source.get("message_id"):
        fail("GOVERNED_AUTHORIZATION_SOURCE_IDENTITY_MISMATCH")
    if source.get("work_unit_id") != work_unit_id or accepted.get("work_unit_id") != auth.get("work_unit_id") or work_unit_id != auth.get("work_unit_id"):
        fail("GOVERNED_AUTHORIZATION_ACCEPTED_WU_MISMATCH")
    if accepted.get("repository_root") != auth.get("repository_root") or accepted.get("branch") != auth.get("branch"):
        fail("GOVERNED_AUTHORIZATION_TARGET_IDENTITY_MISMATCH")


def operation_key(auth: dict[str, Any]) -> str:
    return f"local-commit:{auth['authorization_id']}:{auth['authority']}:{auth['request_id']}"


def operation_id(auth: dict[str, Any]) -> str:
    key = operation_key(auth)
    return "op-" + hashlib.sha256(("local_commit\0" + key).encode()).hexdigest()[:24]


def operation_for(state: dict[str, Any], op_id: str) -> dict[str, Any] | None:
    for item in state.get("pending_operations", []):
        if isinstance(item, dict) and item.get("operation_id") == op_id:
            return item
    return None


def validate_authorization(state: dict[str, Any], auth: dict[str, Any], repo: Path, branch: str, head: str, message: str) -> None:
    required = ("authorization_id", "request_id", "repository_root", "branch", "expected_head", "allowed_paths", "scope_digest", "commit_message", "commit_message_digest", "max_uses", "consumed_uses")
    if any(key not in auth for key in required):
        fail("authorization is malformed")
    if auth.get("kind") != "local_commit":
        fail("authorization kind is not local_commit")
    if auth.get("authority") not in {"user", "frontier"}:
        fail("authorization authority is invalid")
    governed = auth.get("grant_kind") == "governed"
    if governed:
        standing = state.get("commit_policy_authorization")
        if state.get("commit_policy") != "governed" or not isinstance(standing, dict) or standing.get("status") != "active":
            fail("governed local_commit requires an active standing authorization")
        governed_standing_matches(state, auth)
        if auth.get("parent_run_id") != state.get("run_id"):
            fail("GOVERNED_AUTHORIZATION_PARENT_IDENTITY_MISMATCH")
        if not isinstance(auth.get("work_unit_id"), str) or not auth.get("work_unit_id"):
            fail("governed local_commit work-unit identity is missing")
        if not isinstance(auth.get("candidate_diff_sha"), str) or not re.fullmatch(r"[0-9a-f]{64}", auth["candidate_diff_sha"]):
            fail("governed local_commit candidate diff digest is invalid")
        if not isinstance(auth.get("source_operation_id"), str) or not isinstance(auth.get("source_request_id"), str):
            fail("GOVERNED_AUTHORIZATION_SOURCE_IDENTITY_MISMATCH")
        if not isinstance(auth.get("source_scope"), dict):
            fail("GOVERNED_AUTHORIZATION_SOURCE_SCOPE_MISSING")
    if auth.get("status") != "open":
        fail("authorization is not open")
    if auth.get("max_uses") != 1 or auth.get("consumed_uses") != 0:
        fail("local_commit authorization must have exactly one remaining use")
    if auth.get("repository_root") != str(repo):
        fail("AUTHORIZATION_REPOSITORY_IDENTITY_MISMATCH")
    if auth.get("branch") != branch:
        fail("AUTHORIZATION_BRANCH_IDENTITY_MISMATCH")
    if auth.get("expected_head") != head:
        fail("AUTHORIZATION_HEAD_IDENTITY_MISMATCH")
    if auth.get("commit_message") != message:
        fail("AUTHORIZATION_MESSAGE_IDENTITY_MISMATCH")
    if auth.get("commit_message_digest") != hashlib.sha256(message.encode()).hexdigest():
        fail("authorization commit message digest is invalid")
    if not isinstance(auth.get("max_uses"), int) or not isinstance(auth.get("consumed_uses"), int) or auth["max_uses"] < 1 or auth["consumed_uses"] >= auth["max_uses"]:
        fail("authorization use counts are invalid")
    expires_at = auth.get("expires_at")
    if expires_at is not None:
        try:
            expiry = dt.datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        except ValueError as exc:
            raise LocalCommitError("authorization expiry is malformed") from exc
        if expiry.tzinfo is None or expiry <= dt.datetime.now(dt.timezone.utc):
            fail("AUTHORIZATION_EXPIRED")
    paths = auth.get("allowed_paths")
    if not isinstance(paths, list) or not paths or paths != sorted(paths) or len(set(paths)) != len(paths):
        fail("authorization allowed_paths are malformed")
    if auth.get("scope_digest") != scope_digest(auth):
        fail("AUTHORIZATION_SCOPE_DIGEST_MISMATCH")


def scope_digest(auth: dict[str, Any]) -> str:
    payload = {
        "repository_root": auth.get("repository_root"),
        "branch": auth.get("branch"),
        "expected_head": auth.get("expected_head"),
        "allowed_paths": auth.get("allowed_paths"),
        "commit_message": auth.get("commit_message"),
    }
    for key in ("parent_run_id", "work_unit_id", "candidate_diff_sha"):
        if auth.get(key) is not None:
            payload[key] = auth[key]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def ref_snapshot(repo: Path) -> dict[str, list[str]]:
    raw = require_git(repo, "for-each-ref", "--format=%(refname)%00%(objectname)%00%(symref)")
    snapshot: dict[str, list[str]] = {}
    for record in raw.splitlines():
        fields = record.split("\0")
        if len(fields) >= 3 and fields[0]:
            snapshot[fields[0]] = [fields[1], fields[2]]
    return snapshot


def git_baseline(repo: Path) -> dict[str, Any]:
    config_path = Path(require_git(repo, "rev-parse", "--git-path", "config").strip())
    if not config_path.is_absolute():
        config_path = (repo / config_path).resolve()
    try:
        config_digest = hashlib.sha256(config_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise LocalCommitError("Git configuration is unavailable") from exc
    return {
        "branch": require_git(repo, "branch", "--show-current").strip(),
        "head_symbolic": require_git(repo, "symbolic-ref", "-q", "HEAD").strip(),
        "config_digest": config_digest,
        "tags_digest": git_snapshot(repo, "tag", "--list"),
        "remotes_digest": git_snapshot(repo, "remote", "-v"),
        "refs": ref_snapshot(repo),
    }


def post_effect_mismatch(repo: Path, baseline: dict[str, Any], new_commit_sha: str) -> str | None:
    current = git_baseline(repo)
    for key in ("branch", "head_symbolic", "config_digest", "tags_digest", "remotes_digest"):
        if current.get(key) != baseline.get(key):
            return f"Git {key.replace('_digest', '')} changed during local commit"
    branch_ref = f"refs/heads/{baseline['branch']}"
    before_refs = baseline.get("refs")
    current_refs = current.get("refs")
    if not isinstance(before_refs, dict) or not isinstance(current_refs, dict):
        return "Git ref snapshot is malformed"
    if set(before_refs) != set(current_refs):
        return "unauthorized Git ref was created or deleted"
    before_branch = before_refs.get(branch_ref)
    current_branch = current_refs.get(branch_ref)
    if before_branch is None or before_branch[0] != baseline.get("expected_head"):
        return "authorized branch did not start at expected HEAD"
    if current_branch is None or current_branch[0] != new_commit_sha:
        return "authorized branch does not point to the authorized commit"
    for ref_name in before_refs:
        if ref_name != branch_ref and before_refs[ref_name] != current_refs[ref_name]:
            return f"unauthorized Git ref changed: {ref_name}"
    return None


def intent_for(auth: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    intent = {
        "authorization_id": auth["authorization_id"],
        "authority": auth["authority"],
        "request_id": auth["request_id"],
        "repository_root": auth["repository_root"],
        "branch": auth["branch"],
        "expected_head": auth["expected_head"],
        "allowed_paths": auth["allowed_paths"],
        "scope_digest": auth["scope_digest"],
        "commit_message": auth["commit_message"],
        "commit_message_digest": auth["commit_message_digest"],
        "pre_effect": baseline,
    }
    for key in (
        "grant_kind", "parent_run_id", "work_unit_id", "candidate_diff_sha",
        "source_operation_id", "source_request_id", "source_scope", "accepted_work_unit",
    ):
        if key in auth:
            intent[key] = auth[key]
    return intent


def discover_commit(repo: Path, head: str, auth: dict[str, Any]) -> dict[str, Any] | None:
    parents = require_git(repo, "show", "-s", "--format=%P", head).strip().split()
    if parents != [auth["expected_head"]]:
        return None
    message = require_git(repo, "show", "-s", "--format=%B", head).rstrip("\n")
    if message != auth["commit_message"]:
        return None
    paths = sorted(names_from_z(require_git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", head)))
    if paths != auth["allowed_paths"]:
        return None
    tree_sha = require_git(repo, "show", "-s", "--format=%T", head).strip()
    return {"commit_sha": head, "parent_sha": parents[0], "tree_sha": tree_sha, "paths": paths, "message": message}


def discover_unreferenced_commit(repo: Path, auth: dict[str, Any]) -> dict[str, Any] | None:
    output = require_git(repo, "fsck", "--no-reflogs", "--unreachable", "--no-progress")
    candidates: list[dict[str, Any]] = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[-2] in {"commit", "dangling"}:
            candidate = discover_commit(repo, fields[-1], auth)
            if candidate is not None:
                candidates.append(candidate)
    if len(candidates) > 1:
        fail("multiple unreferenced commits match the authorization")
    return candidates[0] if candidates else None


def validate_operation_identity(operation: dict[str, Any], auth: dict[str, Any]) -> None:
    expected_key = operation_key(auth)
    expected = {
        "authorization_id": auth["authorization_id"],
        "authority": auth["authority"],
        "request_id": auth["request_id"],
        "repository_root": auth["repository_root"],
        "branch": auth["branch"],
        "expected_head": auth["expected_head"],
        "allowed_paths": auth["allowed_paths"],
        "scope_digest": auth["scope_digest"],
        "commit_message": auth["commit_message"],
        "commit_message_digest": auth["commit_message_digest"],
    }
    if auth.get("grant_kind") == "governed":
        expected.update({
            "grant_kind": "governed", "parent_run_id": auth["parent_run_id"],
            "work_unit_id": auth["work_unit_id"], "candidate_diff_sha": auth["candidate_diff_sha"],
            "source_operation_id": auth["source_operation_id"],
            "source_request_id": auth["source_request_id"],
            "source_scope": auth["source_scope"],
            "accepted_work_unit": auth["accepted_work_unit"],
        })
    intent = operation.get("intent")
    if (
        operation.get("operation_id") != operation_id(auth)
        or operation.get("kind") != "local_commit"
        or operation.get("idempotency_key") != expected_key
        or not isinstance(intent, dict)
        or any(intent.get(key) != value for key, value in expected.items())
    ):
        fail("LOCAL_COMMIT_OPERATION_IDENTITY_MISMATCH")


def validate_commit_record(record: Any, auth: dict[str, Any], commit: dict[str, Any], *, receipt: bool) -> None:
    if not isinstance(record, dict):
        fail("LOCAL_COMMIT_RECEIPT_INTEGRITY_MISMATCH")
    expected = {
        "authorization_id": auth["authorization_id"],
        "authority": auth["authority"],
        "request_id": auth["request_id"],
        "commit_sha": commit["commit_sha"],
        "tree_sha": commit["tree_sha"],
        "branch": auth["branch"],
        "paths": auth["allowed_paths"],
    }
    if auth.get("grant_kind") == "governed":
        expected.update({
            "work_unit_id": auth["work_unit_id"],
            "candidate_diff_sha": auth["candidate_diff_sha"],
            "source_scope": auth["source_scope"],
        })
    if not receipt:
        expected["parent_sha"] = auth["expected_head"]
    if any(record.get(key) != value for key, value in expected.items()):
        fail("LOCAL_COMMIT_RECEIPT_INTEGRITY_MISMATCH")
    if receipt:
        if record.get("status") not in {"SUCCESS", "CONFIRMED"} or record.get("message") != auth["commit_message"]:
            fail("LOCAL_COMMIT_RECEIPT_INTEGRITY_MISMATCH")


def commit_from_receipt(repo: Path, auth: dict[str, Any], operation: dict[str, Any]) -> dict[str, Any]:
    receipt = operation.get("receipt")
    effect = operation.get("effect")
    if not isinstance(receipt, dict) or not isinstance(effect, dict):
        fail("LOCAL_COMMIT_RECEIPT_INTEGRITY_MISMATCH")
    commit_sha = receipt.get("commit_sha")
    if not isinstance(commit_sha, str) or not GIT_SHA_RE.fullmatch(commit_sha):
        fail("LOCAL_COMMIT_RECEIPT_INTEGRITY_MISMATCH")
    commit = discover_commit(repo, commit_sha, auth)
    if commit is None:
        fail("LOCAL_COMMIT_RECEIPT_INTEGRITY_MISMATCH")
    validate_commit_record(effect, auth, commit, receipt=False)
    validate_commit_record(receipt, auth, commit, receipt=True)
    return commit


def validate_closed_replay(repo: Path, auth: dict[str, Any], operation: dict[str, Any]) -> None:
    validate_operation_identity(operation, auth)
    if operation.get("status") != "confirmed":
        fail("LOCAL_COMMIT_REPLAY_INTEGRITY_MISMATCH")
    commit = commit_from_receipt(repo, auth, operation)
    intent = operation.get("intent")
    baseline = intent.get("pre_effect") if isinstance(intent, dict) else None
    if not isinstance(baseline, dict):
        fail("LOCAL_COMMIT_REPLAY_INTEGRITY_MISMATCH")
    mismatch = post_effect_mismatch(repo, baseline, commit["commit_sha"])
    if mismatch is not None:
        fail(f"LOCAL_COMMIT_REPLAY_INTEGRITY_MISMATCH: {mismatch}")


def fail_operation(state_bin: Path, env: dict[str, str], run_id: str, reason: str) -> None:
    try:
        state = state_show(state_bin, env, run_id)
        auth_id = env.get("MYRMEX_LOCAL_COMMIT_AUTHORIZATION_ID")
        if not auth_id:
            return
        auth = authorization_matches(state, auth_id)
        op = operation_for(state, operation_id(auth))
        if op is None or op.get("status") in {"confirmed", "failed", "abandoned"}:
            return
        state_mutate(
            state_bin, env, run_id, int(state["revision"]), "operation", "confirm",
            "--operation-id", op["operation_id"], "--status", "failed", "--reason", reason,
        )
    except LocalCommitError:
        pass


def finish_operation(
    state_bin: Path, env: dict[str, str], run_id: str, auth: dict[str, Any], commit: dict[str, Any],
    *, confirm: bool = True, record_receipt: bool = True,
) -> dict[str, Any]:
    op_id = operation_id(auth)
    effect = {
        "authorization_id": auth["authorization_id"], "commit_sha": commit["commit_sha"],
        "authority": auth["authority"], "request_id": auth["request_id"],
        "parent_sha": commit["parent_sha"], "tree_sha": commit["tree_sha"], "branch": auth["branch"], "paths": commit["paths"],
    }
    receipt = {
        "status": "SUCCESS", "authorization_id": auth["authorization_id"],
        "authority": auth["authority"], "request_id": auth["request_id"],
        "commit_sha": commit["commit_sha"], "branch": auth["branch"], "paths": commit["paths"],
        "tree_sha": commit["tree_sha"], "message": commit["message"], "push": "not_requested",
    }
    if auth.get("grant_kind") == "governed":
        effect.update({
            "parent_run_id": auth["parent_run_id"], "work_unit_id": auth["work_unit_id"],
            "candidate_diff_sha": auth["candidate_diff_sha"],
            "source_scope": auth["source_scope"],
        })
        receipt.update({
            "parent_run_id": auth["parent_run_id"], "work_unit_id": auth["work_unit_id"],
            "candidate_diff_sha": auth["candidate_diff_sha"],
            "source_scope": auth["source_scope"],
        })
    state = state_show(state_bin, env, run_id)
    op = operation_for(state, op_id)
    if op is None:
        fail("local_commit operation intent is missing")
    validate_operation_identity(op, auth)
    if op.get("status") in {"receipt-recorded", "confirmed"}:
        recorded = commit_from_receipt(Path(auth["repository_root"]), auth, op)
        if recorded["commit_sha"] != commit["commit_sha"]:
            fail("LOCAL_COMMIT_RECEIPT_INTEGRITY_MISMATCH")
    elif op.get("status") == "effect-observed":
        validate_commit_record(op.get("effect"), auth, commit, receipt=False)
    if op.get("status") not in {"effect-observed", "receipt-recorded", "confirmed"}:
        state = state_mutate(
            state_bin, env, run_id, int(state["revision"]), "operation", "observe",
            "--operation-id", op_id, "--effect-json", json.dumps(effect, sort_keys=True),
        )
    state = state_show(state_bin, env, run_id)
    op = operation_for(state, op_id)
    if op is None:
        fail("local_commit operation disappeared")
    if op.get("status") not in {"receipt-recorded", "confirmed"}:
        state = state_mutate(
            state_bin, env, run_id, int(state["revision"]), "operation", "receipt",
            "--operation-id", op_id, "--receipt-json", json.dumps(receipt, sort_keys=True),
        )
    state = state_show(state_bin, env, run_id)
    op = operation_for(state, op_id)
    if op is None:
        fail("local_commit operation disappeared")
    if not record_receipt:
        return state
    if not confirm:
        return state
    baseline = op.get("intent", {}).get("pre_effect") if isinstance(op.get("intent"), dict) else None
    if not isinstance(baseline, dict):
        raise PostEffectRecovery(commit, "local commit operation lacks a protected Git baseline")
    try:
        mismatch = post_effect_mismatch(Path(auth["repository_root"]), baseline, commit["commit_sha"])
    except LocalCommitError as exc:
        raise PostEffectRecovery(commit, str(exc)) from exc
    if mismatch is not None:
        raise PostEffectRecovery(commit, mismatch)
    if op.get("status") != "confirmed":
        state = state_mutate(
            state_bin, env, run_id, int(state["revision"]), "operation", "confirm",
            "--operation-id", op_id, "--status", "confirmed", "--reason", "authorized local commit completed",
        )
    state = state_show(state_bin, env, run_id)
    auth = authorization_matches(state, auth["authorization_id"])
    if auth.get("status") == "open":
        state = state_mutate(
            state_bin, env, run_id, int(state["revision"]), "authorization", "consume",
            "--authorization-id", auth["authorization_id"], "--operation-id", op_id,
            "--commit-sha", commit["commit_sha"],
        )
    return state


def has_preexisting_index_state(
    repo: Path, allowed_index_trees: set[str], output: Callable[..., str] = require_git,
) -> bool:
    if output(repo, "diff", "--no-ext-diff", "--no-textconv", "--cached", "--raw", "-z"):
        try:
            current_tree = output(repo, "write-tree").strip()
        except LocalCommitError:
            return True
        if current_tree not in allowed_index_trees:
            return True
    zero = "0" * 40
    for record in output(repo, "status", "--porcelain=v2", "-z", "--untracked-files=all").split("\0"):
        if not record:
            continue
        if record.startswith("u "):
            return True
        if record.startswith("1 "):
            fields = record.split(" ", 8)
            if len(fields) >= 8 and (fields[1][0] != "." or (fields[6] == zero and fields[7] == zero)):
                return True
        elif record.startswith("2 "):
            fields = record.split(" ", 9)
            if len(fields) >= 3 and fields[1][0] != ".":
                return True
    return False


def real_index_snapshot(repo: Path) -> tuple[Path, bytes | None]:
    path = Path(require_git(repo, "rev-parse", "--git-path", "index").strip())
    if not path.is_absolute():
        path = (repo / path).resolve()
    return path, path.read_bytes() if path.exists() else None


def assert_real_index_unchanged(path: Path, before: bytes | None, where: str = "") -> None:
    after = path.read_bytes() if path.exists() else None
    if after != before:
        before_digest = hashlib.sha256(before).hexdigest() if before is not None else "missing"
        after_digest = hashlib.sha256(after).hexdigest() if after is not None else "missing"
        suffix = f" ({where})" if where else ""
        fail(f"real Git index changed during local commit{suffix} [{path}]: {before_digest} -> {after_digest}")


def plumbing(repo: Path, environment: dict[str, str], *args: str, input_bytes: bytes | None = None) -> bytes:
    result = safe_git_command(repo, *args, environment=environment, input_bytes=input_bytes, text=False, timeout=60)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()
        fail(detail or f"Git plumbing command failed: {' '.join(args)}")
    return result.stdout


def git_identity_environment(repo: Path) -> dict[str, str]:
    identities: dict[str, str] = {}
    for variable, command in (("AUTHOR", "GIT_AUTHOR_IDENT"), ("COMMITTER", "GIT_COMMITTER_IDENT")):
        identity = require_git(repo, "var", command).strip()
        marker = identity.rfind("> ")
        if marker <= 0:
            fail("Git identity is malformed")
        name_email = identity[: marker + 1]
        if " <" not in name_email or not name_email.endswith(">"):
            fail("Git identity is malformed")
        name, email = name_email.rsplit(" <", 1)
        identities[f"GIT_{variable}_NAME"] = name
        identities[f"GIT_{variable}_EMAIL"] = email[:-1]
    return identities


def temporary_plumbing_environment(
    repo: Path, index_path: Path, identities: dict[str, str],
) -> dict[str, str]:
    environment = safe_git_environment(repo, index_path)
    environment.update(identities)
    return environment


def expected_tree_entry(
    repo: Path, expected_head: str, path: str, output: Callable[..., str] = require_git,
) -> tuple[str, str, str] | None:
    tree_output = output(repo, "ls-tree", "-z", expected_head, "--", path)
    if not tree_output:
        return None
    record = tree_output.split("\0", 1)[0]
    metadata, entry_path = record.split("\t", 1)
    mode, entry_type, object_id = metadata.split(" ", 2)
    if entry_path != path:
        return None
    if mode == "160000" or entry_type == "commit":
        fail("submodule paths are not allowed")
    return mode, entry_type, object_id


def safe_worktree_path(repo: Path, relative_path: str) -> Path:
    path = repo.joinpath(*relative_path.split("/"))
    root = repo.resolve()
    try:
        path.parent.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise LocalCommitError(f"path escapes repository: {relative_path}") from exc
    if path.is_symlink():
        target = (path.parent / os.readlink(path)).resolve(strict=False)
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise LocalCommitError(f"symlink escapes repository: {relative_path}") from exc
    return path


def update_private_index(
    repo: Path, environment: dict[str, str], auth: dict[str, Any],
    real_index_path: Path, real_index_before: bytes | None,
    output: Callable[..., str] = require_git,
) -> None:
    reported_index = Path(plumbing(repo, environment, "rev-parse", "--git-path", "index").decode().strip())
    if not reported_index.is_absolute():
        reported_index = (repo / reported_index).resolve()
    if reported_index != Path(environment["GIT_INDEX_FILE"]).resolve():
        fail("Git plumbing did not select the private index")
    assert_real_index_unchanged(real_index_path, real_index_before, "after private index probe")
    plumbing(repo, environment, "read-tree", auth["expected_head"])
    if Path(environment["GIT_INDEX_FILE"]).resolve() == real_index_path.resolve():
        fail("private index path resolved to the real Git index")
    assert_real_index_unchanged(real_index_path, real_index_before, "after private read-tree")
    for relative_path in auth["allowed_paths"]:
        path = safe_worktree_path(repo, relative_path)
        expected = expected_tree_entry(repo, auth["expected_head"], relative_path, output)
        try:
            file_stat = path.lstat()
        except FileNotFoundError:
            if expected is not None:
                plumbing(repo, environment, "update-index", "--remove", "--", relative_path)
            continue
        mode_bits = file_stat.st_mode
        if stat.S_ISLNK(mode_bits):
            mode = "120000"
            content = os.fsencode(os.readlink(path))
        elif stat.S_ISREG(mode_bits):
            mode = "100755" if mode_bits & 0o111 else "100644"
            content = path.read_bytes()
        else:
            fail(f"special or directory path is not allowed: {relative_path}")
        object_id = plumbing(repo, environment, "hash-object", "--no-filters", "-w", "--stdin", input_bytes=content).decode().strip()
        assert_real_index_unchanged(real_index_path, real_index_before, "after private hash-object")
        plumbing(repo, environment, "update-index", "--add", "--cacheinfo", f"{mode},{object_id},{relative_path}")
        assert_real_index_unchanged(real_index_path, real_index_before, "after private update-index")


def validate_preflight_scope(
    repo: Path, auth: dict[str, Any], protected_paths: list[str],
    real_index_path: Path, real_index_before: bytes | None,
) -> None:
    with tempfile.TemporaryDirectory(prefix="myrmex-git-preflight-", dir="/tmp") as preflight_dir:
        preflight_index = Path(preflight_dir) / "index"
        if real_index_before is not None:
            preflight_index.write_bytes(real_index_before)
        preflight_environment = dict(os.environ, GIT_INDEX_FILE=str(preflight_index), GIT_OPTIONAL_LOCKS="0")
        output = lambda _repo, *args: require_git_with_environment(repo, preflight_environment, *args)
        allowed_index_trees = {output(repo, "show", "-s", "--format=%T", auth["expected_head"]).strip()}
        if has_preexisting_index_state(repo, allowed_index_trees, output):
            fail("pre-existing staged changes are not allowed")
        changed = names_from_z(output(repo, "diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z"))
        changed.update(names_from_z(output(repo, "ls-files", "--others", "--exclude-standard", "-z")))
        allowed = set(auth["allowed_paths"])
        ignored = names_from_z(output(repo, "ls-files", "--others", "--ignored", "--exclude-standard", "-z"))
        protected_dirty = sorted({path for path in changed if protected(path, protected_paths)} | {path for path in ignored if protected(path, protected_paths)})
        if protected_dirty:
            fail("protected dirty paths are present: " + ", ".join(protected_dirty))
        outside = sorted(path for path in changed if path not in allowed)
        if outside:
            fail("unrelated dirty paths are outside authorization: " + ", ".join(outside))
        missing = sorted(allowed - changed)
        if missing:
            fail("authorized paths are not changed: " + ", ".join(missing))
    assert_real_index_unchanged(real_index_path, real_index_before, "after dirty-path preflight")


def create_commit_object(
    repo: Path, auth: dict[str, Any], protected_paths: list[str], baseline: dict[str, Any],
    real_index_path: Path, real_index_before: bytes | None, identities: dict[str, str],
) -> dict[str, Any]:
    protected_scope = sorted(path for path in auth["allowed_paths"] if protected(path, protected_paths))
    if protected_scope:
        fail("authorization includes protected paths: " + ", ".join(protected_scope))
    git_dir = Path(require_git(repo, "rev-parse", "--git-dir").strip())
    if not git_dir.is_absolute():
        git_dir = (repo / git_dir).resolve()
    markers = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG", "sequencer")
    if any((git_dir / marker).exists() for marker in markers):
        fail("merge, cherry-pick, revert, or bisect state is not allowed")
    validate_preflight_scope(repo, auth, protected_paths, real_index_path, real_index_before)
    with tempfile.TemporaryDirectory(prefix="myrmex-git-index-", dir="/tmp") as private_dir:
        directory = Path(private_dir)
        private_index = directory / "index"
        environment = temporary_plumbing_environment(repo, private_index, identities)
        assert_real_index_unchanged(real_index_path, real_index_before, "after private environment")
        private_output = lambda _repo, *args: require_git_with_environment(repo, environment, *args)
        update_private_index(repo, environment, auth, real_index_path, real_index_before, private_output)
        assert_real_index_unchanged(real_index_path, real_index_before, "after private index update")
        tree_sha = plumbing(repo, environment, "write-tree").decode().strip()
        assert_real_index_unchanged(real_index_path, real_index_before, "after write-tree")
        commit_sha = plumbing(
            repo, environment, "commit-tree", tree_sha, "-p", auth["expected_head"],
            input_bytes=auth["commit_message"].encode(),
        ).decode().strip()
        assert_real_index_unchanged(real_index_path, real_index_before, "after commit-tree")
    assert_real_index_unchanged(real_index_path, real_index_before)
    commit = discover_commit(repo, commit_sha, auth)
    if commit is None:
        fail("post-effect commit does not match authorization")
    commit["tree_sha"] = tree_sha
    return commit


def update_authorized_branch(repo: Path, auth: dict[str, Any], commit_sha: str) -> None:
    branch_ref = f"refs/heads/{auth['branch']}"
    result = safe_git_command(
        repo, "update-ref", branch_ref, commit_sha, auth["expected_head"],
        environment=safe_git_environment(repo), timeout=30,
    )
    if result.returncode != 0:
        fail(result.stderr.strip() or "authorized branch compare-and-swap failed")


def run_local_commit(args: argparse.Namespace) -> int:
    state_bin = Path(args.state_bin).expanduser().resolve()
    repo = Path(args.repository_root).expanduser().resolve()
    env = safe_git_environment(repo)
    state = state_show(state_bin, env, args.run_id)
    auth = authorization_matches(state, args.authorization_id)
    if state.get("commit_policy") not in {"authorized", "governed"}:
        fail("local_commit requires a governed commit policy")
    if state.get("commit_policy") == "governed" and auth.get("grant_kind") != "governed":
        fail("GOVERNED_LOCAL_COMMIT_GRANT_REQUIRED")
    if auth.get("grant_kind") == "governed":
        if state.get("commit_policy") != "governed" or state.get("status") != "active":
            fail("governed local_commit requires an active parent run")
        standing = state.get("commit_policy_authorization")
        if not isinstance(standing, dict) or standing.get("status") != "active":
            fail("governed local_commit requires an active standing authorization")
    if not repo.is_dir() or not (repo / ".git").exists():
        fail("repository_root is not a Git repository")
    if auth.get("repository_root") != str(repo):
        fail("AUTHORIZATION_REPOSITORY_IDENTITY_MISMATCH")
    real_index_path, real_index_before = real_index_snapshot(repo)
    branch = require_git(repo, "branch", "--show-current").strip()
    head = require_git(repo, "rev-parse", "HEAD").strip()
    if require_git(repo, "symbolic-ref", "-q", "HEAD").strip() != f"refs/heads/{branch}":
        fail("HEAD is not symbolically attached to the authorized branch")
    identities = git_identity_environment(repo)
    message = args.message if args.message is not None else auth.get("commit_message")
    if not isinstance(message, str) or not message:
        fail("commit message is required")
    op_id = operation_id(auth)
    try:
        existing_operation = operation_for(state, op_id)
        if auth.get("status") == "consumed":
            if existing_operation is None:
                fail("LOCAL_COMMIT_REPLAY_INTEGRITY_MISMATCH")
            validate_closed_replay(repo, auth, existing_operation)
            print(json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True))
            return 0
        validate_authorization(state, auth, repo, branch, auth["expected_head"], message)
        if auth.get("grant_kind") == "governed":
            actual_candidate_digest = candidate_diff_digest(repo, auth)
            if args.candidate_diff_sha is not None and args.candidate_diff_sha != actual_candidate_digest:
                fail("CANDIDATE_DIFF_IDENTITY_MISMATCH")
            if auth.get("candidate_diff_sha") != actual_candidate_digest:
                fail("CANDIDATE_DIFF_IDENTITY_MISMATCH")
        if existing_operation is not None:
            validate_operation_identity(existing_operation, auth)
        if head != auth["expected_head"]:
            if existing_operation is None:
                fail("AUTHORIZATION_HEAD_IDENTITY_MISMATCH")
            discovered = discover_commit(repo, head, auth)
            if discovered is None:
                fail("HEAD changed without a matching authorized commit")
            state = finish_operation(state_bin, env, args.run_id, auth, discovered)
            print(json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True))
            return 0
        baseline = git_baseline(repo)
        baseline["expected_head"] = auth["expected_head"]
        intent = intent_for(auth, baseline)
        state = state_mutate(
            state_bin, env, args.run_id, int(state["revision"]), "operation", "intent",
            "--kind", "local_commit", "--idempotency-key", operation_key(auth),
            "--authorization-id", args.authorization_id, "--intent-json", json.dumps(intent, sort_keys=True),
        )
        state = state_show(state_bin, env, args.run_id)
        assert_real_index_unchanged(real_index_path, real_index_before, "after operation intent")
        auth = authorization_matches(state, args.authorization_id)
        op = operation_for(state, op_id)
        if op is None:
            fail("local_commit operation intent was not persisted")
        validate_operation_identity(op, auth)
        if op.get("status") == "confirmed":
            commit = commit_from_receipt(repo, auth, op)
            if auth.get("status") == "open":
                state = finish_operation(state_bin, env, args.run_id, auth, commit)
            print(json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True))
            return 0
        current_head = require_git(repo, "rev-parse", "HEAD").strip()
        if current_head != auth["expected_head"]:
            discovered = discover_commit(repo, current_head, auth)
            if discovered is None:
                fail("HEAD changed without a matching authorized commit")
            state = finish_operation(state_bin, env, args.run_id, auth, discovered)
        elif op.get("status") in {"effect-observed", "receipt-recorded"}:
            effect = op.get("effect")
            if not isinstance(effect, dict) or not isinstance(effect.get("commit_sha"), str):
                fail("local_commit effect is malformed")
            recovered = discover_commit(repo, effect["commit_sha"], auth)
            if recovered is None:
                fail("persisted local_commit effect is not a matching commit")
            assert_real_index_unchanged(real_index_path, real_index_before)
            update_authorized_branch(repo, auth, recovered["commit_sha"])
            assert_real_index_unchanged(real_index_path, real_index_before)
            state = finish_operation(state_bin, env, args.run_id, auth, recovered)
        elif op.get("status") == "intent":
            protected_paths = DEFAULT_PROTECTED_PATHS + [
                str(item) for item in state.get("protected_dirty_paths", []) if isinstance(item, str)
            ]
            validate_preflight_scope(repo, auth, protected_paths, real_index_path, real_index_before)
            if auth.get("grant_kind") == "governed" and candidate_diff_digest(repo, auth) != auth.get("candidate_diff_sha"):
                fail("CANDIDATE_DIFF_IDENTITY_MISMATCH")
            commit = discover_unreferenced_commit(repo, auth)
            if commit is None:
                commit = create_commit_object(
                    repo, auth, protected_paths, baseline, real_index_path, real_index_before, identities,
                )
            state = finish_operation(state_bin, env, args.run_id, auth, commit, confirm=False, record_receipt=False)
            try:
                update_authorized_branch(repo, auth, commit["commit_sha"])
            except LocalCommitError as exc:
                raise PostEffectRecovery(commit, str(exc), record_receipt=False) from exc
            assert_real_index_unchanged(real_index_path, real_index_before)
            state = finish_operation(state_bin, env, args.run_id, auth, commit)
        else:
            fail("local_commit operation is not recoverable at the expected HEAD")
        print(json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True))
        return 0
    except PostEffectRecovery as exc:
        env["MYRMEX_LOCAL_COMMIT_AUTHORIZATION_ID"] = args.authorization_id
        try:
            finish_operation(
                state_bin, env, args.run_id, auth, exc.commit,
                confirm=False, record_receipt=exc.record_receipt,
            )
        except LocalCommitError:
            pass
        raise LocalCommitError(f"post-effect commit requires recovery: {exc}")
    except LocalCommitError as exc:
        env["MYRMEX_LOCAL_COMMIT_AUTHORIZATION_ID"] = args.authorization_id
        fail_operation(state_bin, env, args.run_id, str(exc))
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    commit = sub.add_parser("commit", help="perform one bounded authorized local commit")
    commit.add_argument("--run-id", required=True)
    commit.add_argument("--authorization-id", required=True)
    commit.add_argument("--repository-root", required=True)
    commit.add_argument("--message")
    commit.add_argument("--candidate-diff-sha")
    commit.add_argument("--state-bin", default=str(DEFAULT_STATE))
    commit.set_defaults(func=run_local_commit)
    return parser


def main() -> int:
    try:
        parsed = build_parser().parse_args()
        return int(parsed.func(parsed))
    except LocalCommitError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
