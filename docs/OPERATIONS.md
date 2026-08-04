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
myrmex-state init --objective-file <file> --repository-root <repo> --artifact-root /absolute/external/run-root --mode autonomous --scope narrow
myrmex-state transition <run-id> --to-phase collecting-context --reason "context gathered" --expect-revision <n>
myrmex-state route set <run-id> --policy direct-only --authority user --request-id <request-id> --expect-revision <n>
myrmex-state reconcile <run-id>
myrmex-state frontier <run-id> start --request-id <request-id> --task-id <task-id> --expect-revision <n>
myrmex-state frontier <run-id> result --operation-id <op-...> --request-id <request-id> --message-id <message-id> \
  --transport-status success --frontier-decision ACCEPT --effect-json '{...}' --receipt-json '{...}' --expect-revision <n>
myrmex-state frontier <run-id> result --operation-id <op-...> --request-id <request-id> --message-id <message-id> \
  --transport-status success --frontier-decision ACCEPT --response-type plan --plan-json '{"work_unit_id":"WU-next"}' --expect-revision <n>
myrmex-state frontier <run-id> recover --operation-id <op-...> --request-id <request-id> --message-id <message-id> \
  --transport-status success --frontier-decision REMEDIATE --effect-json '{...}' --receipt-json '{...}' --expect-revision <n>
myrmex-state frontier <run-id> retry --operation-id <failed-op-...> --request-id <same-request-id> \
  --pre-effect-absence-proven --expect-revision <n>
myrmex-state delegation-preflight <run-id> --agent myrmex-worker --role writer --reason "bounded work" --task-id <task-id> --work-unit-id WU-03 --workspace <repo> --expect-revision <n>
myrmex-state operation <run-id> intent --kind pull_request --idempotency-key <stable-key> --intent-json '{"required":true}' --expect-revision <n>
myrmex-state operation <run-id> observe --operation-id op-... --effect-json '{...}' --expect-revision <n>
myrmex-state operation <run-id> receipt --operation-id op-... --receipt-json '{...}' --expect-revision <n>
myrmex-state operation <run-id> confirm --operation-id op-... --status confirmed --reason "receipt verified" --expect-revision <n>
myrmex-state operation <run-id> supersede --operation-id <failed-op-...> \
  --successor-operation-id <confirmed-op-...> --reason PRE_EFFECT_FAILURE --expect-revision <n>
myrmex-state operation <run-id> abandon --operation-id <failed-op-...> \
  --pre-effect-absence-proven --reason PRE_EFFECT_FAILURE --expect-revision <n>
myrmex-state work-unit <run-id> complete --work-unit-id WU-03 --evidence-json '{...}' --expect-revision <n>
myrmex-state pause <run-id> --reason "pause requested" --expect-revision <n>
myrmex-state resume <run-id> --expect-revision <n>
myrmex-state cancel <run-id> --reason "parent cancelled" --cancellation-type PARENT_OBJECTIVE_CANCELLED --expect-revision <n>
myrmex-state correction start <run-id> --work-unit-id WU-03 --task-id <task-id> --workspace <repo> --reason "fix verifier findings" --source-request-id <verification-request-id> --defect-revision <n> --scope-digest <sha256> --source-candidate-sha <sha> --expect-revision <n>
myrmex-state correction authorize <run-id> --work-unit-id WU-03 --authority frontier --request-id <request-id> --verification-request-id <verification-request-id> --defect-revision <n> --scope-digest <sha256> --source-candidate-sha <sha> --max-additional-attempts 1 --expect-revision <n>
```

### Bounded local commit

An autonomous local commit is not a global Git permission. A continuous parent
first records a standing human authorization from an accepted Frontier work
unit. The typed command changes the effective policy to `governed`; it does not
create a commit grant:

```bash
myrmex-state commit-policy authorize <parent-run-id> --authority user \
  --source-request-id <frontier-request-id> --source-operation-id <op-id> \
  --work-unit-id <accepted-wu> --expect-revision <n>
```

The accepted evidence must be a persisted, confirmed Frontier
`SUB_OBJECTIVE_COMPLETE`/`ACCEPT` (or an exact equivalent accepted-WU record);
an accepted `plan` response alone is insufficient. The
standing record retains the human authority, source request, parent scope,
creation timestamp, accepted WU identity, and append-only audit history.

Only then may the primary create one exact `local_commit` grant. The grant is
bound to the parent run, accepted WU, target workspace/repository, target
branch, expected HEAD, candidate diff SHA-256, exact normalized paths, exact
message, and `max_uses=1`:

```bash
myrmex-state authorization <parent-run-id> create --authority user \
  --request-id <grant-request-id> --source-request-id <frontier-request-id> \
  --source-operation-id <op-id> --work-unit-id <accepted-wu> \
  --repository-root <target-wu-workspace> --branch <target-branch> \
  --expected-head <sha> --candidate-diff-sha <sha256> \
  --allowed-path path/to/file --message "<message>" \
  --expires-at <iso-timestamp> --expect-revision <n>
scripts/myrmex-git-local.py commit --run-id <run-id> \
  --authorization-id <grant-id> --repository-root <target-wu-workspace>
```

`deny` rejects ordinary local-commit authorization. Governed mode alone cannot
commit without the exact accepted-WU grant. The parent repository root and
branch are never rewritten; the target WU workspace may differ only when its
persisted accepted identity matches the grant. Parent cancellation or
completion revokes future grant creation. Replaying the same standing
authorization or grant request is a byte-stable no-op; conflicting identity
fails before mutation. Generic `patch` cannot change standing authority, grant
identity, or consumption.
The accepted source effect and receipt must also carry the exact target root,
branch, expected HEAD, candidate digest, allowed paths, and commit message;
any source-scope mismatch rejects the grant before it is persisted.

The helper stages only the listed paths, rejects any pre-existing index state
(including intent-to-add), any dirty `.atl/` or `.playwright-mcp/` path, other
protected paths, unrelated dirty paths, merge/amend/push behavior, and
interactive commit input. It never invokes hooks, filters, editors, signing,
or transport: a private index plus `hash-object --no-filters`, `write-tree`,
`commit-tree`, and compare-and-swap `update-ref` are used instead. Raw branch,
tag, remote, ref-namespace, HEAD, and configuration snapshots are checked
before authorization consumption. The
grant is intentionally single-use (`max_uses=1`). It persists
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

### Tracking issue and draft PR delivery

Issue #4 delivery is an ordered protocol. Validation uses fake/local helpers
only; neither the policy resolver nor the tests call live GitHub.

1. Resolve policy before any GitHub effect. The resolver is read-only and fails
   closed for malformed policy, ambiguous matches, unavailable approval labels,
   and unapproved creation:

   ```bash
   scripts/resolve-delivery-policy.py --repository-root <repo> --mode autonomous \
     > <external-run-root>/delivery-policy.json
   ```

2. Create the issue body and receipt paths under the external run artifact root,
   then persist a typed `tracking_issue` intent. The intent must retain the
   resolved policy, `repo`, `title`, `body_file`, `receipt_file`, `objective_id`,
   `scope_digest`, `approval_marker`, all effective policy booleans,
   `creation_policy`, `ensure_approval`, the resolved `policy_digest`, and
   the stable identity marker. The state CLI verifies the policy digest,
   effective fields, decision, run mode, repository root, and exact resolver
   inputs by recomputing the side-effect-free resolver before accepting the
   intent. A self-consistent forged policy is not sufficient. Use a stable
   idempotency key such as
   `tracking-issue:<objective-id>:<scope-digest>`:

   ```bash
   myrmex-state operation <run-id> intent --kind tracking_issue \
     --idempotency-key tracking-issue:<objective-id>:<scope-digest> \
     --intent-json '<resolved tracking issue intent JSON>' --expect-revision <n>
   ```

3. Invoke `scripts/github-tracking-issue-recovery.py` with the resolved
   `creation_policy`, `approval_marker`, and `reuse_matching_approved` policy.
   Pass `--ensure-approval` when the delivery gate requires an approved issue.
   The helper discovers the exact stable marker before creating anything and
   writes `ISSUE_CREATED_APPROVAL_PENDING` before attempting a label effect.

4. Persist the helper's JSON artifact as both the observed effect and receipt,
   then confirm only the canonical helper statuses `ISSUE_APPROVED` or
   `ISSUE_REUSED`. The confirmed receipt's
   repository, issue number, URL, approval marker, and identity marker must
   match the typed intent:

   ```bash
   myrmex-state operation <run-id> observe --operation-id <issue-op> \
     --effect-json '<issue-recovery JSON>' --expect-revision <n>
   myrmex-state operation <run-id> receipt --operation-id <issue-op> \
     --receipt-json '<issue-recovery JSON>' --expect-revision <n>
   myrmex-state operation <run-id> confirm --operation-id <issue-op> \
     --status confirmed --reason "approved issue receipt verified" --expect-revision <n>
   ```

5. Generate the PR body from that confirmed identity; do not hand-copy an issue
   number. The command is pure with respect to run state and writes an atomic
   body containing the exact persisted issue URL and a stable marker:

   ```bash
   myrmex-state delivery <run-id> pr-body \
     --tracking-operation-id <issue-op> --template-file <template.md> \
     --output-file <external-run-root>/pr-body.md
   ```

6. Persist a typed `pull_request` intent containing `repo`, `head`, `base`,
   `title`, generated `body_file`, `receipt_file`, and
   `tracking_issue_operation_id`, plus the generated body's mandatory
   `body_digest`. The CLI rejects an unconfirmed issue, a mismatched issue URL,
   a missing digest, a prefix/stale issue URL, or a body that does not contain
   an exact canonical issue URL token or generated marker. The digest is
   rechecked when an effect/receipt is recorded and when confirmation runs.

7. Invoke the existing `scripts/github-pr-recovery.py` with the intent's exact
   head/base/body/receipt paths. It queries the matching open PR before create,
   writes `PR_CREATED_LABEL_PENDING` immediately after creation, and never
   pushes a branch.

8. Persist its artifact through `operation observe` and `operation receipt`,
   then confirm only the canonical `PR_CREATED` receipt, and the effect and
   receipt must carry the same exact PR number and URL. A failed label step is
   resumed through discovery and the narrow label fallback; it is never a
   reason to run `gh pr create` again.

9. On interruption, run `myrmex-state reconcile <run-id>` first. For a pending
   tracking issue or PR it returns a single reconcile action. Reuse the saved
   operation ID, body, receipt, stable marker, and exact remote identity. This
   makes resume idempotent without duplicate issue or PR creation.

Frontier responses keep transport status separate from the substantive decision
and record an `effect_stage`: `none`, `transport_started`, `request_sent`, or
`response_observed`. A failed exchange can be retried with the same request ID
only when the caller explicitly proves `none` (no browser tab, outbound
request, or message identity). The retry creates a linked successor; after the
successor is confirmed, `operation supersede` closes the old record without
deleting its evidence. `operation abandon` is the equivalent terminal
resolution when no successor is needed.

Transport errors, timeouts, malformed responses, request mismatches, and
response-identity mismatches with a possible external effect remain recovery
blockers. `frontier recover` no longer requires an original `message_id`; it
can attach a later confirmed response identity to a failed operation. An exact
replay is a byte-stable no-op; conflicting identity/payload or an unexpected
revision is rejected without mutation. Failed, abandoned, and superseded
historical records do not block completion once they have a typed resolution.
The generic `operation observe/receipt/confirm` lifecycle cannot confirm a
Frontier exchange unless those same typed fields and identities are present.

Correction capacity is scoped to each work unit: `--max-corrections-per-work-unit`
defaults to two, while `--max-total-corrections` is an optional, independent
run-wide cost ceiling. A correction records its work-unit ID, verification
request ID, defect revision, defect-scope digest, and source candidate SHA. A
correction grant is bound to that complete identity and can be consumed once.
When a work unit exhausts its base capacity, only `correction authorize` can
add the exact bounded number of attempts and clear that matching blocker; an
older grant cannot replace a newer blocker or authorize another candidate,
verification cycle, work unit, or scope, and it cannot clear the run-wide
ceiling.

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

For `scope=continuous`, `work-unit complete` keeps the parent active and
creates a typed `REQUEST_PARENT_GATE` reconcile action. A confirmed Frontier
gate with one `<proposed_plan>` stores the exact next-WU intent and its request,
message, effect, and receipt provenance; it derives `BEGIN_NEXT_WORK_UNIT` and
does not permit a second active WU. Only the exact typed response
`PARENT_OBJECTIVE_COMPLETE` satisfies the parent gate. `SUB_OBJECTIVE_COMPLETE`,
generic success receipts, and `OBJECTIVE_COMPLETE` do not terminate the
parent. `BLOCKING_CLARIFICATION` and explicit pause preserve a typed resume
phase/action. Informational status messages are side-band and do not mutate
the state or interrupt the reconciled action.

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

### Governed procedural learning

Procedural experiments use `myrmex.procedural-experiment/v1` in a separate
append-only namespace below the configured local memory root. They do not alter
`myrmex-state`, ordinary semantic memory, the active installation, or Git.
Only the primary writes durable records; child agents return candidate metadata
for primary review.

The required lifecycle is `proposed`, `isolated_candidate`, `tests_passed`,
`verifier_passed`, `bounded_trial_active`, then `promoted` or `reverted`.
Pre-terminal stages can be `rejected`. Every mutation supplies a request ID,
expected revision, authority identity, and primary writer. Replays with the
same request and payload are byte-stable no-ops; conflicting request reuse,
stale revisions, invalid transitions, and failed gates are rejected before
append.

Proposals require a weakness, expected benefit, target paths, evidence with at
least one run/commit/artifact/verifier/Frontier identity, explicit rollback
artifact, authority, version, kind, and risk class. Candidate artifacts are
digest-only and must be child-agent output from disposable isolation with
changed paths contained by the proposal. Tests and independent verifier
receipts must pass before activation. A trial must be bounded by a positive
`max_runs`, `max_work_units`, or expiry. Successful outcomes require
Frontier/human promotion authority and matching candidate/evidence identities;
regression or inconclusive outcomes require rollback evidence. Core-control
trials require elevated human authority plus a matching Frontier request.
Installation records must be sanitized/generalizable, and collective/global
scope is never accepted.

```bash
myrmex-memory procedural list --repository-root <repo>
myrmex-memory procedural show <experiment-id> --repository-root <repo>
```

## Draft PR recovery

Keep GitHub create and label operations separate. A confirmed approved
`tracking_issue` operation and a body generated by `myrmex-state delivery` are
prerequisites for the typed `pull_request` intent. Invoke
`scripts/github-pr-recovery.py` only after that intent is persisted, then store
its artifact through `operation observe`, `operation receipt`, and
`operation confirm`. The helper deliberately no longer writes `myrmex-state`
through a revision-less generic patch. It writes `PR_CREATED_LABEL_PENDING`
immediately after creation and can use the issue-label REST fallback when
`gh pr edit --add-label` lacks Projects scope, without creating a duplicate PR.

## Updating

Re-run `install.sh` from a newer package. Existing Myrmex files and the state binary are timestamped into backups before replacement. Unrelated files remain untouched.
