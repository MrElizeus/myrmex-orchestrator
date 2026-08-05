# Changelog

## Unreleased

- Added explicit execution-policy resolution for new runs: clear prompts persist
  their route, while ambiguous prompts reconcile to `REQUEST_EXECUTION_POLICY`
  before any effect.
- Added typed recovery for recoverable `blocked/blocked` Frontier failures,
  atomic confirmed-successor supersession, and historical delegation-attempt
  semantics.
- Added a sanitized EigenGrid incident regression covering multiple failed
  Frontier attempts, a later confirmed plan, an unconsumed authorization, and
  idempotent recovery without transport replay.
- Fixed Frontier pre-effect transport failures getting stuck behind an
  impossible `message_id` recovery precondition. Operations now record effect
  stages, support proven no-effect retries, and can be typed as abandoned or
  superseded by a confirmed successor without losing history.
- Removed hard step ceilings from the continuous orchestrator and frontier transport while retaining bounded scout, worker, and verifier limits.
- Removed legacy bridge files from the public baseline so Myrmex starts as an independent OpenCode project.
- Added `myrmex.memory/v1` and the offline `myrmex-memory` project backend:
  evidence-backed candidate promotion, retrieval metadata, revocation, and
  supersession without replacing exact run state.
- Added governed installation-local memory: explicit project sanitization,
  tool/model applicability, TTL/decay ranking, usefulness-backed confirmation,
  and auditable refutation/supersession. Added separate
  `myrmex.work-unit-metric/v1` normalized outcome telemetry without
  cross-installation sharing or state/policy mutation.

## 0.1.0-alpha.1

- Added hybrid `myrmex-orchestrator` with DIRECT execution as the default route.
- Added bounded `myrmex-scout`, `myrmex-worker`, `myrmex-verifier`, and isolated `myrmex-frontier` subagents.
- Added four on-demand skills: local delegation, autonomous frontier delegation, semantic memory ownership, and Git delivery gates.
- Added seven OpenCode commands for direct, delegated, frontier, resume/status, and doctor workflows.
- Added the atomic dependency-free `myrmex-state` CLI for locks, revisions, request/task IDs, artifacts, digests, recovery, and delivery receipts.
- Added JSON contracts, exact frontier request IDs, stable newest-assistant parsing, bounded browser recovery, continuity, and prompt-injection/secret boundaries.
- Added config-preserving installation, timestamped backups, verification, checksum-aware uninstall, isolated install/rollback tests, and secret scanning.
- Added a one-page `START-HERE.md` path for subagent-assisted or manual installation.
