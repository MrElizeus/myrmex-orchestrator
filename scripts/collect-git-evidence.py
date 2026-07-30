#!/usr/bin/env python3
"""Collect deterministic Git evidence, including untracked files."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any


def run(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=check)


def line_count(data: bytes) -> int:
    if not data:
        return 0
    return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)


def untracked_paths(repo: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "--others", "--exclude-standard", "-z"],
        capture_output=True,
        check=True,
    )
    return [repo / item for item in result.stdout.decode("utf-8", errors="surrogateescape").split("\0") if item]


def untracked_evidence(repo: Path) -> tuple[list[str], int, list[dict[str, Any]], list[str]]:
    files: list[str] = []
    additions = 0
    details: list[dict[str, Any]] = []
    whitespace: list[str] = []
    for path in untracked_paths(repo):
        rel = path.relative_to(repo).as_posix()
        files.append(rel)
        if path.is_symlink():
            details.append({"path": rel, "kind": "symlink", "lines": 0, "sha256": None})
            continue
        if not path.is_file():
            details.append({"path": rel, "kind": "other", "lines": 0, "sha256": None})
            continue
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        binary = b"\0" in data
        lines = 0 if binary else line_count(data)
        additions += lines
        details.append({
            "path": rel,
            "kind": "binary" if binary else "text",
            "lines": lines,
            "sha256": digest,
        })
        if not binary:
            for number, raw in enumerate(data.splitlines(), 1):
                if raw.endswith((b" ", b"\t")):
                    whitespace.append(f"{rel}:{number}: trailing whitespace")
    return files, additions, details, whitespace


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--base-sha", default="")
    args = parser.parse_args()
    repo = Path(args.repo).expanduser().resolve()
    if not (repo / ".git").exists():
        raise SystemExit(f"not a git repository: {repo}")

    head = run(repo, "rev-parse", "HEAD").stdout.strip()
    branch = run(repo, "branch", "--show-current").stdout.strip()
    status = run(repo, "status", "--short").stdout.splitlines()
    diff_ref = args.base_sha or "HEAD"
    numstat = run(repo, "diff", "--numstat", diff_ref).stdout.splitlines()
    files: list[str] = []
    binary_files: list[str] = []
    additions = 0
    deletions = 0
    for line in numstat:
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        files.append(path)
        if added == "-" or deleted == "-":
            binary_files.append(path)
            continue
        additions += int(added)
        deletions += int(deleted)

    new_files, new_additions, untracked, untracked_whitespace = untracked_evidence(repo)
    files.extend(new_files)
    additions += new_additions
    files = sorted(set(files))
    binary_files = sorted(set(binary_files + [item["path"] for item in untracked if item["kind"] == "binary"]))
    check = run(repo, "diff", "--check", diff_ref, check=False)
    tracked_check = check.stdout + check.stderr
    whitespace = [line for line in tracked_check.splitlines() if line] + untracked_whitespace
    result: dict[str, Any] = {
        "schema": "myrmex.evidence-receipt/v1",
        "branch": branch,
        "head": head,
        "base_sha": args.base_sha or None,
        "files": files,
        "additions": additions,
        "deletions": deletions,
        "changed_lines": additions + deletions,
        "status": status,
        "binary_files": binary_files,
        "untracked_files": untracked,
        "diff_check": "pass" if not whitespace else "fail",
        "diff_check_output": "\n".join(whitespace) if whitespace else None,
    }
    print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
    return 0 if not whitespace else 1


if __name__ == "__main__":
    raise SystemExit(main())
