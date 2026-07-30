---
name: myrmex-delegation
description: "Trigger: delegate implementation, complex coding task, fresh-context worker, repository mapping, independent verification. Coordinate one bounded Myrmex work unit without SDD or frontier ceremony."
license: Apache-2.0
metadata:
  author: "Myrmex contributors"
  version: "0.1.0"
---

# Myrmex Delegation

Use this skill when a normal coding objective benefits from fresh context, broad repository mapping, a dedicated writer, or independent verification. Do not use it for a small DIRECT task, explicit SDD, or a frontier-controlled loop.

## Rules

- The primary owns scope, user communication, Engram writes, commit, and push.
- Use `myrmex-scout` or native `explore` only when mapping is needed.
- Use exactly one `myrmex-worker` writer per work unit and workspace.
- Protect all pre-existing dirty paths.
- Give the worker a `myrmex.work-order/v1`; never delegate only a vague sentence.
- Require `myrmex.work-result/v1` evidence.
- Use `myrmex-verifier` for behavioral, multi-layer, risky, or materially non-trivial changes.
- Reuse the same task/session for at most up to two bounded correction attempts.
- Do not silently expand scope, launch competing writers, or introduce SDD.

## Flow

1. Inspect branch, HEAD, status, and relevant user changes.
2. Map the repository if needed.
3. Build the work order from `references/work-order.md`.
4. Delegate implementation.
5. Validate the result contract and inspect Git state.
6. Delegate verification when required.
7. If verification fails, resume the writer once with exact corrections, then re-verify.
8. Commit/push only under the primary's authorization policy.
9. Save only durable memory and report evidence.

Read `references/work-order.md` and `references/verification.md` when constructing the corresponding requests.

Use scripts/collect-git-evidence.py for receipts and scripts/validate-diff-size.py for the flexible 400-line policy.

The state CLI enforces the two-correction budget. A third correction returns BLOCKED_CORRECTION_BUDGET. Record each verification cycle with corrected, remaining, and new defects; two consecutive non-reducing cycles return BLOCKED_NO_PROGRESS.
