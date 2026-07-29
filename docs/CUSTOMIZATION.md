# Customization

## Provider and model policy

The default profile requires a resolved model for delegated agents and allows only
model identifiers whose provider prefix is listed in `agent_policy.allowed_provider_prefixes`:

```json
{
  "agent_policy": {
    "allowed_provider_prefixes": ["openai/"],
    "require_resolved_model_for_delegation": true,
    "block_shadowed_agents": true
  }
}
```

Myrmex never silently falls back. Diagnostics use `BLOCKED_UNRESOLVED_AGENT_MODEL`
or `BLOCKED_NON_ALLOWED_PROVIDER`. Add another provider explicitly in a local
`myrmex.json` only if your deployment requires it.

## Commit and push

The default agent uses `ask` for both. To allow autonomous commits while retaining push approval, change only:

```yaml
permission:
  bash:
    "git commit*": allow
    "git push*": ask
```

Do not allow force push.

## Project-local installation

For a single repository, copy agents/skills under `.opencode/` instead of global config.
Myrmex reports local-over-global shadowing so an older workspace definition cannot be
mistaken for the effective global agent.
