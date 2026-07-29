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
