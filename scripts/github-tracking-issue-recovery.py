#!/usr/bin/env python3
"""Create or recover a Myrmex tracking issue without duplicating side effects.

The helper owns only the GitHub effect and a local artifact receipt. A caller
must persist the intent/receipt in `myrmex-state` through the typed operation
ledger before treating delivery bookkeeping as complete.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any


URL_RE = re.compile(r"https://github\.com/[^\s]+/issues/(\d+)")
TRACKING_MARKER_RE = re.compile(r"<!--\s*myrmex:tracking\b.*?-->", re.I | re.S)


class DiscoveryError(RuntimeError):
    """The remote issue set could not be read safely."""


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
    try:
        return subprocess.run(["gh", *args], capture_output=True, text=True, timeout=45)
    except (OSError, subprocess.TimeoutExpired):
        # Callers must recover by discovery rather than assume that a timed-out
        # command did not perform its remote side effect.
        return subprocess.CompletedProcess(["gh", *args], 1, "", "")


def marker(objective_id: str, scope_digest: str) -> str:
    return f"<!-- myrmex:tracking objective_id={objective_id} scope_digest={scope_digest} -->"


def list_issues(repo: str, search: str) -> list[dict[str, Any]]:
    result = gh([
        "issue", "list", "--repo", repo, "--state", "all", "--limit", "100",
        "--search", search, "--json", "number,url,title,body,state,labels",
    ])
    if result.returncode != 0:
        raise DiscoveryError("could not query tracking issues")
    try:
        rows = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise DiscoveryError("tracking issue query returned invalid JSON") from exc
    if not isinstance(rows, list):
        raise DiscoveryError("tracking issue query did not return an array")
    issues: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if not isinstance(row.get("number"), int) or not isinstance(row.get("url"), str):
            continue
        labels = row.get("labels")
        names = [label.get("name") for label in labels if isinstance(label, dict) and isinstance(label.get("name"), str)] if isinstance(labels, list) else []
        issues.append({
            "number": row["number"], "url": row["url"], "title": row.get("title"),
            "body": str(row.get("body") or ""), "state": row.get("state"), "labels": names,
        })
    return issues


def list_exact(repo: str, identity: str) -> list[dict[str, Any]]:
    return [issue for issue in list_issues(repo, identity) if identity in issue["body"]]


def normalized_scope_body(value: str) -> str:
    """Compare authorized issue scope without weakening it into title similarity."""
    return "\n".join(line.rstrip() for line in value.replace("\r\n", "\n").split("\n")).strip()


def fallback_scope_matches(candidate_body: str, authorized_body: str, scope_digest: str) -> bool:
    """Recognize only strong pre-marker identity evidence.

    An issue with any other Myrmex marker belongs to a different explicit
    identity and must never be adopted through this legacy fallback.  A plain
    historic issue is eligible only when it contains the exact scope digest or
    its normalized authorized scope is byte-for-byte equivalent.
    """
    if TRACKING_MARKER_RE.search(candidate_body):
        return False
    return scope_digest in candidate_body or normalized_scope_body(candidate_body) == normalized_scope_body(authorized_body)


def list_approved_fallback(
    repo: str, title: str, authorized_body: str, scope_digest: str, approval_marker: str,
) -> list[dict[str, Any]]:
    """Find pre-marker issues only when title, scope, and approval all agree."""
    candidates = list_issues(repo, f'in:title "{title}"')
    return [
        issue for issue in candidates
        if issue.get("state") == "OPEN"
        and issue.get("title") == title
        and approval_marker in issue.get("labels", [])
        and fallback_scope_matches(str(issue.get("body") or ""), authorized_body, scope_digest)
    ]


def approval_marker_available(repo: str, approval_marker: str) -> bool:
    """Check the repository label vocabulary before creating a doomed issue."""
    result = gh(["label", "list", "--repo", repo, "--limit", "100", "--json", "name"])
    if result.returncode != 0:
        raise DiscoveryError("could not resolve the repository approval marker")
    try:
        labels = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise DiscoveryError("approval marker query returned invalid JSON") from exc
    if not isinstance(labels, list):
        raise DiscoveryError("approval marker query did not return an array")
    return any(isinstance(label, dict) and label.get("name") == approval_marker for label in labels)


def issue_record(status: str, repo: str, identity: str, issue: dict[str, Any] | None, approval_marker: str) -> dict[str, Any]:
    return {
        "status": status,
        "repo": repo,
        "number": issue.get("number") if issue else None,
        "url": issue.get("url") if issue else None,
        "source": issue.get("source") if issue else None,
        "identity_marker": identity,
        "approval_marker": approval_marker,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--body-file", required=True)
    parser.add_argument("--receipt-file", required=True)
    parser.add_argument("--objective-id", required=True)
    parser.add_argument("--scope-digest", required=True)
    parser.add_argument("--approval-marker", default="status:approved")
    parser.add_argument("--creation-policy", choices=["authorized", "ask", "deny"], default="ask")
    parser.add_argument("--ensure-approval", action="store_true")
    args = parser.parse_args()

    if not re.fullmatch(r"[0-9a-f]{64}", args.scope_digest):
        raise SystemExit("--scope-digest must be a lowercase SHA-256 digest")
    body_path = Path(args.body_file).expanduser().resolve()
    receipt_path = Path(args.receipt_file).expanduser().resolve()
    authorized_body = body_path.read_text(encoding="utf-8")
    body = authorized_body
    identity = marker(args.objective_id, args.scope_digest)
    temporary_body: Path | None = None
    if identity not in body:
        body = body.rstrip() + "\n\n" + identity + "\n"
        # Use a private temporary body so callers' authorized text is never
        # mutated merely to add the deterministic recovery marker.
        fd, temporary = tempfile.mkstemp(prefix="myrmex-issue-body.", suffix=".md")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(body)
            temporary_body = Path(temporary)
            body_path = temporary_body
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    try:
        try:
            matches = list_exact(args.repo, identity)
        except DiscoveryError:
            record = issue_record("ISSUE_DISCOVERY_FAILED", args.repo, identity, None, args.approval_marker)
            atomic_json(receipt_path, record)
            print(json.dumps(record, indent=2, sort_keys=True))
            return 1
        open_matches = [item for item in matches if item.get("state") == "OPEN"]
        if len(open_matches) > 1 or (not open_matches and matches):
            record = issue_record("ISSUE_AMBIGUOUS", args.repo, identity, None, args.approval_marker)
            record["candidate_numbers"] = [item["number"] for item in matches]
            atomic_json(receipt_path, record)
            print(json.dumps(record, indent=2, sort_keys=True))
            return 1

        issue: dict[str, Any] | None = open_matches[0] if open_matches else None
        if issue is None:
            # Pre-marker issues are deliberately not reused by title alone.
            # Only an already-approved, open issue with the exact title and
            # strong scope evidence can be adopted without inventing a new
            # tracking record.
            try:
                fallback_matches = list_approved_fallback(
                    args.repo, args.title, authorized_body, args.scope_digest, args.approval_marker,
                )
            except DiscoveryError:
                record = issue_record("ISSUE_DISCOVERY_FAILED", args.repo, identity, None, args.approval_marker)
                atomic_json(receipt_path, record)
                print(json.dumps(record, indent=2, sort_keys=True))
                return 1
            if len(fallback_matches) > 1:
                record = issue_record("ISSUE_AMBIGUOUS", args.repo, identity, None, args.approval_marker)
                record["candidate_numbers"] = [item["number"] for item in fallback_matches]
                record["ambiguity"] = "multiple approved pre-marker issues match the exact title and scope"
                atomic_json(receipt_path, record)
                print(json.dumps(record, indent=2, sort_keys=True))
                return 1
            if len(fallback_matches) == 1:
                issue = dict(fallback_matches[0])
                issue["source"] = "reused-approved-fallback"
        if issue is None:
            if args.creation_policy != "authorized":
                status = "ISSUE_CREATION_FORBIDDEN" if args.creation_policy == "deny" else "HUMAN_DECISION_REQUIRED"
                record = issue_record(status, args.repo, identity, None, args.approval_marker)
                atomic_json(receipt_path, record)
                print(json.dumps(record, indent=2, sort_keys=True))
                return 1
            # A missing repository label is a policy/configuration decision,
            # not a reason to create an issue that can never satisfy the
            # delivery gate.  Check it before the create side effect.
            try:
                marker_is_available = approval_marker_available(args.repo, args.approval_marker)
            except DiscoveryError as exc:
                marker_is_available = False
                marker_problem = str(exc)
            else:
                marker_problem = None
            if not marker_is_available:
                record = issue_record("ISSUE_APPROVAL_MARKER_UNAVAILABLE", args.repo, identity, None, args.approval_marker)
                record["blocker"] = "APPROVAL_MARKER_UNAVAILABLE"
                record["recovery"] = "configure the canonical approval marker or authorize a different documented marker"
                if marker_problem:
                    record["detail"] = marker_problem
                atomic_json(receipt_path, record)
                print(json.dumps(record, indent=2, sort_keys=True))
                return 1
            created = gh(["issue", "create", "--repo", args.repo, "--title", args.title, "--body-file", str(body_path)])
            match = URL_RE.search(created.stdout)
            if match:
                issue = {"number": int(match.group(1)), "url": match.group(0), "labels": [], "state": "OPEN", "source": "created"}
            else:
                # `gh` can fail after the remote effect. Recover by the exact
                # stable marker before reporting failure or attempting no retry.
                try:
                    recovered = [item for item in list_exact(args.repo, identity) if item.get("state") == "OPEN"]
                except DiscoveryError:
                    record = issue_record("ISSUE_CREATION_UNCONFIRMED", args.repo, identity, None, args.approval_marker)
                    atomic_json(receipt_path, record)
                    print(json.dumps(record, indent=2, sort_keys=True))
                    return 1
                if len(recovered) == 1:
                    issue = dict(recovered[0])
                    issue["source"] = "created-recovered"
                else:
                    record = issue_record("ISSUE_CREATION_FAILED", args.repo, identity, None, args.approval_marker)
                    atomic_json(receipt_path, record)
                    print(json.dumps(record, indent=2, sort_keys=True))
                    return 1
        else:
            issue = dict(issue)
            issue.setdefault("source", "reused")

        approved = args.approval_marker in issue.get("labels", [])
        reused = str(issue["source"]).startswith("reused")
        pending_status = "ISSUE_REUSED_APPROVAL_PENDING" if reused else "ISSUE_CREATED_APPROVAL_PENDING"
        # Identity is a durable checkpoint before any approval mutation. This
        # remains true when callers select the convenience ensure-approval
        # path, so a crash between create and label is recoverable.
        if not approved:
            atomic_json(receipt_path, issue_record(pending_status, args.repo, identity, issue, args.approval_marker))
        if not args.ensure_approval:
            status = "ISSUE_REUSED" if reused and approved else (
                pending_status
            )
            record = issue_record(status, args.repo, identity, issue, args.approval_marker)
            atomic_json(receipt_path, record)
            print(json.dumps(record, indent=2, sort_keys=True))
            return 0

        if not approved:
            # An existing deterministic issue can be resumed later, but only
            # attempt its approval mutation when the configured marker is
            # actually available in the repository vocabulary.
            try:
                marker_is_available = approval_marker_available(args.repo, args.approval_marker)
            except DiscoveryError as exc:
                marker_is_available = False
                marker_problem = str(exc)
            else:
                marker_problem = None
            if not marker_is_available:
                record = issue_record("ISSUE_APPROVAL_MARKER_UNAVAILABLE", args.repo, identity, issue, args.approval_marker)
                record["blocker"] = "APPROVAL_MARKER_UNAVAILABLE"
                record["recovery"] = "configure the canonical approval marker or authorize a different documented marker"
                if marker_problem:
                    record["detail"] = marker_problem
                atomic_json(receipt_path, record)
                print(json.dumps(record, indent=2, sort_keys=True))
                return 1
            edit = gh(["issue", "edit", str(issue["number"]), "--repo", args.repo, "--add-label", args.approval_marker])
            if edit.returncode != 0:
                fallback = gh([
                    "api", "--method", "POST", f"/repos/{args.repo}/issues/{issue['number']}/labels",
                    "-f", f"labels[]={args.approval_marker}",
                ])
                if fallback.returncode != 0:
                    record = issue_record("ISSUE_APPROVAL_FAILED", args.repo, identity, issue, args.approval_marker)
                    atomic_json(receipt_path, record)
                    print(json.dumps(record, indent=2, sort_keys=True))
                    return 1
            try:
                refreshed = [item for item in list_exact(args.repo, identity) if item.get("number") == issue["number"]]
            except DiscoveryError:
                record = issue_record("ISSUE_APPROVAL_UNCONFIRMED", args.repo, identity, issue, args.approval_marker)
                atomic_json(receipt_path, record)
                print(json.dumps(record, indent=2, sort_keys=True))
                return 1
            if len(refreshed) != 1 or args.approval_marker not in refreshed[0].get("labels", []):
                record = issue_record("ISSUE_APPROVAL_FAILED", args.repo, identity, issue, args.approval_marker)
                atomic_json(receipt_path, record)
                print(json.dumps(record, indent=2, sort_keys=True))
                return 1
            issue.update(refreshed[0])

        record = issue_record("ISSUE_APPROVED", args.repo, identity, issue, args.approval_marker)
        atomic_json(receipt_path, record)
        print(json.dumps(record, indent=2, sort_keys=True))
        return 0
    finally:
        # Only remove the private temporary body created above.
        if temporary_body is not None:
            try:
                temporary_body.unlink()
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
