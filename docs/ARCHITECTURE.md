# Architecture

## Control plane

`myrmex-orchestrator` owns intent/routing, user interaction, scope, local run state, semantic memory reads/writes, Git delivery, and final acceptance. Every new persisted run resolves an explicit execution policy before effects: clear prompts select the route; ambiguous prompts use OpenCode `question` once and remain `unresolved` until the answer is persisted. It executes ordinary bounded work directly only when the resolved policy permits it; in delegated/frontier routes it coordinates fresh-context agents.

## Execution plane

- `myrmex-scout`: compact evidence-grounded repository map.
- `myrmex-worker`: one bounded writer.
- `myrmex-verifier`: independent read-only verdict.
- `myrmex-frontier`: browser-only frontier transport with active waiting.

Child agents cannot invoke `Task`, preventing recursive swarms and making ownership explicit.

## State and memory planes

`myrmex-state` is a small dependency-free CLI that stores exact frontier state atomically under the user's XDG state directory: phase, revisions, task/request IDs, locks, digests, delivery receipts, and blockers.

Campaign Intelligence (P1) records planning artifacts as immutable, digest-addressed sidecar contracts — `plan-revision-v1` and the planning exchange (`planning-request-v1`/`planning-result-v1`) — preserving provenance and lifecycle history without replacing `myrmex-state` or `myrmex-campaign`, without authorizing repository effects, and without completing continuous objectives.

The durable backlog boundary uses `myrmex.backlog-item/v1` and `myrmex.backlog-snapshot/v1`. Items retain exact source provenance but no execution status; snapshots are immutable, deterministic completion markers written only after every referenced item artifact is durable. Legacy pre-contract normalized schema IDs remain readable, while every new write uses the public P1-006 contracts.

Plan revisions form immutable, linear lifecycle chains. `plan-list` and `plan-show` reconstruct those chains from authoritative sidecar artifacts and expose no write authority. The generic P1-007 builder/store still rejects creation of an absent `active` record. P1-012 satisfies that barrier only through a durable PASS precondition report bound to exact campaign, reviewed plan, critic, semantic DAG, work orders, source/repository freshness, human decisions, and activation authority. Activation appends deterministic `validated` then `active` records and grants no repository or delivery authority.

The structured planner boundary is: authoritative normalized-backlog snapshot plus exact `myrmex.repository-context/v1` digest -> deterministic planning request/context and fixed prompt -> dedicated `myrmex-planner` task identity -> strict result and exact backlog-coverage validation -> immutable response/task receipt -> immutable `proposed` plan revision. Facts, assumptions, and uncertainties are separate in the result, and every backlog item maps to at least one proposed WU. The agent and gateway are planning-only: they cannot edit, delegate, write memory, create campaign WUs, activate plans, commit, push, merge, release, deploy, or perform repository effects.

The independent review boundary then binds that proposed record and planner task to a distinct `myrmex-plan-critic` task. A deterministic preflight checks coverage, scope, verification, dependencies, risk, unsupported assumptions, and human gates before a strict `PASS|REVISE|BLOCKED|INVALID` review can be accepted. A PASS may append only an immutable `reviewed` lifecycle record; neither the critic nor its orchestrator can activate the plan or authorize repository effects.

The P1-010 compiler projects one current `reviewed` plan into deterministic `myrmex.work-order/v2` records and campaign-v1 WorkUnits. Every order carries exact plan/review and normalized-backlog provenance plus explicit scope, acceptance, verification, risk, route, gates, and evidence. Preview is strictly read-only and fails rather than repairing stale projections; apply uses campaign revision CAS and preserves legacy WUs that have no work-order extension.

P1-011 semantic DAG validation binds those work orders back to the reviewed plan and critic receipt, reports typed blocking defects, excludes unresolved `work_unit_ready` gates from readiness, and emits stable graph/order/critical-path evidence. P1-012 consumes one exact PASS receipt through a state-first activation intent and crash-recoverable precondition/projection/receipt chain. Exact replay is byte-preserving, while stale pre-effect inputs, authority, decisions, campaign revisions, or repository HEAD fail closed.

`myrmex-memory` is a separate dependency-free local JSONL/index backend for evidence-backed claims: private **project** architecture invariants, decisions, conventions, and known failure modes, plus sanitized **installation**-local operational lessons. It records candidate, verified, revoked, superseded, and confirmed lifecycle snapshots without turning semantic memory into a transaction database. Installation retrieval is filtered/ranked by tool/model applicability and freshness (TTL/decay); project scope ranks first. The primary is the sole native-memory writer/promoter/revoker/confirmer; browser/scout/worker/verifier return evidence or `memory_candidates` only.

Installation promotion is an explicit privacy boundary: raw project-private claims and proof metadata are never copied. The backend validates a newly supplied local proof, then stores only a digest-derived sanitized handle with an opaque source-memory reference. Normalized work-unit metrics live in a separate installation-local JSONL stream, so they cannot alter semantic confidence, policy, or exact run state. There is no cross-installation sharing, model training, automatic policy change, daemon, or scheduler.

Engram remains an optional semantic adapter for compact continuity across sessions. Exact run state and receipts stay in `myrmex-state`; native memory and Engram can degrade safely rather than inventing a receipt. This split keeps recovery durable without a database service, vector store, or daemon.

## Frontier plane

The frontier model is a planning/validation authority for one stated objective. It receives a redacted context pack tied to a base SHA and later an evidence bundle. The browser transport is isolated because snapshots and long active waits would otherwise inflate coding context.

## Deliberate v0.1 boundaries

Myrmex does not introduce a daemon, custom OpenCode plugin, parallel worktree scheduler, review framework, or deployment engine. OpenCode supplies agents/tasks; the state CLI supplies exact recovery. Add heavier infrastructure only after a demonstrated operational need.
