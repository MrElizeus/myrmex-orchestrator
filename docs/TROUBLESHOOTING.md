# Troubleshooting

## Agent does not appear

- Confirm the file exists at `~/.config/opencode/agents/myrmex-orchestrator.md`.
- Restart OpenCode.
- Run `scripts/verify-install.sh`.
- Check YAML frontmatter syntax and config directory selection.

## Frontier tools are unavailable

- Check the browser MCP entry in `opencode.json`.
- Confirm Node 18+ and `npx`.
- Restart OpenCode after MCP changes.
- Ensure the MCP process can launch the configured browser executable.

## Browser profile is locked

Close other instances using the same `--user-data-dir`, or configure a separate profile. Do not delete the profile unless you are prepared to authenticate again.

## Frontier parser returns unknown

- Confirm the frontier echoed the expected request ID.
- Inspect the raw latest assistant text.
- Verify the page still exposes assistant-message metadata.
- Update the DOM helper if the UI changed; do not fall back to whole-page marker searches.

## Local state unavailable

Run `myrmex-state doctor`. Confirm `~/.local/bin` is on PATH and the XDG state directory is writable. Frontier mode must not start without healthy local state.

## Memory unavailable

Normal and frontier work may continue when local state is healthy, but semantic cross-session memory is degraded. Restart OpenCode after MCP changes. Never claim a memory save that did not succeed.

## Worker cannot edit

Check that the parent did not pass a repository path outside the active worktree and that `allowed_paths` include the target. Child sessions inherit parent deny/external-directory restrictions.

## Provider credential is not visible to the orchestrator

Do not treat a missing provider environment variable as a delegation failure.
Myrmex resolves the effective OpenCode agent, model, provider policy, and
permissions first; OpenCode may route the Task with credentials unavailable to
the parent process. `CREDENTIAL_NOT_VISIBLE_TO_ORCHESTRATOR` is informational.
Block only for `AGENT_NOT_INSTALLED`, `AGENT_MODEL_UNRESOLVED`, policy failure,
or a real `PROVIDER_INVOCATION_FAILED` with sanitized evidence.

## Delegations appear complete but the workflow did not advance

Inspect `myrmex-state show <run-id>` for the delegation batch. The workflow
waits for a terminal structured result from each recorded task ID. Run the one
safe recovery only for the listed missing ID; do not rerun completed scouts or
send a user prompt merely to continue consolidation.

## A GitHub label command failed after PR creation

Do not repeat `gh pr create`. Run `myrmex-state reconcile <run-id>`, query the
saved head/base PR identity first, and use `scripts/github-pr-recovery.py` to
write a local artifact and apply the label through the narrow issue-label REST
fallback when the Projects scope is absent. Persist discovery/receipt through
the typed `pull_request` operation lifecycle; the helper no longer patches
state directly.
