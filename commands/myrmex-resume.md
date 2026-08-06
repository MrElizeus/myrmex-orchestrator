---
description: Resume a persisted Myrmex frontier run by run ID
agent: myrmex-orchestrator
---

Resume Myrmex frontier run `$ARGUMENTS` using `myrmex-frontier-delegation`.

Load authoritative local state with `myrmex-state show`, acquire the run lock with a new stable owner, retrieve relevant verified native memory with `myrmex-memory search --scope auto` (private project records first, then applicable sanitized installation lessons) and the compact optional Engram summary when available, then run `myrmex-state reconcile <run-id>` **before** launching an agent, browser exchange, CI retry, tracking issue, PR action, commit, or push. Inspect tool/model applicability and TTL/decay rather than assuming an installation lesson remains current. The command is pure: it writes neither state nor events and returns exactly one action. Native/Engram memory is advisory and never replaces exact run receipts.

Treat the returned action as the recovery plan, not permission to repeat an
effect. `REQUEST_EXECUTION_POLICY` uses OpenCode `question` once and persists the
answer before effects. `COLLECT_DELEGATIONS`/`RECOVER_MISSING_RESULT` collect
only saved task IDs. `RESOLVE_PRE_EFFECT_FRONTIER_FAILURE` and
`FINALIZE_FRONTIER_SUPERSESSION` use the typed `myrmex-state recovery` command;
they never resend a request and never invent a `message_id`.
`RECOVER_FRONTIER_EXCHANGE` discovers the saved request before transport, and
`WAIT_FRONTIER` waits for that same exchange. Delivery and verification actions
reconcile their existing typed operation. `COMPLETE_RUN` may call
`myrmex-state complete` only after its gate passes. `HUMAN_DECISION_REQUIRED`
remains blocked. For an unknown `BLOCKED_STATE_INCOMPLETE`, preserve the blocker
and evidence; do not guess whether an unrecorded effect occurred.

Honor `execution.requested_policy` first. A persisted `direct-only` policy
must survive resume: do not start a Task or a Frontier exchange, and report a
precise execution-policy blocker if recovery would require either route.

Continue from the exact valid phase. Do not restart the objective, duplicate a worker task, resend an already present frontier request, recommit an existing commit, or push without authorization. If reconciliation is ambiguous or the candidate changed materially, enter blocked state with exact evidence.
