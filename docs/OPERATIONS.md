# Operating Myrmex

## Rollout

1. Install without changing the default agent.
2. Restart OpenCode and run `/myrmex-doctor`.
3. Test DIRECT on a trivial non-critical change.
4. Test DELEGATED on a bounded multi-layer task.
5. Test verifier failure and up to two bounded correction resumes.
6. Test FRONTIER interactive on a narrow objective with push disabled.
7. Test FRONTIER autonomous and interruption/recovery using `myrmex-state` plus memory continuity.
8. Use it on a real bounded task.
9. Set as default only after stable results.

## Suggested prompts

DIRECT:

```text
Fix this bounded issue using the smallest safe workflow. Do not use frontier.
```

DELEGATED:

```text
Map the relevant subsystem, delegate one bounded implementation work unit, and independently verify it. Do not commit or push.
```

FRONTIER interactive:

```text
Use frontier delegation in interactive mode for this narrow objective. Show me the grounded plan before implementation. Do not push.
```

FRONTIER autonomous:

```text
Use autonomous frontier delegation for this narrow objective. Continue through implementation and frontier validation without approval prompts, but stop for login, credentials, product decisions, destructive actions, or push authorization.
```

## State operations

```bash
myrmex-state doctor
myrmex-state list
myrmex-state show <run-id>
myrmex-state transition <run-id> --to-phase collecting-context --reason "context gathered" --expect-revision <n>
myrmex-state route set <run-id> --policy direct-only --authority user --request-id <request-id> --expect-revision <n>
myrmex-state reconcile <run-id>
myrmex-state frontier <run-id> start --request-id <request-id> --task-id <task-id> --expect-revision <n>
myrmex-state delegation-preflight <run-id> --agent myrmex-worker --role writer --reason "bounded work" --task-id <task-id> --work-unit-id WU-03 --workspace <repo> --expect-revision <n>
myrmex-state operation <run-id> intent --kind pull_request --idempotency-key <stable-key> --intent-json '{"required":true}' --expect-revision <n>
myrmex-state operation <run-id> observe --operation-id op-... --effect-json '{...}' --expect-revision <n>
myrmex-state operation <run-id> receipt --operation-id op-... --receipt-json '{...}' --expect-revision <n>
myrmex-state operation <run-id> confirm --operation-id op-... --status confirmed --reason "receipt verified" --expect-revision <n>
myrmex-state work-unit <run-id> complete --work-unit-id WU-03 --evidence-json '{...}' --expect-revision <n>
myrmex-state correction start <run-id> --work-unit-id WU-03 --task-id <task-id> --workspace <repo> --reason "fix verifier findings" --source-request-id <request-id> --scope-digest <sha256> --source-candidate-sha <sha> --expect-revision <n>
myrmex-state correction authorize <run-id> --work-unit-id WU-03 --authority frontier --request-id <request-id> --scope-digest <sha256> --source-candidate-sha <sha> --max-additional-attempts 1 --expect-revision <n>
```

### Bounded local commit

An autonomous local commit is not a global Git permission. The primary first
creates a `local_commit` authorization while the persisted run has
`commit_policy=authorized`; the authorization records the authority, stable
request ID, repository/branch/expected HEAD, exact normalized paths, commit
message digest, expiry, and a one-use budget:

```bash
myrmex-state authorization <run-id> create --authority frontier \
  --request-id <request-id> --repository-root <repo> --branch <branch> \
  --expected-head <sha> --allowed-path path/to/file --message "<message>" \
  --expires-at <iso-timestamp> --expect-revision <n>
scripts/myrmex-git-local.py commit --run-id <run-id> \
  --authorization-id <auth-id> --repository-root <repo>
```

The helper stages only the listed paths, rejects any pre-existing index state
(including intent-to-add), any dirty `.atl/` or `.playwright-mcp/` path, other
protected paths, unrelated dirty paths, merge/amend/push behavior, and
interactive commit input. It never invokes hooks, filters, editors, signing,
or transport: a private index plus `hash-object --no-filters`, `write-tree`,
`commit-tree`, and compare-and-swap `update-ref` are used instead. Raw branch,
tag, remote, ref-namespace, HEAD, and configuration snapshots are checked
before authorization consumption. The
authorization is intentionally single-use (`max_uses=1`). It persists
`local_commit` intent before invoking Git, then
records observed commit, receipt, terminal confirmation, and authorization
consumption. On retry it searches for the one matching child of the expected
HEAD before attempting Git again; this is the crash boundary that prevents a
duplicate commit. Direct `git commit` remains subject to the normal permission
gate, and this authorization never permits push or remote mutation.

`myrmex-state reconcile` is the mandatory first resume step: it is pure and
returns one safe action before any external retry. Persist every external
effect as `intent → observed effect → receipt → terminal confirmation` with
`myrmex-state operation`; the stable kind/idempotency key makes retries safe.
`frontier start` enforces the persisted direct-only policy before browser
transport, and `delegation-preflight` requires an accessible workspace while
recording the WU/task identity before a Task launch. `myrmex-state patch` is
limited to non-critical metadata only. It cannot change phase, status,
blockers, execution or Git policy, receipts, budgets/counters, work units,
remediation authority, pending operations, or delegation identity.

Correction capacity is scoped to each work unit: `--max-corrections-per-work-unit`
defaults to two, while `--max-total-corrections` is an optional, independent
run-wide cost ceiling. A correction records its work-unit ID, source request,
defect-scope digest, and source candidate SHA. When a work unit exhausts its
base capacity, only `correction authorize` can add the exact bounded number of
attempts and clear that matching blocker; it cannot clear a different work
unit's blocker or the run-wide ceiling.

Resume in OpenCode:

```text
/myrmex-resume <run-id>
```

The resume flow reconciles local state, memory, Git, and browser state before repeating any side effect.

For a completed finite fan-out, resume from the persisted delegation batch rather
than asking the user to continue. Myrmex records task IDs, waits for every final
result, consolidates evidence once, and advances to the next gate once. A missing
result gets one safe recovery attempt before `BLOCKED_MISSING_DELEGATION_RESULT`.

Successful completion uses:

```bash
myrmex-state complete <run-id> --message "objective complete" --unlock-owner <owner> --expect-revision <n>
```

This persists `dormant/dormant` and the completion event before releasing the
run lock. Completion rejects open work units, incomplete batches, active
delegations/corrections/exchanges, pending operations, an unevaluated parent
objective, and any explicit CI/tracking-issue/PR receipt that is failed,
cancelled, unknown, or partial. New runs use `myrmex.frontier-state/v2`. `myrmex-state migrate
<run-id> --expect-revision <n>` explicitly upgrades a v1 run after writing an
exact `state.v1.r<n>.json` backup. Historic run-global correction counts remain
under `work_units.__legacy__`; migration never invents a work-unit assignment.

## Project and installation memory

Native project memory is optional, local, and evidence-backed; it is not a
replacement for `myrmex-state` run transitions or receipts. The primary may
record a candidate and promote it only after checking a digest-addressed,
repository-contained evidence file:

```bash
myrmex-memory candidate create \
  --repository-root <repo> --authority primary --kind architecture-invariant \
  --claim "<specific falsifiable claim>" --confidence 0.95 \
  --evidence-json '[{"kind":"verification-receipt","path":"<relative-path>","digest":"sha256:<digest>"}]' \
  --request-id <request-id>
myrmex-memory promote <memory-id> --repository-root <repo> \
  --authority primary --request-id <request-id> --expect-revision 0
myrmex-memory search --repository-root <repo> --query "<terms>"
```

Candidates are hidden from normal retrieval. Promotion and refutation require
accessible evidence; `refute` revokes or supersedes a record without deleting
its JSONL audit history.

To turn a verified project lesson into a reusable **local installation** lesson,
the primary must deliberately rewrite and sanitize it. Do not reuse a
project-private claim or let a source proof's path/run/WU/commit/request IDs
cross the boundary:

```bash
myrmex-memory installation promote <project-memory-id> \
  --repository-root <repo> --source-expect-revision <revision> \
  --sanitized-claim "<generic lesson>" \
  --sanitization-reason "<what was removed>" \
  --sanitized-evidence-json '[{"kind":"verification-receipt","path":"<relative-proof>","digest":"sha256:<digest>"}]' \
  --applicability-json '{"tool_version_range":">=1.0,<2.0","model":"<model>"}' \
  --authority primary --request-id <request-id>
```

`--scope auto` retrieves project records first, then applicable installation
records. Tool/model mismatch, `ttl_seconds`, and `half-life:<seconds>` or
`linear:<seconds>` decay lower retrieval priority before any deletion. A read
does not reinforce a record. To refresh a lesson, use `confirm` with fresh
evidence and an explicit demonstrated-usefulness statement; its audit event
resets freshness but never increases confidence automatically.

Use `myrmex-memory metric record` for normalized work-unit outcome,
correction/defect evidence, verification, recovery, and test data. Metrics are
installation-local, evidence-sanitized, and separate from `myrmex-state`; they
hash WU/run/request labels and accept normalized recovery/test categories
instead of project commands or paths. They cannot transition a run or modify
policy. There is no cross-installation sharing or model training. If native
memory or optional Engram fails, continue safe local work when persistence is
not itself required and report `memory: degraded` rather than claiming a
receipt.

## Draft PR recovery

Keep GitHub create and label operations separate. Record a typed
`pull_request` operation intent before invoking `scripts/github-pr-recovery.py`,
then store its artifact record through `operation observe`, `operation receipt`,
and `operation confirm`. The helper deliberately no longer writes
`myrmex-state` through a revision-less generic patch. It writes
`PR_CREATED_LABEL_PENDING` immediately after creation and can use the
issue-label REST fallback when `gh pr edit --add-label` lacks Projects scope,
without creating a duplicate PR.

## Updating

Re-run `install.sh` from a newer package. Existing Myrmex files and the state binary are timestamped into backups before replacement. Unrelated files remain untouched.
