# Delegation and verification

## Translating a frontier plan

Validate request ID, run/objective identity, base SHA, repository evidence, allowed/protected scope, acceptance criteria, and verification instructions before execution. Reject malformed, stale, unsafe, speculative, or unrelated work.

Translate the approved plan into one or more ordered `myrmex.work-order/v1` units. Each unit must have one writer, explicit allowed/forbidden/protected paths, acceptance criteria, relevant skills, verification requirements, and `commit=false` / `push=false`.

## Execution

- Execute one `myrmex-worker` at a time in the active workspace.
- Persist the work-order artifact/digest and worker task ID before delegation.
- Validate the returned `myrmex.work-result/v1` against Git state and actual files; never trust the summary alone.
- Do not continue after scope drift, unresolved repository conflict, unsafe dirty-path ownership, or a material product/data/security decision.

## Independent verification

Use `myrmex-verifier` for every frontier-planned code change. Supply the objective, approved plan/criteria, base and candidate identity, allowed/protected paths, worker result, known pre-existing failures, and requested checks.

A `pass` requires scope compliance, diff review, acceptance evidence, and successful relevant checks. A zero exit code cannot override unauthorized scope or missing behavior.

## Corrections and evidence

On a concrete verifier/frontier failure, resume the same worker task for at most the configured correction budget, passing only the bounded corrections. Re-verify the resulting candidate. Never launch competing writers.

Before frontier validation, build an evidence bundle containing changed files/diffstat, relevant patch or symbol-level changes, commands and exit codes, acceptance results, verifier verdict/findings, known limitations, protected dirty paths, and current Git identity. Redact secrets and unrelated source.
