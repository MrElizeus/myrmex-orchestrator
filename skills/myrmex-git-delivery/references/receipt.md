# Delivery receipt

Receipts are derived artifacts, not prose. Run scripts/collect-git-evidence.py
with the repository and base SHA and retain its JSON output. It records branch,
HEAD, base, changed files, additions, deletions, status, and git diff --check.

Validate a submitted receipt with:

  python3 scripts/verify-receipt.py --repo . --base-sha "$BASE_SHA" --receipt receipt.json

Any mismatch is FAIL_RECEIPT_MISMATCH. A target mutation invalidates the receipt;
recollect it after every tracked content or mode change. Delivery still requires
separate commit and push authorization.
