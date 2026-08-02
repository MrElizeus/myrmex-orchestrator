# Local state and governed memory

## Ownership

`myrmex-state` is the source of truth for exact operational state: phase,
status, revisions, task IDs, request IDs/digests, browser URL, locks,
verification revision, commit SHA, push receipt, budgets, and blockers. Native
project memory and Engram must never replace this transactional record.

Each run also has one persisted absolute `artifact_root`, derived or supplied
before transport. The state CLI resolves it against the repository, Git common
directory, and linked worktrees before persistence and rejects repository-local
or symlink-escaping roots. Frontier state retains only request/task/chat/message
identity, response type/digest, stability/poll/recovery metadata, and the root;
cookies, tokens, auth headers, session tokens, and raw browser/profile contents
are never state artifacts.

`myrmex-memory` stores durable, evidence-backed project claims across runs.
The primary alone can create a candidate, promote it, revoke it, or supersede
it. Transport/scout/worker/verifier return evidence or `memory_candidates`; no
child agent writes memory. Engram remains an optional semantic adapter for
compact summaries, plans, decisions, root causes, and final run summaries.

## Local topics and artifacts

A run lives under the configured state root, normally:

```text
~/.local/state/opencode/myrmex-orchestrator/runs/<run-id>/
  state.json
  events.jsonl
  lock.json
  artifacts/
```

Use `myrmex-state artifact-path <run-id> <name>` before writing an objective,
context pack, outbound prompt, frontier response, plan, execution report, or
verification receipt. Hash outbound/response files with `myrmex-state hash
--file` and record non-critical receipt metadata through the applicable typed
state command. External effects use the durable `pending_operations` ledger:
record `intent`, then an observed effect, receipt, and terminal confirmation
with a stable idempotency key. Generic `patch` must never write a receipt,
operation, task, phase, or completion field.

The native project-memory backend is separate and normally lives under:

```text
~/.local/state/opencode/myrmex-orchestrator/memory/projects/<identity-hash>/
  events.jsonl       # authoritative append-only lifecycle audit
  index.json         # recoverable materialized retrieval index
```

Promote a candidate only with accessible digest-addressed project evidence.
Run IDs, commit SHAs, verifier/frontier request IDs, and artifact digests may
link a claim to the run but remain evidence metadata, not a replacement for
local state.

## Optional Engram topic keys

```text
myrmex/frontier/<run-id>/summary
myrmex/frontier/<run-id>/plan
myrmex/frontier/<run-id>/decisions
myrmex/frontier/<run-id>/result
myrmex/frontier/<run-id>/dormant
```

Do not store polling heartbeats, duplicate full artifacts, secrets, or raw
repository snapshots in either backend.

## Degraded memory

If native memory or Engram is unavailable, continue when local state is healthy
and the action is otherwise safe. Report `memory: degraded` (and persist only a
separate operational health indicator when one is applicable). Never claim a
memory save/retrieval without the backend's receipt.
