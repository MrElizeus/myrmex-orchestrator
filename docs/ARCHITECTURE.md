# Architecture

## Control plane

`myrmex-orchestrator` owns intent/routing, user interaction, scope, local run state, semantic memory reads/writes, Git delivery, and final acceptance. It executes ordinary bounded work directly; in delegated/frontier routes it coordinates fresh-context agents.

## Execution plane

- `myrmex-scout`: compact evidence-grounded repository map.
- `myrmex-worker`: one bounded writer.
- `myrmex-verifier`: independent read-only verdict.
- `myrmex-frontier`: browser-only frontier transport with active waiting.

Child agents cannot invoke `Task`, preventing recursive swarms and making ownership explicit.

## State and memory planes

`myrmex-state` is a small dependency-free CLI that stores exact frontier state atomically under the user's XDG state directory: phase, revisions, task/request IDs, locks, digests, delivery receipts, and blockers.

Semantic memory stores durable continuity: decisions, plans, root causes, and compact summaries. The primary is the sole memory writer. Browser/scout/worker/verifier return evidence or memory candidates.

This split avoids using semantic search as a transaction database while keeping recovery across sessions and compacted conversations.

## Frontier plane

The frontier model is a planning/validation authority for one stated objective. It receives a redacted context pack tied to a base SHA and later an evidence bundle. The browser transport is isolated because snapshots and long active waits would otherwise inflate coding context.

## Deliberate v0.1 boundaries

Myrmex does not introduce a daemon, custom OpenCode plugin, parallel worktree scheduler, review framework, or deployment engine. OpenCode supplies agents/tasks; the state CLI supplies exact recovery. Add heavier infrastructure only after a demonstrated operational need.
