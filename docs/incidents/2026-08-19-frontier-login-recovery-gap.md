# Frontier login recovery gap — 2026-08-19

## Context

During a supervised `frontier-gated` production campaign for `MrElizeus/investanddream-platform`, a Myrmex run reached:

```text
status=blocked
phase=blocked
blocker=BLOCKED_FRONTIER_LOGIN_REQUIRED
```

The original Frontier operation was:

```text
op-bcab593c708198d8db3cbf3f
```

Authentication was restored externally, but the persisted run could not continue through the typed recovery path.

Observed reconciliation result:

```text
BLOCKED_STATE_INCOMPLETE
```

Observed typed recovery result:

```text
RECOVERY_CANNOT_CLEAR_UNRELATED_BLOCKER
```

No generic `patch` was used. No new run was created automatically. No duplicate Frontier exchange, code modification, commit, deploy, or production write occurred.

## Root cause

The current typed Frontier recovery gate only considers these blocker codes recoverable:

```text
BLOCKED_FRONTIER_RECOVERY_MISSING_ORIGINAL_IDENTITY
BLOCKED_FRONTIER_PRE_EFFECT_FAILURE
BLOCKED_FRONTIER_RECOVERY
```

`BLOCKED_FRONTIER_LOGIN_REQUIRED` is not included, so `recovery resolve-frontier` correctly refuses to clear it even after the external authentication condition has been resolved.

The generic `resume` transition is also intentionally restricted to explicit pause or blocking clarification, so it cannot clear a login blocker either.

This leaves a stale authentication blocker without a domain-specific supported transition.

## Operational workaround

For the affected campaign:

1. preserve the blocked run as audit evidence;
2. if the failed Frontier operation has provable pre-effect absence, terminalize that operation with the typed `operation abandon` transition using explicit pre-effect proof;
3. do not attempt to clear the run blocker with generic state mutation;
4. create a new explicitly authorized run for the same campaign/objective;
5. preserve predecessor/run continuity in campaign evidence when supported;
6. restart from the relevant work unit without replaying the failed external operation.

## Desired behavior

Add an explicit typed authentication-recovery transition instead of broadening generic patch/resume behavior.

Possible contract:

```text
myrmex-state frontier-auth resolve RUN_ID \
  --operation-id <failed-frontier-op> \
  --evidence-json '{"authentication_restored":true,...}' \
  --resume-phase <active-phase> \
  --expect-revision N
```

or an equivalent typed recovery disposition.

Preconditions should include:

- current blocker is exactly `BLOCKED_FRONTIER_LOGIN_REQUIRED`;
- authentication restoration evidence is explicit and sanitized;
- unresolved Frontier operations are either safely terminalized or proven compatible with retry;
- no duplicate outbound request is produced as a side effect of the state transition;
- the run resumes only to an active phase permitted by the original route;
- replay is idempotent;
- unrelated blocker types cannot be cleared.

## Acceptance criteria

- [ ] A run blocked by `BLOCKED_FRONTIER_LOGIN_REQUIRED` can resume after authentication is restored through a typed transition.
- [ ] Generic `patch` remains unable to clear `status`, `phase`, or `blocker`.
- [ ] Generic `resume` remains limited to its existing explicit pause/clarification semantics unless deliberately redesigned.
- [ ] Authentication recovery cannot clear another blocker type.
- [ ] A pending failed Frontier operation must be resolved safely before the run becomes active.
- [ ] Proven pre-effect failures can retry without duplicate Frontier effects.
- [ ] Unknown-effect failures remain blocked for discovery/reconciliation.
- [ ] Recovery evidence is sanitized and persisted.
- [ ] Replaying the same authentication-recovery action is idempotent.
- [ ] Regression tests cover login-required → authentication restored → safe resume.
