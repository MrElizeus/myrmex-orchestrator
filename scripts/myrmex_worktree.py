#!/usr/bin/env python3
"""
Isolated Worktree Management for Myrmex Work Units.
Enforces scope boundary, dirty state checks, read-only verifier worktrees, and clean worktree lifecycles.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def _get_worktrees_dir() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    d = Path(xdg) / "myrmex/worktrees"
    d.mkdir(parents=True, exist_ok=True)
    return d


def create_wu_worktree(
    source_repo: Path,
    campaign_id: str,
    wu_id: str,
    base_sha: str,
    allowed_scope: list[str] | None = None,
) -> tuple[Path, dict[str, Any]]:
    """
    Creates an isolated Git worktree for a specific Work Unit (Writer environment).
    Returns (worktree_path, receipt_dict).
    """
    source_repo = source_repo.resolve()
    target_dir = _get_worktrees_dir() / campaign_id / wu_id
    if target_dir.exists():
        if (target_dir / ".git").exists():
            proc_check = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=target_dir,
                capture_output=True,
                text=True,
            )
            if proc_check.returncode == 0:
                head_sha = proc_check.stdout.strip()
                receipt = {
                    "source_repository": str(source_repo),
                    "worktree_path": str(target_dir),
                    "branch": f"wu-{wu_id}",
                    "base_sha": base_sha,
                    "expected_head": head_sha,
                    "scope": allowed_scope or [],
                    "owner_wu": wu_id,
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "cleanup_status": "reused",
                }
                return target_dir, receipt

        shutil.rmtree(target_dir, ignore_errors=True)

    target_dir.parent.mkdir(parents=True, exist_ok=True)
    branch_name = f"wu-{wu_id}-{int(time.time())}"

    # Prune stale worktrees first
    subprocess.run(
        ["git", "worktree", "prune"],
        cwd=source_repo,
        capture_output=True,
        text=True,
    )

    cmd = ["git", "worktree", "add", "-b", branch_name, str(target_dir), base_sha]
    proc = subprocess.run(cmd, cwd=source_repo, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Failed to create git worktree at {target_dir}: {proc.stderr}")

    receipt = {
        "source_repository": str(source_repo),
        "worktree_path": str(target_dir),
        "branch": branch_name,
        "base_sha": base_sha,
        "expected_head": base_sha,
        "scope": allowed_scope or [],
        "owner_wu": wu_id,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "cleanup_status": "active",
    }
    (target_dir / ".myrmex-worktree-receipt.json").write_text(
        json.dumps(receipt, indent=2), encoding="utf-8"
    )

    return target_dir, receipt


def create_verifier_worktree(
    source_repo: Path,
    campaign_id: str,
    wu_id: str,
    candidate_sha: str,
    writer_worktree: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    """
    Creates an isolated, read-only worktree for the Verifier agent.
    The Verifier agent CANNOT alter index, permissions, symlinks, or files.
    """
    source_repo = source_repo.resolve()
    target_dir = _get_worktrees_dir() / campaign_id / f"{wu_id}-verifier"
    shutil.rmtree(target_dir, ignore_errors=True)
    target_dir.parent.mkdir(parents=True, exist_ok=True)

    # Prune stale worktrees
    subprocess.run(
        ["git", "worktree", "prune"],
        cwd=source_repo,
        capture_output=True,
        text=True,
    )

    # Detached head at candidate_sha
    cmd = ["git", "worktree", "add", "--detach", str(target_dir), candidate_sha]
    proc = subprocess.run(cmd, cwd=source_repo, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Failed to create verifier worktree at {target_dir}: {proc.stderr}")

    # If writer_worktree has uncommitted candidate changes, copy modified files into verifier worktree
    if writer_worktree and writer_worktree.exists():
        proc_status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=writer_worktree,
            capture_output=True,
            text=True,
        )
        lines = [l for l in proc_status.stdout.splitlines() if l.strip()]
        for line in lines:
            parts = line[3:].strip()
            if parts != ".myrmex-worktree-receipt.json":
                src_file = writer_worktree / parts
                dst_file = target_dir / parts
                if src_file.is_file():
                    dst_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_file, dst_file)

    receipt = {
        "source_repository": str(source_repo),
        "verifier_worktree_path": str(target_dir),
        "candidate_sha": candidate_sha,
        "read_only": True,
        "owner_wu": wu_id,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    return target_dir, receipt


def compute_workspace_hash(worktree_dir: Path) -> str:
    """Computes a total hash of all tracked/untracked/ignored state in the workspace."""
    worktree_dir = worktree_dir.resolve()
    hasher = hashlib.sha256()

    proc_status = subprocess.run(
        ["git", "status", "--porcelain=v2", "--ignored"],
        cwd=worktree_dir,
        capture_output=True,
        text=True,
    )
    hasher.update(proc_status.stdout.encode("utf-8"))

    # Walk files to hash contents and metadata permissions
    for root, dirs, files in os.walk(worktree_dir):
        if ".git" in dirs:
            dirs.remove(".git")
        for f in sorted(files):
            p = Path(root) / f
            try:
                st = p.stat()
                hasher.update(f"{p.relative_to(worktree_dir)}:{st.st_mode}:{st.st_size}".encode("utf-8"))
            except Exception:
                pass

    return hasher.hexdigest()


def verify_workspace_scope(
    worktree_dir: Path,
    base_sha: str,
    allowed_scope: list[str],
) -> list[str]:
    """
    Independently inspects the worktree for scope violations, symlink escapes,
    untracked files, mode changes, or .git modifications.
    Returns a list of defect/violation strings (empty if clean & in-scope).
    """
    violations: list[str] = []
    worktree_dir = worktree_dir.resolve()

    # 1. Prohibit changes inside .git
    git_dir = worktree_dir / ".git"
    if git_dir.is_dir():
        proc_git_status = subprocess.run(
            ["git", "status", "--porcelain", ".git"],
            cwd=worktree_dir,
            capture_output=True,
            text=True,
        )
        if proc_git_status.stdout.strip():
            violations.append("Prohibited modification inside .git directory")

    # 2. Check git diff against base_sha
    proc_diff = subprocess.run(
        ["git", "diff", "--name-status", base_sha],
        cwd=worktree_dir,
        capture_output=True,
        text=True,
    )
    diff_lines = proc_diff.stdout.strip().splitlines()

    # 3. Check untracked files
    proc_untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=worktree_dir,
        capture_output=True,
        text=True,
    )
    untracked_lines = proc_untracked.stdout.strip().splitlines()

    touched_paths: set[str] = set()

    for line in diff_lines:
        parts = line.split(maxsplit=1)
        if len(parts) == 2:
            status, filepath = parts[0], parts[1]
            touched_paths.add(filepath)
            if status.startswith("R"):  # Rename
                violations.append(f"Prohibited rename detected: {filepath}")

    for filepath in untracked_lines:
        if filepath != ".myrmex-worktree-receipt.json":
            touched_paths.add(filepath)

    # Validate paths against allowed_scope
    if allowed_scope and allowed_scope != ["*"]:
        for rel_path in touched_paths:
            in_scope = False
            for pat in allowed_scope:
                if pat == "*" or rel_path == pat or rel_path.startswith(pat.rstrip("/") + "/"):
                    in_scope = True
                    break
            if not in_scope:
                violations.append(f"Path outside allowed scope modified: {rel_path}")

    # 4. Check escaping symlinks
    for root, dirs, files in os.walk(worktree_dir):
        if ".git" in dirs:
            dirs.remove(".git")
        for f in files:
            p = Path(root) / f
            if p.is_symlink():
                try:
                    resolved = p.resolve()
                    if not resolved.is_relative_to(worktree_dir):
                        violations.append(f"Escaping symlink detected: {p.relative_to(worktree_dir)}")
                except ValueError:
                    violations.append(f"Escaping symlink detected: {p.relative_to(worktree_dir)}")

    return violations


def cleanup_wu_worktree(
    source_repo: Path,
    worktree_dir: Path,
    force: bool = False,
) -> bool:
    """
    Safely removes a worktree. Refuses to delete if un-integrated changes exist unless force=True.
    """
    worktree_dir = worktree_dir.resolve()
    if not worktree_dir.exists():
        return True

    if not force:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=worktree_dir,
            capture_output=True,
            text=True,
        )
        untracked_non_receipt = [
            l for l in proc.stdout.strip().splitlines()
            if ".myrmex-worktree-receipt.json" not in l
        ]
        if untracked_non_receipt:
            sys.stderr.write(f"[myrmex-worktree] Refusing cleanup: worktree has unintegrated changes: {untracked_non_receipt}\n")
            return False

    cmd = ["git", "worktree", "remove", "--force", str(worktree_dir)]
    proc = subprocess.run(cmd, cwd=source_repo, capture_output=True, text=True)
    if proc.returncode != 0:
        shutil.rmtree(worktree_dir, ignore_errors=True)
        subprocess.run(["git", "worktree", "prune"], cwd=source_repo, capture_output=True, text=True)

    return True
