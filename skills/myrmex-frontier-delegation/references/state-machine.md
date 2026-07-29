# Frontier state machine

## Scope

- `narrow`: one bounded objective. Exact objective completion ends the run.
- `continuous`: explicitly requested parent objective. Each plan is a sub-objective; after sub-objective completion run one parent gate.

Ambiguous autonomous scope defaults to `narrow`.

## Phases

```text
initializing
collecting-context
requesting-plan
waiting-for-frontier
reviewing-plan
implementing
verifying
reporting
parent-gate
committing
pushing
blocked
dormant
failed
cancelled
superseded
```

Normal transitions:

```text
initializing -> collecting-context
collecting-context -> requesting-plan
requesting-plan -> waiting-for-frontier
waiting-for-frontier -> collecting-context | reviewing-plan | blocked | dormant
reviewing-plan -> implementing | blocked
implementing -> verifying | blocked | failed
verifying -> implementing | reporting | blocked | failed
reporting -> waiting-for-frontier
waiting-for-frontier -> parent-gate        # continuous sub-objective complete
parent-gate -> waiting-for-frontier | blocked | dormant
waiting-for-frontier -> committing | dormant  # narrow/parent complete
committing -> pushing | dormant | blocked
pushing -> dormant | blocked
```

An explicit user cancellation becomes `cancelled`. A changed objective becomes `superseded`; create a new run instead of reusing its plan.

## Initialization

Save the exact objective to a local artifact, then:

```bash
myrmex-state init \
  --objective-file <objective-file> \
  --repository-root <repo> \
  --mode interactive|autonomous \
  --scope narrow|continuous \
  --branch <branch> \
  --base-sha <sha> \
  --commit-policy deny|ask|authorized \
  --push-policy deny|ask|authorized
```

Add each protected dirty path with `--protected-dirty-path`. Persist the returned run ID and acquire `myrmex-state lock <run-id> --owner <stable-session-owner>` before side effects.

Use `myrmex-state patch` for every phase/status/receipt transition and `myrmex-state event` for compact audit events. Prefer `--expect-revision` when recovery or concurrency is possible.

## Interactive gates

Interactive mode pauses:

1. After a valid initial plan, before implementation.
2. After execution/verification, before frontier validation.
3. Before commit and push unless separately authorized.
4. On any material human decision.

Autonomous mode continues technical phases but still blocks for login/MFA, credentials, unsafe dirty overlap, destructive-risk choices, product decisions, or exhausted budgets.

## Completion

For narrow scope, `OBJECTIVE_COMPLETE` or `OBJECTIVE_ALREADY_COMPLETE` can proceed to delivery/dormant.

For continuous scope, `SUB_OBJECTIVE_COMPLETE` triggers exactly one `assets/parent-objective-gate.md` exchange. A new plan continues; `PARENT_OBJECTIVE_COMPLETE` ends. Never ask a generic “anything else?”.
