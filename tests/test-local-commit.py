#!/usr/bin/env python3
from __future__ import annotations

import json
import hashlib
import os
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "bin" / "myrmex-state"
HELPER = ROOT / "scripts" / "myrmex-git-local.py"


def command(args: list[str], *, env: dict[str, str], ok: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, capture_output=True, text=True, env=env, timeout=30)
    if ok and result.returncode != 0:
        raise AssertionError(f"failed: {args}\nstdout={result.stdout}\nstderr={result.stderr}")
    if not ok and result.returncode == 0:
        raise AssertionError(f"unexpected success: {args}")
    return result


def git(repo: Path, *args: str, ok: bool = True) -> subprocess.CompletedProcess[str]:
    return command(["git", "-C", str(repo), *args], env=os.environ.copy(), ok=ok)


def state(env: dict[str, str], *args: str, ok: bool = True) -> dict:
    result = command([str(STATE), *args], env=env, ok=ok)
    if not result.stdout.strip():
        return {}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return result.stdout.strip()


def helper(env: dict[str, str], run_id: str, auth_id: str, repo: Path, message: str | None = None, ok: bool = True) -> dict:
    args = [str(HELPER), "commit", "--run-id", run_id, "--authorization-id", auth_id, "--repository-root", str(repo)]
    if message is not None:
        args.extend(["--message", message])
    result = command(args, env=env, ok=ok)
    return json.loads(result.stdout) if result.stdout.strip() else {}


def setup_repo(td: str) -> tuple[Path, dict[str, str], str, str]:
    repo = Path(td) / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Local Commit Test")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", "--", "base.txt")
    git(repo, "commit", "-qm", "base")
    branch = git(repo, "branch", "--show-current").stdout.strip()
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    env = dict(os.environ, MYRMEX_STATE_HOME=str(Path(td) / "state"), PYTHONDONTWRITEBYTECODE="1")
    return repo, env, branch, head


def create_authorization(env: dict[str, str], run_id: str, repo: Path, branch: str, head: str, path: str, message: str, *, expires: str | None = "2030-01-01T00:00:00+00:00", request_id: str | None = None) -> dict:
    revision = state(env, "show", run_id)["revision"]
    args = [
        "authorization", run_id, "create", "--authority", "user", "--request-id", request_id or f"request-{path}-{message}",
        "--repository-root", str(repo), "--branch", branch, "--expected-head", head,
        "--allowed-path", path, "--message", message,
    ]
    if expires is not None:
        args.extend(["--expires-at", expires])
    return state(env, *args, "--expect-revision", str(revision))


def baseline(repo: Path, expected_head: str | None = None) -> dict:
    def digest(*args: str) -> str:
        return hashlib.sha256(git(repo, *args).stdout.encode()).hexdigest()
    refs = {}
    for record in git(repo, "for-each-ref", "--format=%(refname)%00%(objectname)%00%(symref)").stdout.splitlines():
        fields = record.split("\0")
        refs[fields[0]] = [fields[1], fields[2]]
    return {
        "branch": git(repo, "branch", "--show-current").stdout.strip(),
        "head_symbolic": git(repo, "symbolic-ref", "-q", "HEAD").stdout.strip(),
        "expected_head": expected_head or git(repo, "rev-parse", "HEAD").stdout.strip(),
        "config_digest": hashlib.sha256((repo / ".git" / "config").read_bytes()).hexdigest(),
        "tags_digest": digest("tag", "--list"),
        "remotes_digest": digest("remote", "-v"),
        "refs": refs,
    }


with tempfile.TemporaryDirectory(prefix="myrmex-local-commit-") as td:
    repo, env, branch, base = setup_repo(td)
    run_id = state(
        env, "init", "--run-id", "local-commit-run", "--objective", "local commit test",
        "--repository-root", str(repo), "--branch", branch, "--base-sha", base,
        "--mode", "autonomous", "--scope", "narrow", "--commit-policy", "authorized", "--push-policy", "deny",
    )
    initialized = state(env, "show", run_id)
    assert initialized["revision"] == 0 and initialized["authorizations"] == []

    auth = create_authorization(env, "local-commit-run", repo, branch, base, "owned.txt", "feat: owned")
    authorization = auth["authorizations"][0]
    assert authorization["kind"] == "local_commit"
    assert authorization["max_uses"] == 1 and authorization["consumed_uses"] == 0
    assert authorization["allowed_paths"] == ["owned.txt"]
    replay = create_authorization(env, "local-commit-run", repo, branch, base, "owned.txt", "feat: owned")
    assert replay["revision"] == auth["revision"]
    conflict_result = command([
        str(STATE), "authorization", "local-commit-run", "create", "--authority", "user", "--request-id",
        "request-owned.txt-feat: owned", "--repository-root", str(repo), "--branch", branch,
        "--expected-head", base, "--allowed-path", "owned.txt", "--message", "feat: changed",
        "--expires-at", "2030-01-01T00:00:00+00:00", "--expect-revision", str(auth["revision"]),
    ], env=env, ok=False)
    assert "IDEMPOTENCY_CONFLICT" in conflict_result.stderr

    (repo / "owned.txt").write_text("authorized\n", encoding="utf-8")
    committed = helper(env, "local-commit-run", authorization["authorization_id"], repo)
    assert committed["authorizations"][0]["status"] == "consumed"
    operation = committed["pending_operations"][0]
    assert operation["kind"] == "local_commit" and operation["status"] == "confirmed"
    commit_sha = operation["receipt"]["commit_sha"]
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == commit_sha
    assert git(repo, "remote").stdout.strip() == ""
    command(["git", "-C", str(repo), "read-tree", "HEAD"], env=os.environ.copy())
    second_replay = helper(env, "local-commit-run", authorization["authorization_id"], repo)
    assert second_replay["authorizations"][0]["status"] == "consumed"
    assert second_replay["pending_operations"][0]["receipt"]["commit_sha"] == commit_sha
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == commit_sha

    # A persisted operation intent plus a matching post-effect commit is
    # recovered without creating a second commit.
    recovery_run = "local-commit-recovery"
    recovery_state = state(
        env, "init", "--run-id", recovery_run, "--objective", "recovery test", "--repository-root", str(repo),
        "--branch", branch, "--base-sha", commit_sha, "--mode", "autonomous", "--scope", "narrow",
        "--commit-policy", "authorized", "--push-policy", "deny",
    )
    recovery_auth = create_authorization(env, recovery_run, repo, branch, commit_sha, "recovered.txt", "feat: recovered")
    recovery_auth_item = recovery_auth["authorizations"][0]
    intent = {key: recovery_auth_item[key] for key in (
        "authorization_id", "authority", "request_id", "repository_root", "branch", "expected_head",
        "allowed_paths", "scope_digest", "commit_message", "commit_message_digest",
    )}
    intent["pre_effect"] = baseline(repo, commit_sha)
    intent_state = state(
        env, "operation", recovery_run, "intent", "--kind", "local_commit", "--idempotency-key",
        f"local-commit:{recovery_auth_item['authorization_id']}:{recovery_auth_item['authority']}:{recovery_auth_item['request_id']}", "--authorization-id",
        recovery_auth_item["authorization_id"], "--intent-json", json.dumps(intent),
        "--expect-revision", str(recovery_auth["revision"]),
    )
    assert intent_state["pending_operations"][0]["status"] == "intent"
    (repo / "recovered.txt").write_text("recovered\n", encoding="utf-8")
    recovery_index = Path(td) / "recovery-index"
    recovery_env = dict(os.environ, GIT_INDEX_FILE=str(recovery_index))
    command(["git", "-C", str(repo), "read-tree", commit_sha], env=recovery_env)
    command(["git", "-C", str(repo), "add", "--", "recovered.txt"], env=recovery_env)
    recovery_tree = command(["git", "-C", str(repo), "write-tree"], env=recovery_env).stdout.strip()
    recovery_commit = subprocess.run(
        ["git", "-C", str(repo), "commit-tree", recovery_tree, "-p", commit_sha],
        input="feat: recovered", capture_output=True, text=True, env=recovery_env, check=False,
    )
    if recovery_commit.returncode != 0:
        raise AssertionError(recovery_commit.stderr)
    recovery_effect = {
        "authorization_id": recovery_auth_item["authorization_id"], "authority": recovery_auth_item["authority"],
        "request_id": recovery_auth_item["request_id"], "commit_sha": recovery_commit.stdout.strip(),
        "tree_sha": recovery_tree, "parent_sha": commit_sha, "branch": branch, "paths": ["recovered.txt"],
    }
    observed_recovery = state(
        env, "operation", recovery_run, "observe", "--operation-id", intent_state["pending_operations"][0]["operation_id"],
        "--effect-json", json.dumps(recovery_effect), "--expect-revision", str(intent_state["revision"]),
    )
    assert observed_recovery["pending_operations"][0]["status"] == "effect-observed"
    recovery_receipt = {
        "status": "SUCCESS", "authorization_id": recovery_auth_item["authorization_id"],
        "authority": recovery_auth_item["authority"], "request_id": recovery_auth_item["request_id"],
        "commit_sha": recovery_commit.stdout.strip(), "branch": branch, "paths": ["recovered.txt"],
        "tree_sha": recovery_tree, "message": "feat: recovered", "push": "not_requested",
    }
    recorded_recovery = state(
        env, "operation", recovery_run, "receipt", "--operation-id", intent_state["pending_operations"][0]["operation_id"],
        "--receipt-json", json.dumps(recovery_receipt), "--expect-revision", str(observed_recovery["revision"]),
    )
    confirmed_recovery = state(
        env, "operation", recovery_run, "confirm", "--operation-id", intent_state["pending_operations"][0]["operation_id"],
        "--status", "confirmed", "--reason", "replay test", "--expect-revision", str(recorded_recovery["revision"]),
    )
    assert confirmed_recovery["pending_operations"][0]["status"] == "confirmed"
    command(["git", "-C", str(repo), "update-ref", f"refs/heads/{branch}", recovery_commit.stdout.strip(), commit_sha], env=os.environ.copy())
    recovered = helper(env, recovery_run, recovery_auth_item["authorization_id"], repo)
    assert recovered["authorizations"][0]["status"] == "consumed"
    assert git(repo, "log", "--format=%s", "-2").stdout.splitlines().count("feat: recovered") == 1

    # Protected and unrelated paths cannot enter an exact authorization.
    guarded_run = "local-commit-guarded"
    guarded_head = git(repo, "rev-parse", "HEAD").stdout.strip()
    state(
        env, "init", "--run-id", guarded_run, "--objective", "guarded paths", "--repository-root", str(repo),
        "--branch", branch, "--base-sha", guarded_head, "--protected-dirty-path", ".atl",
        "--mode", "autonomous", "--scope", "narrow", "--commit-policy", "authorized", "--push-policy", "deny",
    )
    protected_auth = create_authorization(env, guarded_run, repo, branch, guarded_head, ".atl/blocked.txt", "feat: blocked")
    (repo / ".atl").mkdir()
    (repo / ".atl/blocked.txt").write_text("protected\n", encoding="utf-8")
    wrong_repo = Path(td) / "wrong-repo"
    wrong_repo.mkdir()
    git(wrong_repo, "init", "-q")
    helper(env, guarded_run, protected_auth["authorizations"][0]["authorization_id"], wrong_repo, ok=False)
    helper(env, guarded_run, protected_auth["authorizations"][0]["authorization_id"], repo, ok=False)

    outside_auth = create_authorization(env, guarded_run, repo, branch, guarded_head, "owned-again.txt", "feat: exact")
    (repo / "owned-again.txt").write_text("owned\n", encoding="utf-8")
    (repo / "outside.txt").write_text("outside\n", encoding="utf-8")
    helper(env, guarded_run, outside_auth["authorizations"][0]["authorization_id"], repo, ok=False)

    staged_auth = create_authorization(env, guarded_run, repo, branch, guarded_head, "staged.txt", "feat: staged")
    (repo / "staged.txt").write_text("staged\n", encoding="utf-8")
    git(repo, "add", "--", "staged.txt")
    helper(env, guarded_run, staged_auth["authorizations"][0]["authorization_id"], repo, ok=False)
    git(repo, "restore", "--staged", "--", "staged.txt")

    # Expiry, wrong message, whitespace, and hook failures fail closed.
    expiry_auth = create_authorization(env, guarded_run, repo, branch, guarded_head, "expired.txt", "feat: expired", expires="2000-01-01T00:00:00+00:00")
    (repo / "expired.txt").write_text("expired\n", encoding="utf-8")
    helper(env, guarded_run, expiry_auth["authorizations"][0]["authorization_id"], repo, ok=False)

    message_auth = create_authorization(env, guarded_run, repo, branch, guarded_head, "message.txt", "feat: exact message")
    (repo / "message.txt").write_text("message\n", encoding="utf-8")
    helper(env, guarded_run, message_auth["authorizations"][0]["authorization_id"], repo, message="feat: wrong", ok=False)

    diff_auth = create_authorization(env, guarded_run, repo, branch, guarded_head, "whitespace.txt", "feat: whitespace")
    (repo / "whitespace.txt").write_text("bad \n", encoding="utf-8")
    helper(env, guarded_run, diff_auth["authorizations"][0]["authorization_id"], repo, ok=False)

    hook_auth = create_authorization(env, guarded_run, repo, branch, guarded_head, "hook.txt", "feat: hook")
    (repo / "hook.txt").write_text("hook\n", encoding="utf-8")
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    helper(env, guarded_run, hook_auth["authorizations"][0]["authorization_id"], repo, ok=False)
    hook.unlink()

    # Adversarial cases use a clean fixture so each failure exercises its own
    # gate rather than being masked by another protected dirty path.
    clean_parent = Path(td) / "clean-fixture"
    clean_parent.mkdir()
    clean_repo, clean_env, clean_branch, clean_head = setup_repo(str(clean_parent))
    clean_run = state(
        clean_env, "init", "--run-id", "local-commit-adversarial", "--objective", "adversarial local commit",
        "--repository-root", str(clean_repo), "--branch", clean_branch, "--base-sha", clean_head,
        "--mode", "autonomous", "--scope", "narrow", "--commit-policy", "authorized", "--push-policy", "deny",
    )
    max_result = command([
        str(STATE), "authorization", clean_run, "create", "--authority", "user", "--request-id", "max-two",
        "--repository-root", str(clean_repo), "--branch", clean_branch, "--expected-head", clean_head,
        "--allowed-path", "max-two.txt", "--message", "feat: max-two", "--max-uses", "2", "--expect-revision", "0",
    ], env=clean_env, ok=False)
    assert "--max-uses 1" in max_result.stderr

    remote = Path(td) / "bare-remote"
    remote.mkdir()
    git(remote, "init", "--bare", "-q")
    git(clean_repo, "remote", "add", "origin", str(remote))
    push_auth = create_authorization(clean_env, clean_run, clean_repo, clean_branch, clean_head, "push-hook.txt", "feat: push hook")
    (clean_repo / "push-hook.txt").write_text("push hook\n", encoding="utf-8")
    post_hook = clean_repo / ".git" / "hooks" / "post-commit"
    post_hook.write_text("#!/bin/sh\ngit push origin HEAD\n", encoding="utf-8")
    post_hook.chmod(0o755)
    push_result = helper(clean_env, clean_run, push_auth["authorizations"][0]["authorization_id"], clean_repo)
    assert push_result["authorizations"][0]["status"] == "consumed"
    assert git(remote, "show-ref", ok=False).stdout == ""
    post_hook.unlink()
    command(["git", "-C", str(clean_repo), "read-tree", "HEAD"], env=os.environ.copy())

    # Plumbing bypasses every hook. Each malicious hook tries absolute Git ref
    # mutation and writes a marker; neither may run or alter any ref namespace.
    hook_head = git(clean_repo, "rev-parse", "HEAD").stdout.strip()
    hooks_dir = Path(td) / "malicious-hooks"
    hooks_dir.mkdir()
    markers = []
    for hook_name in ("pre-commit", "commit-msg", "post-commit"):
        marker = Path(td) / f"{hook_name}.ran"
        markers.append(marker)
        hook = hooks_dir / hook_name
        hook.write_text(
            f"#!/bin/sh\n/usr/bin/git -C '{clean_repo}' update-ref refs/heads/hook-mutated-{hook_name} HEAD\ntouch '{marker}'\n",
            encoding="utf-8",
        )
        hook.chmod(0o755)
    git(clean_repo, "config", "core.hooksPath", str(hooks_dir))
    for ref in ("refs/heads/other", "refs/tags/preserved", "refs/remotes/origin/preserved", "refs/stash", "refs/namespaces/test/refs/heads/preserved"):
        git(clean_repo, "update-ref", ref, hook_head)
    signing_marker = Path(td) / "signing.ran"
    signing_program = Path(td) / "fake-gpg"
    signing_program.write_text(f"#!/bin/sh\ntouch '{signing_marker}'\n", encoding="utf-8")
    signing_program.chmod(0o755)
    git(clean_repo, "config", "commit.gpgsign", "true")
    git(clean_repo, "config", "gpg.program", str(signing_program))
    fsmonitor_marker = Path(td) / "fsmonitor-marker.ran"
    fsmonitor_program = Path(td) / "fake-fsmonitor-marker"
    fsmonitor_program.write_text(f"#!/bin/sh\ntouch '{fsmonitor_marker}'\n", encoding="utf-8")
    fsmonitor_program.chmod(0o755)
    included_config = Path(td) / "included-git-config"
    included_config.write_text(f"[core]\n\tfsmonitor = {fsmonitor_program}\n", encoding="utf-8")
    git(clean_repo, "config", "include.path", str(included_config))
    filter_marker = Path(td) / "filter.ran"
    filter_program = Path(td) / "malicious-clean"
    filter_program.write_text(f"#!/bin/sh\ntouch '{filter_marker}'\ncat\n", encoding="utf-8")
    filter_program.chmod(0o755)
    attributes = Path(td) / "attributes"
    attributes.write_text("filtered.txt filter=malicious\n", encoding="utf-8")
    git(clean_repo, "config", "filter.malicious.clean", str(filter_program))
    git(clean_repo, "config", "filter.malicious.required", "true")
    git(clean_repo, "config", "core.attributesfile", str(attributes))
    hook_auth = create_authorization(clean_env, clean_run, clean_repo, clean_branch, hook_head, "hook-bypass.txt", "feat: hook bypass")
    (clean_repo / "hook-bypass.txt").write_text("hook bypass\n", encoding="utf-8")
    before_index = (clean_repo / ".git" / "index").read_bytes()
    before_refs = baseline(clean_repo, hook_head)["refs"]
    hook_result = helper(clean_env, clean_run, hook_auth["authorizations"][-1]["authorization_id"], clean_repo)
    assert hook_result["authorizations"][-1]["status"] == "consumed"
    assert all(not marker.exists() for marker in markers + [signing_marker, filter_marker, fsmonitor_marker])
    assert (clean_repo / ".git" / "index").read_bytes() == before_index
    after_refs = baseline(clean_repo, hook_head)["refs"]
    changed_refs = sorted(ref for ref in set(before_refs) | set(after_refs) if before_refs.get(ref) != after_refs.get(ref))
    assert changed_refs == [f"refs/heads/{clean_branch}"]
    hook_commit = hook_result["pending_operations"][-1]["receipt"]["commit_sha"]
    assert git(clean_repo, "show", f"{hook_commit}:hook-bypass.txt").stdout == "hook bypass\n"
    command(["git", "-C", str(clean_repo), "read-tree", "HEAD"], env=os.environ.copy())

    filter_head = git(clean_repo, "rev-parse", "HEAD").stdout.strip()
    fsmonitor_ref = "refs/heads/fsmonitor-mutated"
    fsmonitor_update = Path(td) / "fake-fsmonitor-update-ref"
    fsmonitor_update_marker = Path(td) / "fsmonitor-update-ref.ran"
    fsmonitor_update.write_text(
        f"#!/bin/sh\ntouch '{fsmonitor_update_marker}'\n/usr/bin/git -C '{clean_repo}' update-ref {fsmonitor_ref} HEAD\n",
        encoding="utf-8",
    )
    fsmonitor_update.chmod(0o755)
    included_config.write_text(f"[core]\n\tfsmonitor = {fsmonitor_update}\n", encoding="utf-8")
    filter_auth = create_authorization(clean_env, clean_run, clean_repo, clean_branch, filter_head, "filtered.txt", "feat: raw filter bypass")
    (clean_repo / "filtered.txt").write_text("raw content\n", encoding="utf-8")
    before_fsmonitor_refs = baseline(clean_repo, filter_head)["refs"]
    filter_result = helper(clean_env, clean_run, filter_auth["authorizations"][-1]["authorization_id"], clean_repo)
    assert filter_result["authorizations"][-1]["status"] == "consumed"
    assert not filter_marker.exists() and not signing_marker.exists()
    assert not fsmonitor_update_marker.exists() and fsmonitor_ref not in baseline(clean_repo)["refs"]
    after_fsmonitor_refs = baseline(clean_repo, filter_head)["refs"]
    changed_fsmonitor_refs = sorted(ref for ref in set(before_fsmonitor_refs) | set(after_fsmonitor_refs) if before_fsmonitor_refs.get(ref) != after_fsmonitor_refs.get(ref))
    assert changed_fsmonitor_refs == [f"refs/heads/{clean_branch}"]
    assert git(clean_repo, "show", f"{filter_result['pending_operations'][-1]['receipt']['commit_sha']}:filtered.txt").stdout == "raw content\n"
    command(["git", "-C", str(clean_repo), "read-tree", "HEAD"], env=os.environ.copy())

    escape_head = git(clean_repo, "rev-parse", "HEAD").stdout.strip()
    outside_target = Path(td) / "outside-target"
    outside_target.write_text("outside\n", encoding="utf-8")
    escape_auth = create_authorization(clean_env, clean_run, clean_repo, clean_branch, escape_head, "escape-link", "feat: escape")
    (clean_repo / "escape-link").symlink_to(outside_target)
    helper(clean_env, clean_run, escape_auth["authorizations"][-1]["authorization_id"], clean_repo, ok=False)
    (clean_repo / "escape-link").unlink()

    special_auth = create_authorization(clean_env, clean_run, clean_repo, clean_branch, escape_head, "special-node", "feat: special")
    os.mkfifo(clean_repo / "special-node")
    helper(clean_env, clean_run, special_auth["authorizations"][-1]["authorization_id"], clean_repo, ok=False)
    (clean_repo / "special-node").unlink()

    ita_head = git(clean_repo, "rev-parse", "HEAD").stdout.strip()
    ita_auth = create_authorization(clean_env, clean_run, clean_repo, clean_branch, ita_head, "intent-to-add.txt", "feat: ita")
    (clean_repo / "intent-to-add.txt").write_text("ita\n", encoding="utf-8")
    git(clean_repo, "add", "-N", "--", "intent-to-add.txt")
    helper(clean_env, clean_run, ita_auth["authorizations"][-1]["authorization_id"], clean_repo, ok=False)
    assert git(clean_repo, "rev-parse", "HEAD").stdout.strip() == ita_head
    git(clean_repo, "restore", "--staged", "--", "intent-to-add.txt")

    protected_auth = create_authorization(clean_env, clean_run, clean_repo, clean_branch, ita_head, "protected-check.txt", "feat: protected")
    (clean_repo / ".playwright-mcp").mkdir()
    (clean_repo / ".playwright-mcp" / "dirty.txt").write_text("protected\n", encoding="utf-8")
    helper(clean_env, clean_run, protected_auth["authorizations"][-1]["authorization_id"], clean_repo, ok=False)
    assert not (clean_repo / ".playwright-mcp" / "dirty.txt").read_text(encoding="utf-8").endswith("committed\n")
    (clean_repo / ".playwright-mcp" / "dirty.txt").unlink()
    (clean_repo / ".playwright-mcp").rmdir()

    forged_head = git(clean_repo, "rev-parse", "HEAD").stdout.strip()
    forged_auth = create_authorization(clean_env, clean_run, clean_repo, clean_branch, forged_head, "forged.txt", "feat: forged")
    forged_item = forged_auth["authorizations"][-1]
    forged_intent = {key: forged_item[key] for key in (
        "authorization_id", "authority", "request_id", "repository_root", "branch", "expected_head",
        "allowed_paths", "scope_digest", "commit_message", "commit_message_digest",
    )}
    forged_intent["pre_effect"] = baseline(clean_repo, forged_head)
    wrong_identity = dict(forged_intent, request_id="different-request")
    identity_result = command([
        str(STATE), "operation", clean_run, "intent", "--kind", "local_commit", "--idempotency-key",
        f"local-commit:{forged_item['authorization_id']}:{forged_item['authority']}:{forged_item['request_id']}",
        "--authorization-id", forged_item["authorization_id"], "--intent-json", json.dumps(wrong_identity),
        "--expect-revision", str(forged_auth["revision"]),
    ], env=clean_env, ok=False)
    assert "IDENTITY_MISMATCH" in identity_result.stderr
    forged_op = state(
        clean_env, "operation", clean_run, "intent", "--kind", "local_commit", "--idempotency-key",
        f"local-commit:{forged_item['authorization_id']}:{forged_item['authority']}:{forged_item['request_id']}",
        "--authorization-id", forged_item["authorization_id"], "--intent-json", json.dumps(forged_intent),
        "--expect-revision", str(forged_auth["revision"]),
    )
    fake_sha = "f" * 40
    forged_effect = {"authorization_id": forged_item["authorization_id"], "authority": forged_item["authority"], "request_id": forged_item["request_id"], "commit_sha": fake_sha, "tree_sha": fake_sha, "parent_sha": forged_head, "branch": clean_branch, "paths": ["forged.txt"]}
    forged_receipt = {"status": "SUCCESS", **forged_effect, "message": "feat: forged"}
    observed = state(clean_env, "operation", clean_run, "observe", "--operation-id", forged_op["pending_operations"][-1]["operation_id"], "--effect-json", json.dumps(forged_effect), "--expect-revision", str(forged_op["revision"]))
    recorded = state(clean_env, "operation", clean_run, "receipt", "--operation-id", forged_op["pending_operations"][-1]["operation_id"], "--receipt-json", json.dumps(forged_receipt), "--expect-revision", str(observed["revision"]))
    confirmed = state(clean_env, "operation", clean_run, "confirm", "--operation-id", forged_op["pending_operations"][-1]["operation_id"], "--status", "confirmed", "--reason", "forged test", "--expect-revision", str(recorded["revision"]))
    forged_consume = command([
        str(STATE), "authorization", clean_run, "consume", "--authorization-id", forged_item["authorization_id"],
        "--operation-id", forged_op["pending_operations"][-1]["operation_id"], "--commit-sha", fake_sha,
        "--expect-revision", str(confirmed["revision"]),
    ], env=clean_env, ok=False)
    assert "COMMIT_NOT_FOUND" in forged_consume.stderr or "RECEIPT_MISMATCH" in forged_consume.stderr
    state_path = Path(clean_env["MYRMEX_STATE_HOME"]) / "runs" / clean_run / "state.json"
    tampered_state = json.loads(state_path.read_text(encoding="utf-8"))
    tampered_state["pending_operations"][-1]["intent"]["request_id"] = "incompatible-request"
    state_path.write_text(json.dumps(tampered_state), encoding="utf-8")
    incompatible_result = command([
        str(HELPER), "commit", "--run-id", clean_run, "--authorization-id", forged_item["authorization_id"],
        "--repository-root", str(clean_repo),
    ], env=clean_env, ok=False)
    assert "LOCAL_COMMIT_OPERATION_IDENTITY_MISMATCH" in incompatible_result.stderr
    assert "Traceback" not in incompatible_result.stderr

print("local commit authorization tests: PASS")
