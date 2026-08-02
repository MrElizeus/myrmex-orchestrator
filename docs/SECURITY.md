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
- Frontier transport is validated independently from the model decision. Only
  a matching request ID, response message ID, effect, and receipt can produce
  a technically confirmed exchange; `REMEDIATE` and `BLOCKED` are decisions,
  not transport failures. Recovery of a legacy failed exchange is typed,
  revision-checked, identity-bound, append-only, and cannot rewrite its
  original terminal evidence.
- Continuous-parent lifecycle is typed: a completed WU cannot complete its
  parent, a next-WU handoff requires confirmed Frontier `proposed_plan`
  provenance, and only `PARENT_OBJECTIVE_COMPLETE` or the explicit
  `PARENT_OBJECTIVE_CANCELLED` command is terminal for the parent. Generic
  patches cannot modify parent identity, scope, gate evidence, handoffs,
  clarification, pause, or cancellation state.
- Informational status messages are a non-mutating side-band. They may expose
  the current state and exact reconcile action, but must not create a pause,
  cancel, abandonment, or replacement operation. Explicit pause and typed
  clarification retain a resumable action; cancellation remains terminal.
- Correction grants are immutable identity capabilities: work unit, candidate
  SHA, scope digest, verification request ID, and defect revision must all match
  the current blocker. A consumed grant is never replayed, and a stale grant
  cannot clear or replace a newer blocker.

## Delivery

OpenCode asks before commit/push by default. Force push is denied. Direct pushes to protected branches require explicit, branch-specific authorization.
The bounded `local_commit` helper is narrower than that global gate: it requires
a persisted single-use authorization tied to one repository, branch, expected
HEAD, exact path set, and message, and it never includes push or remote mutation.
Hooks, filters, editors, signing, and transport are never invoked: the helper
uses private-index plumbing and compare-and-swap ref updates. Post-effect
snapshots still reject branch, tag, remote, ref, HEAD, or raw configuration
changes before consumption.

Tracking-issue and PR delivery have a separate typed side-effect boundary. The
policy resolver is read-only and validation must use fake/local GitHub helpers;
tests must not call live GitHub. Before any GitHub effect, state persists a
`tracking_issue` intent containing the complete resolved policy, policy digest,
and a stable objective/scope marker. State recomputes the resolver from the
persisted repository root and exact input paths before accepting it, so a
self-consistent forged allow artifact cannot bypass repository policy. The exact
marker is the only unconditional resume identity; title similarity never
authorizes adoption. A confirmed tracking operation requires the persisted
repository, issue number, URL, approval marker, and a canonical
`ISSUE_APPROVED` or `ISSUE_REUSED` receipt. Alias statuses are not accepted as
approval. Ambiguous discovery, missing approval vocabulary,
unconfirmed creation, and policy-denied creation remain blockers.

A PR intent is rejected unless it names that confirmed approved issue. The
state CLI generates the PR body from the persisted issue URL and writes a
stable body marker and mandatory digest, preventing a copied, tampered, prefix,
or stale issue link. Effect and receipt must also share the exact PR number and
URL. The existing PR
helper queries the exact head/base pair before creation and records
`PR_CREATED_LABEL_PENDING` before label mutation. A retry must reconcile the
typed operation and discover the saved issue/PR identity; it must never assume
that a failed command had no remote effect or create a duplicate.

## Browser

The Playwright profile is expected to be authenticated by the user in advance. Agents never enter credentials or solve MFA. Only one client should use a persistent profile at a time. Every run derives or requires an absolute external artifact root before browser launch; the profile and MCP output directory remain beneath it, outside the repository, Git common directory, linked worktrees, and current working directory. State records only sanitized identity, response, stability, polling, recovery, and root metadata—not cookies, tokens, auth headers, session tokens, or raw profile contents.

## Permission-boundary caveat

OpenCode permission rules, prompt constraints, Git inspection, and subagent separation provide defense in depth; they are not an operating-system sandbox. A broadly allowed shell can invoke tools with side effects through many syntactic forms. Use Myrmex only inside repositories and accounts whose operating-system permissions are appropriate, keep production credentials out of the workspace, and retain human approval for delivery or destructive operations.


## Agent resolution

Before delegation, inspect the effective workspace and global agent definitions. A local
agent with the same name has precedence and is reported as `WARN_SHADOWED_AGENT`; with
`block_shadowed_agents` enabled it is a blocking condition until the definition is
reviewed. Unresolved models and disallowed provider prefixes are blocking conditions.
