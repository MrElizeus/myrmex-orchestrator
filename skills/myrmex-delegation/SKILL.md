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
- Resolve an agent/model/provider through OpenCode precedence before delegation. Never treat an absent provider environment variable as proof that Task cannot route the agent.
- `CREDENTIAL_NOT_VISIBLE_TO_ORCHESTRATOR` is informational. Only a real Task failure records `PROVIDER_INVOCATION_FAILED`; resolver-backed `AGENT_MODEL_UNRESOLVED` and `AGENT_NOT_INSTALLED` are blocking states.

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

## Fan-out join and liveness

For a finite scout/verifier fan-out, create one `myrmex-state delegation-batch start` record with all task IDs before launching it. Do not continue merely because the UI appears idle or complete.

1. Wait for each recorded task to reach `success`, `failed`, `blocked`, or `cancelled`.
2. Recover exactly one structured final response per task ID and persist it with `delegation-batch collect`.
3. When all results are present, consolidate them, detect duplicate task IDs and contradictory structured claims, then call `delegation-batch proceed --next-phase <phase>` exactly once.
4. If a final response is missing, use `--recover-missing` once when safe. A second unresolved collection becomes `BLOCKED_MISSING_DELEGATION_RESULT`.
5. On resume, inspect the saved batch first; never relaunch a completed child or duplicate the next gate.

Emit a compact state/event transition for `waiting-for-delegations`, `collecting-delegation-results`, `consolidating-evidence`, and `proceeding-to-next-gate`. Do not add aggressive polling or chatty heartbeats.

Read `references/work-order.md` and `references/verification.md` when constructing the corresponding requests.

Use scripts/collect-git-evidence.py for receipts and scripts/validate-diff-size.py for the flexible 400-line policy.

The state CLI enforces the two-correction budget. A third correction returns BLOCKED_CORRECTION_BUDGET. Record each verification cycle with corrected, remaining, and new defects; two consecutive non-reducing cycles return BLOCKED_NO_PROGRESS.
