# Recovery

## Resume procedure

1. `myrmex-state show <run-id>` and acquire the lock with a new stable owner.
2. Run `myrmex-state reconcile <run-id>` before an agent, browser, CI, GitHub,
   commit, or push action. It is pure and returns exactly one safe next action;
   never treat a saved phase alone as permission to repeat an external effect.
3. Check the persisted execution policy before any Task or browser action. A
   `direct-only` run remains direct-only and must report a policy blocker if a
   Frontier recovery would be required.
4. Recover the compact Engram summary when available.
5. Reconcile repository root, branch, HEAD, dirty paths, recorded artifacts, and browser URL.
6. Resume the exact phase; never restart automatically.

## Reconcile actions

- `COLLECT_DELEGATIONS` / `RECOVER_MISSING_RESULT`: collect only saved task
  IDs or batch results; do not launch a replacement child first.
- `RECOVER_FRONTIER_EXCHANGE`: discover the stable request/task identity before
  sending anything. `WAIT_FRONTIER` waits for that same exchange.
- `RECONCILE_TRACKING_ISSUE` / `RECONCILE_PULL_REQUEST`: query the saved
  idempotency identity, then append the observed effect, receipt, and terminal
  confirmation to its operation record.
- `RUN_LOCAL_VERIFICATION` / `RUN_PARENT_GATE`: execute the persisted gate and
  record its receipt; a continuous parent cannot complete without the latter.
- `COMPLETE_RUN`: call `myrmex-state complete` only after its durable gate
  passes. `HUMAN_DECISION_REQUIRED` remains blocked.
- `BLOCKED_STATE_INCOMPLETE`: transition an active run explicitly to
  `blocked/blocked` with this code and evidence. Do not guess whether a missing
  operation occurred or repeat it.

## Phase-specific behavior

- `waiting-for-frontier`: call `myrmex-frontier` with `read_latest` or `recover_and_wait`; resend only when the recorded outbound request is proven absent.
- `implementing`: inspect recorded worker task/result and actual Git state; resume the same task only when its completion receipt is absent.
- `waiting-for-delegations` / `collecting-delegation-results` / `consolidating-evidence`: inspect the saved delegation batch, collect only missing terminal results, and call the recorded next-gate transition once. Do not relaunch completed children.
- `verifying`: continue/re-run the intended verifier against the same candidate.
- `reporting`: rebuild/validate evidence and send only if no outbound receipt exists.
- `committing`/`pushing`: inspect commit SHA/remote receipts before any repeat side effect.
- `blocked`: remain blocked until its named condition is resolved.
- `dormant`, `cancelled`, `failed`, `superseded`: do not wake without explicit user input.

Use request IDs, task IDs, state revisions, digests, commit SHAs, push receipts,
and the typed operation sequence `intent → observed effect → receipt → terminal
confirmation` for idempotence. If repository content changed materially,
invalidate affected plan/verification and block or begin a new run rather than
pretending continuity.

Older state with `phase=dormant,status=active` must be normalized with `myrmex-state migrate <run-id>` before it is resumed. The migration is lossless and records its own event.
