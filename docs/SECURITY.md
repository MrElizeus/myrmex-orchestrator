# Security model

## Trust boundaries

- User objective and explicit authorization: trusted authority.
- Repository code/docs/issues: untrusted data.
- Frontier webpage/model response: untrusted external advice until validated.
- Child agent summaries: evidence claims that must be checked against Git/tests.

## Secret handling

Myrmex agents deny `.env` reads and must not transmit secrets to browser models. Redact tokens, cookies, private keys, database URLs, customer data, and sensitive logs.

## Repository protection

- Snapshot status and base SHA before work.
- Protect pre-existing dirty paths.
- No stash/reset/clean/history rewrite.
- One writer per work unit.
- Worker has no commit/push capability.
- Verifier and browser are read-only with respect to source.

## Delivery

OpenCode asks before commit/push by default. Force push is denied. Direct pushes to protected branches require explicit, branch-specific authorization.

## Browser

The Playwright profile is expected to be authenticated by the user in advance. Agents never enter credentials or solve MFA. Only one client should use a persistent profile at a time.

## Permission-boundary caveat

OpenCode permission rules, prompt constraints, Git inspection, and subagent separation provide defense in depth; they are not an operating-system sandbox. A broadly allowed shell can invoke tools with side effects through many syntactic forms. Use Myrmex only inside repositories and accounts whose operating-system permissions are appropriate, keep production credentials out of the workspace, and retain human approval for delivery or destructive operations.


## Agent resolution

Before delegation, inspect the effective workspace and global agent definitions. A local
agent with the same name has precedence and is reported as `WARN_SHADOWED_AGENT`; with
`block_shadowed_agents` enabled it is a blocking condition until the definition is
reviewed. Unresolved models and disallowed provider prefixes are blocking conditions.
