# Changelog

## Unreleased

- Removed hard step ceilings from the continuous orchestrator and frontier transport while retaining bounded scout, worker, and verifier limits.
- Removed legacy bridge files from the public baseline so Myrmex starts as an independent OpenCode project.

## 0.1.0-alpha.1

- Added hybrid `myrmex-orchestrator` with DIRECT execution as the default route.
- Added bounded `myrmex-scout`, `myrmex-worker`, `myrmex-verifier`, and isolated `myrmex-frontier` subagents.
- Added four on-demand skills: local delegation, autonomous frontier delegation, semantic memory ownership, and Git delivery gates.
- Added seven OpenCode commands for direct, delegated, frontier, resume/status, and doctor workflows.
- Added the atomic dependency-free `myrmex-state` CLI for locks, revisions, request/task IDs, artifacts, digests, recovery, and delivery receipts.
- Added JSON contracts, exact frontier request IDs, stable newest-assistant parsing, bounded browser recovery, continuity, and prompt-injection/secret boundaries.
- Added config-preserving installation, timestamped backups, verification, checksum-aware uninstall, isolated install/rollback tests, and secret scanning.
- Added a one-page `START-HERE.md` path for subagent-assisted or manual installation.
