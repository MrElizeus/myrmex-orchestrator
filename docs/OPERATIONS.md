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

## Updating

Re-run `install.sh` from a newer package. Existing Myrmex files and the state binary are timestamped into backups before replacement. Unrelated files remain untouched.
