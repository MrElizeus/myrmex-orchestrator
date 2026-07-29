# Local state and Engram

## Ownership

`myrmex-state` is the source of truth for exact operational state: phase, status, revisions, task IDs, request IDs/digests, browser URL, locks, verification revision, commit SHA, push receipt, budgets, and blockers.

Engram stores durable semantic continuity: objective summaries, plans, non-obvious decisions, root causes, and final run summaries. The primary is the only memory writer. Transport/scout/worker/verifier return evidence or memory candidates.

## Local topics and artifacts

A run lives under the configured state root, normally:

```text
~/.local/state/opencode/myrmex-orchestrator/runs/<run-id>/
  state.json
  events.jsonl
  lock.json
  artifacts/
```

Use `myrmex-state artifact-path <run-id> <name>` before writing an objective, context pack, outbound prompt, frontier response, plan, execution report, or verification receipt. Hash outbound/response files with `myrmex-state hash --file` and record the digest through `patch`.

## Engram topic keys

```text
myrmex/frontier/<run-id>/summary
myrmex/frontier/<run-id>/plan
myrmex/frontier/<run-id>/decisions
myrmex/frontier/<run-id>/result
myrmex/frontier/<run-id>/dormant
```

Do not store polling heartbeats or duplicate full artifacts in Engram.

## Degraded memory

If Engram is unavailable, continue when local state is healthy and the action is otherwise safe. Record `memory_status=degraded`. Never claim a memory save without a tool receipt.
