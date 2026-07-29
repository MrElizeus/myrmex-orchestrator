---
name: myrmex-memory
description: "Use when prior project decisions/recovery context matter or durable non-obvious knowledge should be saved. Keeps Engram semantic and makes the primary the sole memory owner."
license: Apache-2.0
compatibility: "Optional Engram mem_* or engram_* tools"
metadata:
  author: "Myrmex contributors"
  version: "0.1.0"
---

# Myrmex Memory

Engram stores durable semantic knowledge, not exact run state, transcripts, routine tool output, or Git facts.

## Read

Search when prior architecture, product decisions, conventions, root causes, unresolved work, or recovery context may materially change the objective. Prefer recent context, then targeted search. Verify stale memories against repository truth.

## Write

Only the primary writes. Save non-obvious reusable architecture/decision/bug/pattern/discovery/blocker knowledge with stable topic keys. Subagents return `memory_candidates`; evaluate, merge, correct, or discard them.

Do not save every file read, edit, test line, browser poll, or fact already represented by Git/local state.

## Failure

If Engram is unavailable, continue when safe, preserve exact frontier state locally, and report `memory: degraded`. Never claim a save without a tool receipt.

See `references/topic-keys.md`.
