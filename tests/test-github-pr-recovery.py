#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "github-pr-recovery.py"

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
state = Path(os.environ["FAKE_GH_STATE"])
log = Path(os.environ["FAKE_GH_LOG"])
args = sys.argv[1:]
with log.open("a") as handle:
    handle.write(json.dumps(args) + "\n")
exists = state.exists()
if args[:2] == ["pr", "list"]:
    print(json.dumps([{"number": 42, "url": "https://github.com/acme/myrmex/pull/42"}] if exists else []))
elif args[:2] == ["pr", "create"]:
    state.write_text("created\n")
    print("https://github.com/acme/myrmex/pull/42")
elif args[:2] == ["pr", "edit"]:
    print("error: your authentication token is missing required scopes [read:project]", file=sys.stderr)
    raise SystemExit(1)
elif args[:1] == ["api"]:
    if os.environ.get("FAKE_GH_API_FAIL") == "1":
        print("label fallback failed", file=sys.stderr)
        raise SystemExit(1)
    print("[]")
else:
    raise SystemExit("unexpected gh invocation: " + repr(args))
'''

with tempfile.TemporaryDirectory(prefix="myrmex-gh-recovery-") as td:
    root = Path(td)
    fake = root / "gh"
    fake.write_text(FAKE_GH)
    fake.chmod(0o755)
    body = root / "body.md"
    body.write_text("draft body\n")
    receipt = root / "receipt.json"
    env = dict(os.environ, PATH=f"{root}{os.pathsep}{os.environ['PATH']}", FAKE_GH_STATE=str(root / "state"), FAKE_GH_LOG=str(root / "log"))
    result = subprocess.run([
        "python3", str(HELPER), "--repo", "acme/myrmex", "--head", "fix/example", "--base", "main",
        "--title", "fix: example", "--body-file", str(body), "--label", "type:bug", "--receipt-file", str(receipt),
    ], capture_output=True, text=True, env=env, timeout=30)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "PR_CREATED", payload
    assert payload["number"] == 42 and payload["label_method"] == "rest-issue-label-fallback", payload
    saved = json.loads(receipt.read_text())
    assert saved["status"] == "PR_CREATED" and saved["url"].endswith("/42"), saved
    calls = [json.loads(line) for line in (root / "log").read_text().splitlines()]
    assert sum(call[:2] == ["pr", "create"] for call in calls) == 1, calls
    assert sum(call[:2] == ["pr", "edit"] for call in calls) == 1, calls
    assert sum(call[:1] == ["api"] for call in calls) == 1, calls
    assert sum(call[:2] == ["pr", "list"] for call in calls) >= 2, calls

    # A failed narrow label fallback still writes the terminal artifact record
    # rather than throwing while attempting obsolete state persistence.
    failed_receipt = root / "failed-receipt.json"
    fallback_failure = subprocess.run([
        "python3", str(HELPER), "--repo", "acme/myrmex", "--head", "fix/example", "--base", "main",
        "--title", "fix: example", "--body-file", str(body), "--label", "type:bug", "--receipt-file", str(failed_receipt),
    ], capture_output=True, text=True, env=dict(env, FAKE_GH_API_FAIL="1"), timeout=30)
    assert fallback_failure.returncode == 1, fallback_failure.stderr
    failed_payload = json.loads(fallback_failure.stdout)
    assert failed_payload["status"] == "LABEL_APPLICATION_FAILED"
    assert json.loads(failed_receipt.read_text())["label_method"] == "rest-fallback-failed"

    # The helper no longer accepts the old revision-less state patch bridge.
    # Refusal occurs before discovery/create can cause another GitHub effect.
    before = (root / "log").read_text()
    retired = subprocess.run([
        "python3", str(HELPER), "--repo", "acme/myrmex", "--head", "fix/example", "--base", "main",
        "--title", "fix: example", "--body-file", str(body), "--label", "type:bug", "--receipt-file", str(receipt),
        "--state-bin", "/bin/false", "--state-run", "myrmex-test-run",
    ], capture_output=True, text=True, env=env, timeout=30)
    assert retired.returncode != 0 and "retired" in retired.stderr
    assert (root / "log").read_text() == before

print("GitHub PR recovery test: PASS")
