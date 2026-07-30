# Delegation and verification

## Translating a frontier plan

Validate request ID, run/objective identity, base SHA, repository evidence, allowed/protected scope, acceptance criteria, and verification instructions before execution. Reject malformed, stale, unsafe, speculative, or unrelated work.

Translate the approved plan into one or more ordered `myrmex.work-order/v1` units. Each unit must have one writer, explicit allowed/forbidden/protected paths, acceptance criteria, relevant skills, verification requirements, and `commit=false` / `push=false`.

## Execution

- Execute one `myrmex-worker` at a time in the active workspace.
- Persist the work-order artifact/digest and worker task ID before delegation.
- Validate the returned `myrmex.work-result/v1` against Git state and actual files; never trust the summary alone.
- Do not continue after scope drift, unresolved repository conflict, unsafe dirty-path ownership, or a material product/data/security decision.

For finite scout or verifier fan-out, persist the complete task-ID set with `myrmex-state delegation-batch start` before launch. Completion is an explicit join: collect exactly one terminal structured result for every ID, persist `delegation_batch.completed`, consolidate evidence, and proceed once. A missing result gets one safe recovery attempt; a second unresolved collection is `BLOCKED_MISSING_DELEGATION_RESULT`. On recovery, use the saved batch rather than repeating already-evidenced inspections.

Do not use missing provider environment variables as a delegation precondition. Resolve the effective OpenCode agent first; credential visibility to the parent is informational, while a real Task failure is recorded as `PROVIDER_INVOCATION_FAILED` with sanitized evidence.

## Independent verification

Use `myrmex-verifier` for every frontier-planned code change. Supply the objective, approved plan/criteria, base and candidate identity, allowed/protected paths, worker result, known pre-existing failures, and requested checks.

A `pass` requires scope compliance, diff review, acceptance evidence, and successful relevant checks. A zero exit code cannot override unauthorized scope or missing behavior.

## Corrections and evidence

On a concrete verifier/frontier failure, resume the same worker task for at most the configured correction budget, passing only the bounded corrections. Re-verify the resulting candidate. Never launch competing writers.

Before frontier validation, build an evidence bundle containing changed files/diffstat, relevant patch or symbol-level changes, commands and exit codes, acceptance results, verifier verdict/findings, known limitations, protected dirty paths, and current Git identity. Redact secrets and unrelated source.
