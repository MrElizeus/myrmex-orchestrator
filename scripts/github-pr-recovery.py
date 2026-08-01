#!/usr/bin/env python3
"""Create and label a draft GitHub PR without repeating successful side effects.

The helper deliberately separates discovery, creation, persistence, and label
application. It owns only its local artifact receipt: the caller must persist
the typed `pull_request` operation intent/receipt/confirmation in
`myrmex-state`. It never pushes a branch and it never prints command stderr,
which may contain environment-specific details.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


URL_RE = re.compile(r"https://github\.com/[^\s]+/pull/(\d+)")


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def gh(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["gh", *args], capture_output=True, text=True, timeout=45)


def find_pr(repo: str, head: str, base: str) -> dict[str, Any] | None:
    result = gh([
        "pr", "list", "--repo", repo, "--head", head, "--base", base,
        "--state", "open", "--limit", "100", "--json", "number,url,headRefName,baseRefName",
    ])
    if result.returncode != 0:
        return None
    try:
        rows = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(rows, list):
        return None
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("number"), int) and isinstance(row.get("url"), str):
            return {"number": row["number"], "url": row["url"]}
    return None


def persist(record: dict[str, Any], receipt: Path) -> None:
    atomic_json(receipt, record)


def result_record(status: str, repo: str, head: str, base: str, pr: dict[str, Any] | None, label: str | None) -> dict[str, Any]:
    return {
        "status": status,
        "repo": repo,
        "head": head,
        "base": base,
        "number": pr.get("number") if pr else None,
        "url": pr.get("url") if pr else None,
        "label": label,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--base", default="main")
    parser.add_argument("--title", required=True)
    parser.add_argument("--body-file", required=True)
    parser.add_argument("--label")
    parser.add_argument("--receipt-file", required=True)
    # Retain the old flags only long enough to fail safely before any GitHub
    # effect.  A blind `patch` had no optimistic revision and could overwrite
    # typed state written by a concurrent recovery process.
    parser.add_argument("--state-bin", help=argparse.SUPPRESS)
    parser.add_argument("--state-run", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.state_bin or args.state_run:
        raise SystemExit(
            "--state-bin/--state-run are retired: persist a typed pull_request operation "
            "with myrmex-state before and after github-pr-recovery.py"
        )

    receipt = Path(args.receipt_file).expanduser().resolve()
    pr = find_pr(args.repo, args.head, args.base)
    created_now = False
    if pr is None:
        create = gh([
            "pr", "create", "--repo", args.repo, "--draft", "--base", args.base, "--head", args.head,
            "--title", args.title, "--body-file", args.body_file,
        ])
        match = URL_RE.search(create.stdout)
        if match:
            pr = {"number": int(match.group(1)), "url": match.group(0)}
            created_now = True
        else:
            # gh can return non-zero after the PR side effect; query before retrying.
            pr = find_pr(args.repo, args.head, args.base)
            if pr is None:
                record = result_record("PR_CREATION_FAILED", args.repo, args.head, args.base, None, args.label)
                persist(record, receipt)
                print(json.dumps(record, indent=2, sort_keys=True))
                return 1

    record = result_record("PR_CREATED_LABEL_PENDING", args.repo, args.head, args.base, pr, args.label)
    record["created_now"] = created_now
    persist(record, receipt)
    if not args.label:
        record["status"] = "PR_CREATED"
        persist(record, receipt)
        print(json.dumps(record, indent=2, sort_keys=True))
        return 0

    edit = gh(["pr", "edit", str(pr["number"]), "--repo", args.repo, "--add-label", args.label])
    if edit.returncode == 0:
        record["status"] = "PR_CREATED"
        record["label_method"] = "gh-pr-edit"
        persist(record, receipt)
        print(json.dumps(record, indent=2, sort_keys=True))
        return 0

    # Confirm the PR still exists before the narrow REST fallback. Do not recreate it.
    confirmed = find_pr(args.repo, args.head, args.base)
    if confirmed is None:
        record["status"] = "LABEL_APPLICATION_FAILED"
        record["label_method"] = "pr-not-found-after-edit-error"
        persist(record, receipt)
        print(json.dumps(record, indent=2, sort_keys=True))
        return 1
    pr = confirmed
    record.update({"number": pr["number"], "url": pr["url"]})
    fallback = gh([
        "api", "--method", "POST", f"/repos/{args.repo}/issues/{pr['number']}/labels",
        "-f", f"labels[]={args.label}",
    ])
    if fallback.returncode != 0:
        record["status"] = "LABEL_APPLICATION_FAILED"
        record["label_method"] = "rest-fallback-failed"
        persist(record, receipt)
        print(json.dumps(record, indent=2, sort_keys=True))
        return 1
    record["status"] = "PR_CREATED"
    record["label_method"] = "rest-issue-label-fallback"
    persist(record, receipt)
    print(json.dumps(record, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
