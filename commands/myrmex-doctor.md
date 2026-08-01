---
description: Diagnose the installed Myrmex agents, skills, memory, browser transport, and Git safety without modifying a repository
agent: myrmex-orchestrator
---

Run a read-only Myrmex capability diagnostic for the current OpenCode environment.

Requirements:

1. Resolve the authoritative workspace with `git rev-parse --show-toplevel 2>/dev/null || pwd`.
2. Inspect Git branch, HEAD, and status without editing.
3. Confirm the Myrmex subagents available to `Task`: `myrmex-scout`, `myrmex-worker`, `myrmex-verifier`, and `myrmex-frontier`.
4. Confirm the skills `myrmex-delegation`, `myrmex-frontier-delegation`, `myrmex-memory`, and `myrmex-git-delivery` are discoverable.
5. Run `myrmex-state doctor` and `myrmex-memory doctor`; report local state and offline project-memory health without creating a real project run.
6. Check optional Engram memory tool availability without writing a test memory.
7. Check browser tool availability without navigating, sending a message, or reading browser secrets.
8. Report `PASS`, `WARN`, or `FAIL` for every capability, with the exact missing prerequisite and one remediation action.

Do not edit, install, commit, push, open a browser page, or invoke a writer.


The diagnostic also inspects effective agent resolution. A local .opencode/agents/
definition shadows the global one and is reported as WARN_SHADOWED_AGENT; unresolved
delegated models and disallowed provider prefixes are blocking policy states. The
diagnostic reports effective source, model, provider, and bounded steps.
