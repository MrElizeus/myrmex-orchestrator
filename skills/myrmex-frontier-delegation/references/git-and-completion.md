# Git delivery and completion

## Delivery gates

Commit and push are separate capabilities. Frontier approval does not authorize either action.

Before commit:

1. Require local verifier `pass` for frontier-planned changes.
2. Re-check branch, HEAD, status, protected dirty paths, and candidate diff.
3. Stage only objective-owned paths and review the staged diff for secrets, generated junk, and unrelated work.
4. Commit only under the run's explicit commit policy; record commit SHA and verification receipt in local state.

Before push, confirm exact commit(s), remote, branch, and explicit current-run push authorization. Never force-push, rewrite shared history, or infer deployment/release permission. If content changes after verification, invalidate the gate and re-verify.

## Scope-aware completion

- **Narrow objective:** `OBJECTIVE_COMPLETE` or `OBJECTIVE_ALREADY_COMPLETE`, supported by evidence, ends the active loop.
- **Continuous parent objective:** `SUB_OBJECTIVE_COMPLETE` is not terminal. Run `assets/parent-objective-gate.md` exactly once for that completed sub-objective. Continue only for one concrete repo-grounded plan within the same parent objective. `PARENT_OBJECTIVE_COMPLETE` is terminal.
- A blocking clarification, exhausted budget, login/credential requirement, destructive decision, or hard tool failure becomes `blocked` with the exact recovery condition.

## Terminal transition

Persist final artifacts and receipts, update local state to `dormant`, `blocked`, `failed`, `cancelled`, or `superseded`, save only a compact durable Engram summary/decision when available, and release the run lock. Do not ask the frontier for arbitrary additional work after the relevant objective scope is complete.
