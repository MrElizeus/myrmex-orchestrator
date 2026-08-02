#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "github-tracking-issue-recovery.py"
OBJECTIVE_ID = "objective-controlled-alpha"
SCOPE_DIGEST = "a" * 64
APPROVAL_MARKER = "status:approved"
IDENTITY = f"<!-- myrmex:tracking objective_id={OBJECTIVE_ID} scope_digest={SCOPE_DIGEST} -->"


FAKE_GH = r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

state_path = Path(os.environ["FAKE_GH_STATE"])
log_path = Path(os.environ["FAKE_GH_LOG"])
receipt_path = Path(os.environ["FAKE_GH_RECEIPT"])
args = sys.argv[1:]
state = json.loads(state_path.read_text())

entry = {"args": args}
if args[:2] == ["issue", "edit"] and receipt_path.is_file():
    entry["receipt_before_edit"] = json.loads(receipt_path.read_text())
with log_path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(entry, sort_keys=True) + "\n")

def save():
    state_path.write_text(json.dumps(state, sort_keys=True))

def issue_by_number(number):
    for issue in state["issues"]:
        if issue["number"] == number:
            return issue
    raise SystemExit("unknown issue: " + str(number))

def label_from_args():
    for value in args:
        if value.startswith("labels[]="):
            return value.split("=", 1)[1]
    if "--add-label" in args:
        return args[args.index("--add-label") + 1]
    raise SystemExit("missing label")

def add_label(issue, label):
    if label not in [item.get("name") for item in issue["labels"] if isinstance(item, dict)]:
        issue["labels"].append({"name": label})

if args[:2] == ["issue", "list"]:
    if state.get("list_fail"):
        print("simulated discovery failure", file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(state["issues"]))
elif args[:2] == ["label", "list"]:
    if state.get("label_list_fail"):
        print("simulated label vocabulary failure", file=sys.stderr)
        raise SystemExit(1)
    names = state.get("available_labels", ["status:approved"])
    print(json.dumps([{"name": name} for name in names]))
elif args[:2] == ["issue", "create"]:
    body_path = Path(args[args.index("--body-file") + 1])
    number = int(state.get("next_number", 1))
    issue = {
        "number": number,
        "url": f"https://github.com/acme/myrmex/issues/{number}",
        "title": args[args.index("--title") + 1],
        "body": body_path.read_text(),
        "state": "OPEN",
        "labels": [],
    }
    state["issues"].append(issue)
    state["next_number"] = number + 1
    save()
    if state.get("create_after_effect_error"):
        print("simulated post-create failure", file=sys.stderr)
        raise SystemExit(1)
    print(issue["url"])
elif args[:2] == ["issue", "edit"]:
    if state.get("edit_fail"):
        print("simulated label failure", file=sys.stderr)
        raise SystemExit(1)
    issue = issue_by_number(int(args[2]))
    label = label_from_args()
    add_label(issue, label)
    save()
elif args[:1] == ["api"]:
    if state.get("fallback_fail"):
        print("simulated REST label failure", file=sys.stderr)
        raise SystemExit(1)
    endpoint = next(value for value in args if value.startswith("/repos/"))
    issue = issue_by_number(int(endpoint.split("/issues/", 1)[1].split("/", 1)[0]))
    label = label_from_args()
    add_label(issue, label)
    save()
    print("[]")
else:
    raise SystemExit("unexpected gh invocation: " + repr(args))
'''


def issue(number: int, *, labels: list[str] | None = None, state: str = "OPEN") -> dict[str, Any]:
    return {
        "number": number,
        "url": f"https://github.com/acme/myrmex/issues/{number}",
        "title": "tracking issue",
        "body": f"Known objective\n\n{IDENTITY}\n",
        "state": state,
        "labels": [{"name": label} for label in labels or []],
    }


def setup_case(root: Path, name: str, state: dict[str, Any]) -> tuple[dict[str, str], Path, Path, Path]:
    case = root / name
    case.mkdir()
    fake = case / "gh"
    fake.write_text(FAKE_GH)
    fake.chmod(0o755)
    state_path = case / "state.json"
    state_path.write_text(json.dumps(state))
    log_path = case / "gh.log"
    receipt_path = case / "receipt.json"
    body_path = case / "body.md"
    body_path.write_text("Authorized tracking scope only.\n")
    env = dict(
        os.environ,
        PATH=f"{case}{os.pathsep}{os.environ['PATH']}",
        FAKE_GH_STATE=str(state_path),
        FAKE_GH_LOG=str(log_path),
        FAKE_GH_RECEIPT=str(receipt_path),
    )
    return env, state_path, log_path, receipt_path


def invoke(
    env: dict[str, str],
    receipt_path: Path,
    body_path: Path,
    *,
    creation_policy: str = "authorized",
    ensure_approval: bool = False,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any], dict[str, Any]]:
    args = [
        "python3", str(HELPER), "--repo", "acme/myrmex", "--title", "tracking: controlled alpha",
        "--body-file", str(body_path), "--receipt-file", str(receipt_path),
        "--objective-id", OBJECTIVE_ID, "--scope-digest", SCOPE_DIGEST,
        "--approval-marker", APPROVAL_MARKER, "--creation-policy", creation_policy,
    ]
    if ensure_approval:
        args.append("--ensure-approval")
    result = subprocess.run(args, capture_output=True, text=True, env=env, timeout=30)
    payload = json.loads(result.stdout)
    return result, payload, json.loads(receipt_path.read_text())


def calls(log_path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in log_path.read_text().splitlines()]


with tempfile.TemporaryDirectory(prefix="myrmex-tracking-issue-") as td:
    root = Path(td)

    # Creation persists the identity checkpoint before a label effect, then
    # verifies approval. The caller-owned body remains unchanged.
    env, state_path, log_path, receipt_path = setup_case(root, "create", {"issues": [], "next_number": 7})
    body_path = receipt_path.with_name("body.md")
    caller_body = receipt_path.with_name("myrmex-issue-body.user.md")
    body_path.rename(caller_body)
    body_path = caller_body
    result, payload, saved = invoke(env, receipt_path, body_path, ensure_approval=True)
    assert result.returncode == 0, (result.stderr, payload)
    assert payload["status"] == saved["status"] == "ISSUE_APPROVED", payload
    assert payload["approved"] is saved["approved"] is True, payload
    state = json.loads(state_path.read_text())
    assert len(state["issues"]) == 1 and {"name": APPROVAL_MARKER} in state["issues"][0]["labels"], state
    assert body_path.is_file() and IDENTITY not in body_path.read_text(), "helper must not mutate or delete the caller body"
    create_calls = calls(log_path)
    edit = next(call for call in create_calls if call["args"][:2] == ["issue", "edit"])
    assert edit["receipt_before_edit"]["status"] == "ISSUE_CREATED_APPROVAL_PENDING", edit
    assert edit["receipt_before_edit"]["number"] == 7, edit

    # A nonzero create can still have created the remote issue. Exact marker
    # discovery recovers it and a later run reuses it rather than creating again.
    env, state_path, log_path, receipt_path = setup_case(
        root, "recover", {"issues": [], "next_number": 13, "create_after_effect_error": True},
    )
    body_path = receipt_path.with_name("body.md")
    result, payload, saved = invoke(env, receipt_path, body_path)
    assert result.returncode == 0, result.stderr
    assert payload["status"] == saved["status"] == "ISSUE_CREATED_APPROVAL_PENDING", payload
    assert payload["source"] == "created-recovered", payload
    result, payload, saved = invoke(env, receipt_path, body_path, creation_policy="deny", ensure_approval=True)
    assert result.returncode == 0, result.stderr
    assert payload["status"] == saved["status"] == "ISSUE_APPROVED", payload
    recovery_calls = calls(log_path)
    assert sum(call["args"][:2] == ["issue", "create"] for call in recovery_calls) == 1, recovery_calls

    # A unique approved exact marker is reusable even when creation is denied.
    env, state_path, log_path, receipt_path = setup_case(
        root, "reuse", {"issues": [issue(21, labels=[APPROVAL_MARKER])], "next_number": 22},
    )
    body_path = receipt_path.with_name("body.md")
    result, payload, saved = invoke(env, receipt_path, body_path, creation_policy="deny")
    assert result.returncode == 0, result.stderr
    assert payload["status"] == saved["status"] == "ISSUE_REUSED", payload
    assert payload["number"] == 21 and payload["source"] == "reused", payload
    assert not any(call["args"][:2] == ["issue", "create"] for call in calls(log_path))

    # A pre-marker issue is reusable only when the authorized title, complete
    # scope, and existing approval all match. This avoids duplicating old
    # tracking issues without adopting a merely similar title.
    legacy = {
        "number": 27,
        "url": "https://github.com/acme/myrmex/issues/27",
        "title": "tracking: controlled alpha",
        "body": "Authorized tracking scope only.\n",
        "state": "OPEN",
        "labels": [{"name": APPROVAL_MARKER}],
    }
    env, state_path, log_path, receipt_path = setup_case(
        root, "legacy-reuse", {"issues": [legacy], "next_number": 28},
    )
    body_path = receipt_path.with_name("body.md")
    result, payload, saved = invoke(env, receipt_path, body_path, creation_policy="deny")
    assert result.returncode == 0, result.stderr
    assert payload["status"] == saved["status"] == "ISSUE_REUSED", payload
    assert payload["source"] == "reused-approved-fallback" and payload["number"] == 27, payload
    assert not any(call["args"][:2] == ["issue", "create"] for call in calls(log_path))

    # Two exact open markers are materially ambiguous, so no new issue is made.
    env, state_path, log_path, receipt_path = setup_case(
        root, "ambiguous", {"issues": [issue(31), issue(32)], "next_number": 33},
    )
    body_path = receipt_path.with_name("body.md")
    result, payload, saved = invoke(env, receipt_path, body_path)
    assert result.returncode != 0
    assert payload["status"] == saved["status"] == "ISSUE_AMBIGUOUS", payload
    assert payload["candidate_numbers"] == [31, 32], payload
    assert not any(call["args"][:2] == ["issue", "create"] for call in calls(log_path))

    # An approval failure retains the exact issue identity, and the prior
    # receipt proves that the label was never attempted before persistence.
    env, state_path, log_path, receipt_path = setup_case(
        root, "approval-failure", {"issues": [], "next_number": 41, "edit_fail": True, "fallback_fail": True},
    )
    body_path = receipt_path.with_name("body.md")
    result, payload, saved = invoke(env, receipt_path, body_path, ensure_approval=True)
    assert result.returncode != 0
    assert payload["status"] == saved["status"] == "ISSUE_APPROVAL_FAILED", payload
    assert payload["number"] == 41 and payload["url"].endswith("/41"), payload
    failure_calls = calls(log_path)
    edit = next(call for call in failure_calls if call["args"][:2] == ["issue", "edit"])
    assert edit["receipt_before_edit"]["status"] == "ISSUE_CREATED_APPROVAL_PENDING", edit
    assert sum(call["args"][:1] == ["api"] for call in failure_calls) == 1, failure_calls

    # Discovery failure is not interpreted as no matching issue: authorized
    # creation fails closed and never risks a duplicate.
    env, state_path, log_path, receipt_path = setup_case(
        root, "discovery-failure", {"issues": [], "next_number": 51, "list_fail": True},
    )
    body_path = receipt_path.with_name("body.md")
    result, payload, saved = invoke(env, receipt_path, body_path)
    assert result.returncode != 0
    assert payload["status"] == saved["status"] == "ISSUE_DISCOVERY_FAILED", payload
    assert not any(call["args"][:2] == ["issue", "create"] for call in calls(log_path))

    # A missing canonical approval marker is a recoverable human/configuration
    # blocker. It is detected before a create side effect, rather than leaving
    # a new issue that can never satisfy the required delivery gate.
    env, state_path, log_path, receipt_path = setup_case(
        root, "marker-unavailable", {"issues": [], "next_number": 61, "available_labels": []},
    )
    body_path = receipt_path.with_name("body.md")
    result, payload, saved = invoke(env, receipt_path, body_path)
    assert result.returncode != 0
    assert payload["status"] == saved["status"] == "ISSUE_APPROVAL_MARKER_UNAVAILABLE", payload
    assert payload["blocker"] == "APPROVAL_MARKER_UNAVAILABLE", payload
    assert not any(call["args"][:2] == ["issue", "create"] for call in calls(log_path))

print("GitHub tracking issue recovery test: PASS")
