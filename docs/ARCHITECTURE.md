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

`myrmex-memory` is a separate dependency-free local JSONL/index backend for evidence-backed claims: private **project** architecture invariants, decisions, conventions, and known failure modes, plus sanitized **installation**-local operational lessons. It records candidate, verified, revoked, superseded, and confirmed lifecycle snapshots without turning semantic memory into a transaction database. Installation retrieval is filtered/ranked by tool/model applicability and freshness (TTL/decay); project scope ranks first. The primary is the sole native-memory writer/promoter/revoker/confirmer; browser/scout/worker/verifier return evidence or `memory_candidates` only.

Installation promotion is an explicit privacy boundary: raw project-private claims and proof metadata are never copied. The backend validates a newly supplied local proof, then stores only a digest-derived sanitized handle with an opaque source-memory reference. Normalized work-unit metrics live in a separate installation-local JSONL stream, so they cannot alter semantic confidence, policy, or exact run state. There is no cross-installation sharing, model training, automatic policy change, daemon, or scheduler.

Engram remains an optional semantic adapter for compact continuity across sessions. Exact run state and receipts stay in `myrmex-state`; native memory and Engram can degrade safely rather than inventing a receipt. This split keeps recovery durable without a database service, vector store, or daemon.

## Frontier plane

The frontier model is a planning/validation authority for one stated objective. It receives a redacted context pack tied to a base SHA and later an evidence bundle. The browser transport is isolated because snapshots and long active waits would otherwise inflate coding context.

## Deliberate v0.1 boundaries

Myrmex does not introduce a daemon, custom OpenCode plugin, parallel worktree scheduler, review framework, or deployment engine. OpenCode supplies agents/tasks; the state CLI supplies exact recovery. Add heavier infrastructure only after a demonstrated operational need.
