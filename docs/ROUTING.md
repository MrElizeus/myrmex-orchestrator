# Routing policy

## DIRECT

Prefer DIRECT when:

- objective and expected behavior are clear;
- repository context is local and coherent;
- no business/product decision is missing;
- change is reversible;
- risk to data, auth, billing, legal content, or public contracts is low;
- focused verification is available.

File count alone does not disqualify DIRECT.

### Persisted `direct-only`

For a long-running or resumable run, an explicit user instruction such as
“resolve it yourself” or “do not delegate” is persisted as `direct-only`:

```bash
myrmex-state route set <run-id> \
  --policy direct-only --authority user --request-id <request-id> \
  --expect-revision <n>
```

It locks the effective route to DIRECT, rejects new delegation batches and
Frontier requests with `BLOCKED_DELEGATION_FORBIDDEN_BY_EXECUTION_POLICY`, and
survives resume/migration. Tests, linters, type checks, builds, diff review,
and ordinary Git safety gates remain available. Switching into this policy is
rejected while child tasks, an incomplete batch, a pending Frontier exchange,
or a pending external operation still needs reconciliation. The legacy spelling
`myrmex-state route <run-id> set ...` remains accepted for persisted runbooks.

## DELEGATED

Prefer DELEGATED when:

- broad mapping would inflate the primary context;
- the subsystem is unfamiliar;
- the change crosses coherent layers;
- implementation is lengthy;
- independent verification materially reduces risk;
- the user explicitly asks for delegation.

Normal delegated flow is at most one explorer, one worker, one verifier, and up to two bounded correction resumes.

## FRONTIER

Use only with explicit frontier intent. It is suitable for:

- architectural planning grounded in a real repository;
- long autonomous bounded objectives;
- a planning/validation loop where the frontier model is intentionally authoritative;
- continuation of a saved frontier session.

FRONTIER is not a reason to send the whole repository to the browser. Send a targeted context pack or use verified direct repository access.

## BLOCKED

Block only for material uncertainty that repository inspection cannot resolve, unavailable credentials/login, destructive production action, unsafe repository state, missing required capability, or an exhausted recovery budget.

Size is a soft limit: <=400 is normal, larger cohesive work needs a complete exception, and separable work is split.

## Deterministic route and model decisions

Before an agent/provider invocation, P1 route and model selection is an explicit,
read-only policy decision. `scripts/myrmex_route_model_policy.py` binds the
decision to one WorkUnit and immutable plan revision, then evaluates configured
options in `priority_asc`, `option_id_asc` order. It does not perform learned or
adaptive scoring.

Every decision records:

- the effective versioned policy and digest;
- the complete policy-ordered option set;
- allowlist, required-route, and role-compatibility rejection reasons;
- explicit agent, provider, and model availability supplied by the caller;
- budget headroom versus each option's maximum estimated cost;
- the selected route, agent, provider, and model, or a typed blocker.

An explicitly configured later option may be selected only after earlier
options carry visible rejection reasons. That is deterministic policy traversal,
not a silent fallback. Missing availability fails closed. The resolver never
converts missing availability into `PROVIDER_INVOCATION_FAILED`; that status is
reserved for an actual invocation failure.

Credential visibility is deliberately not an input. The policy does not inspect
environment variables, request a new credential, or infer transport readiness
from orchestrator-visible secrets. Transport/provider availability must be
supplied explicitly, while credentials remain owned by OpenCode's transport.

An example configurable policy is shipped at
`profiles/route-model-policy-example.json`. A decision can be previewed with:

```bash
python3 scripts/myrmex_route_model_policy.py \
  --request request.json \
  --policy profiles/route-model-policy-example.json \
  --availability availability.json \
  --require-selected
```

The resulting `myrmex.route-model-decision/v1` artifact grants no dispatch,
agent invocation, provider invocation, repository-write, commit, or push
authority.
