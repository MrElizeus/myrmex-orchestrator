# Recovery

## Resume procedure

1. `myrmex-state show <run-id>` and acquire the lock with a new stable owner.
2. Recover the compact Engram summary when available.
3. Reconcile repository root, branch, HEAD, dirty paths, recorded artifacts, and browser URL.
4. Resume the exact phase; never restart automatically.

## Phase-specific behavior

- `waiting-for-frontier`: call `myrmex-frontier` with `read_latest` or `recover_and_wait`; resend only when the recorded outbound request is proven absent.
- `implementing`: inspect recorded worker task/result and actual Git state; resume the same task only when its completion receipt is absent.
- `waiting-for-delegations` / `collecting-delegation-results` / `consolidating-evidence`: inspect the saved delegation batch, collect only missing terminal results, and call the recorded next-gate transition once. Do not relaunch completed children.
- `verifying`: continue/re-run the intended verifier against the same candidate.
- `reporting`: rebuild/validate evidence and send only if no outbound receipt exists.
- `committing`/`pushing`: inspect commit SHA/remote receipts before any repeat side effect.
- `blocked`: remain blocked until its named condition is resolved.
- `dormant`, `cancelled`, `failed`, `superseded`: do not wake without explicit user input.

Use request IDs, task IDs, state revisions, digests, commit SHAs, and push receipts for idempotence. If repository content changed materially, invalidate affected plan/verification and block or begin a new run rather than pretending continuity.

Older state with `phase=dormant,status=active` must be normalized with `myrmex-state migrate <run-id>` before it is resumed. The migration is lossless and records its own event.
