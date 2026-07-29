---
description: Show read-only status for a Myrmex frontier run
agent: myrmex-orchestrator
---

Show read-only status for Myrmex frontier run `$ARGUMENTS`.

Use `myrmex-state show <run-id>` (or the latest run when no ID is supplied), then optionally enrich it with the compact Engram summary. Reconcile with current Git facts without changing anything. Report objective, scope, mode, phase/status, state revision, browser conversation, latest request/plan, worker/verifier task state, commit/push state, blocker, memory status, and next valid transition.

Do not send browser messages, invoke subagents, edit, commit, push, or unlock another owner.
