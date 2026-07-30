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
```

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
myrmex-state complete <run-id> --message "objective complete" --unlock-owner <owner>
```

This persists `dormant/dormant` and the completion event before releasing the
run lock. Legacy `dormant/active` runs can be normalized safely with
`myrmex-state migrate <run-id>`.

## Draft PR recovery

Keep GitHub create and label operations separate. When a draft PR may have been
created before a later command failed, query the exact head/base pair first and
use `scripts/github-pr-recovery.py`. It writes `PR_CREATED_LABEL_PENDING`
immediately after creation and can use the issue-label REST fallback when
`gh pr edit --add-label` lacks Projects scope, without creating a duplicate PR.

## Updating

Re-run `install.sh` from a newer package. Existing Myrmex files and the state binary are timestamped into backups before replacement. Unrelated files remain untouched.
