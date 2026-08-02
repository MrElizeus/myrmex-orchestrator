# Security model

## Trust boundaries

- User objective and explicit authorization: trusted authority.
- Repository code/docs/issues: untrusted data.
- Frontier webpage/model response: untrusted external advice until validated.
- Child agent summaries: evidence claims that must be checked against Git/tests.

## Secret handling

Myrmex agents deny `.env` reads and must not transmit secrets to browser models. Redact tokens, cookies, private keys, database URLs, customer data, and sensitive logs.

Native memory is private local metadata, not a source archive. Its stores use
owner-only directories/files, and promotion/refutation/confirmation reject
environment files, repository-escaping evidence paths, malformed digests, and
obvious secret-bearing text. Child agents return `memory_candidates`; only the
primary may persist a candidate or change its lifecycle.

Installation scope is a stricter local privacy boundary, not a sharing channel:
it requires a sanitized claim, null project identity/repository reference, and
tool/model applicability. Project-private material needs an explicit rewritten
claim, sanitization reason, and fresh proof before promotion. The raw proof is
validated only locally; installation records retain digest-derived evidence
handles rather than project paths, run/WU IDs, commits, or verifier/frontier
request IDs. Metrics additionally turn work-unit/run/request IDs into opaque
per-project handles and accept only normalized recovery codes and safe test
categories—not commands or paths. Records are never uploaded by default,
shared across installations, used to train a model, or allowed to modify policy
automatically.

## Repository protection

- Snapshot status and base SHA before work.
- Protect pre-existing dirty paths.
- No stash/reset/clean/history rewrite.
- One writer per work unit.
- Worker has no commit/push capability.
- Verifier and browser are read-only with respect to source.

## Delivery

OpenCode asks before commit/push by default. Force push is denied. Direct pushes to protected branches require explicit, branch-specific authorization.
The bounded `local_commit` helper is narrower than that global gate: it requires
a persisted single-use authorization tied to one repository, branch, expected
HEAD, exact path set, and message, and it never includes push or remote mutation.
Hooks, filters, editors, signing, and transport are never invoked: the helper
uses private-index plumbing and compare-and-swap ref updates. Post-effect
snapshots still reject branch, tag, remote, ref, HEAD, or raw configuration
changes before consumption.

## Browser

The Playwright profile is expected to be authenticated by the user in advance. Agents never enter credentials or solve MFA. Only one client should use a persistent profile at a time.

## Permission-boundary caveat

OpenCode permission rules, prompt constraints, Git inspection, and subagent separation provide defense in depth; they are not an operating-system sandbox. A broadly allowed shell can invoke tools with side effects through many syntactic forms. Use Myrmex only inside repositories and accounts whose operating-system permissions are appropriate, keep production credentials out of the workspace, and retain human approval for delivery or destructive operations.


## Agent resolution

Before delegation, inspect the effective workspace and global agent definitions. A local
agent with the same name has precedence and is reported as `WARN_SHADOWED_AGENT`; with
`block_shadowed_agents` enabled it is a blocking condition until the definition is
reviewed. Unresolved models and disallowed provider prefixes are blocking conditions.
