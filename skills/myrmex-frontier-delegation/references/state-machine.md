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
waiting-for-delegations
collecting-delegation-results
consolidating-evidence
proceeding-to-next-gate
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
implementing -> waiting-for-delegations | verifying | blocked | failed
waiting-for-delegations -> collecting-delegation-results | blocked
collecting-delegation-results -> consolidating-evidence | blocked
consolidating-evidence -> proceeding-to-next-gate | blocked
proceeding-to-next-gate -> implementing | verifying | requesting-plan | blocked
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

Use typed `myrmex-state` domain commands for every phase/status/receipt
transition and `myrmex-state event` for compact audit events. Generic `patch`
is only for non-critical metadata; it cannot modify state-machine fields or
receipts. Persist every external effect in `pending_operations` as `intent →
observed effect → receipt → terminal confirmation` with a stable idempotency
key, then use `myrmex-state reconcile <run-id>` before recovery. `myrmex-state`
accepts only explicit
pairs: all work phases are `/active`; `blocked/blocked`, `dormant/dormant`,
`failed/failed`, `cancelled/cancelled`, and `superseded/superseded` are
terminal. Use `--expect-revision` for mutations and recovery.

Before a browser exchange, enforce the persisted execution route. A
`direct-only` route is a user lock: it forbids Frontier and delegation even
after compaction or resume.

`myrmex-state frontier <run-id> start` is the Frontier entry point: it records
the exchange intent before browser transport and enforces that direct-only
lock. `myrmex-state delegation-preflight` performs the equivalent pre-Task
identity/workspace persistence.

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

`myrmex-state complete` is a proof gate, not a generic phase change. It rejects
open WUs, incomplete batches, live delegations/corrections/exchanges or other
operations, an unevaluated continuous parent, and any explicit CI/tracking
issue/PR receipt that is failed, cancelled, unknown, or partial.
