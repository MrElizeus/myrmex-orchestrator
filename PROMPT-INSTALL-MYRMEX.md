# Implementation prompt — install Myrmex Orchestrator

Use this prompt from the extracted `myrmex-orchestrator-v0.1.0-alpha.1` directory with an OpenCode implementation agent that can modify the user's OpenCode configuration directory.

Default authorization for this prompt:

```text
SET_DEFAULT_AGENT=false
PATCH_MISSING_MCP=true
RUN_LIVE_FRONTIER_TEST=false
```

Change one of those values only when the user explicitly adds that authorization to the same request.

---

You are installing the supplied **Myrmex Orchestrator** package into the user's real OpenCode environment. This is a bounded installation, preservation, and verification task. Do not redesign the package or replace it with another framework.

## Required result

Install and verify:

- primary agent `myrmex-orchestrator`;
- subagents `myrmex-scout`, `myrmex-worker`, `myrmex-verifier`, and `myrmex-frontier`;
- skills `myrmex-delegation`, `myrmex-frontier-delegation`, `myrmex-memory`, and `myrmex-git-delivery`;
- all seven `myrmex-*` commands;
- executable `myrmex-state` in the selected user bin directory;
- contracts, docs, prompts, install metadata, and optional Myrmex defaults;
- existing memory and browser MCP definitions, or safe missing entries added by the installer.

The behavioral goals are:

- DIRECT is the normal route for clear bounded work;
- delegation is proportional and uses one writer;
- autonomous frontier work uses the isolated `myrmex-frontier` browser transport;
- exact autonomous state is stored by `myrmex-state` while memory stores durable semantic continuity.

## Authority and safety boundary

You are authorized to install or update only the Myrmex-owned paths through the supplied installer and to add missing memory/browser MCP entries when `PATCH_MISSING_MCP=true`.

You are not authorized to:

- edit or delete unrelated agents, skills, plugins, or configuration;
- replace an existing memory or browser MCP definition;
- set Myrmex as `default_agent` unless `SET_DEFAULT_AGENT=true` was explicitly authorized;
- read or print `.env`, credentials, cookies, browser storage, tokens, private keys, production/customer data, or unrelated source;
- log in, enter MFA/CAPTCHA, make purchases, push, deploy, use `sudo`, force-push, hard-reset, clean a repository, or rewrite history;
- claim live frontier autonomy has been proven when `RUN_LIVE_FRONTIER_TEST=false`.

If the actual configuration is invalid, managed from an unexpected source, shadowed by a later config, or unsafe to patch, stop with exact evidence. Do not guess.

## Authoritative package root

Resolve the directory containing all of:

```text
README.md
INSTALL.md
VERSION
agents/
skills/
commands/
contracts/
bin/myrmex-state
scripts/
```

Use those supplied files as authoritative. Do not regenerate prompts from memory.

## Procedure

### 1. Inspect the environment without mutation

1. Resolve the package root.
2. Resolve the actual OpenCode config directory from `OPENCODE_CONFIG_DIR`, otherwise `~/.config/opencode`.
3. Resolve the target user bin directory from `MYRMEX_BIN_DIR`, otherwise `~/.local/bin`.
4. Read `README.md`, `INSTALL.md`, `docs/ARCHITECTURE.md`, and `docs/SECURITY.md`.
5. Inspect `opencode.json`, `opencode.jsonc`, `agents/`, `skills/`, `commands/`, and `plugins/` without reading secrets.
6. Record the effective/default agent, existing Myrmex collisions, and exact existing MCP commands/profile paths.
7. Preserve model/provider/variant settings and unrelated configuration.

### 2. Validate the package

Run from the package root:

```bash
./scripts/run-tests.sh
./scripts/preflight.sh
```

Treat any failed package test, invalid JSON/JSONC, unwritable target, or unsafe ambiguity as fatal. Do not bypass checks.

### 3. Preview the installation

Run:

```bash
./scripts/install.sh --dry-run
```

Add `--config-dir <actual-dir>` and `--bin-dir <actual-bin-dir>` when required. Compare the preview with the authorized scope.

### 4. Install

Normal installation:

```bash
./scripts/install.sh
```

Use only the necessary options:

```text
--config-dir <dir>  actual non-default OpenCode config directory
--bin-dir <dir>     actual non-default user bin directory
--no-mcp            only when MCP is intentionally managed elsewhere
--set-default       only when SET_DEFAULT_AGENT=true
```

Do not manually copy files before trying the installer. The installer must create timestamped backups, preserve existing MCP definitions, write checksums, and run installed-file verification.

If installation fails, preserve the backup and report the exact failing step. Make a package-local correction only when the defect is proven, then rerun package tests before retrying.

### 5. Verify the installed system

Run:

```bash
./scripts/verify-install.sh
```

Use the same `--config-dir` and `--bin-dir` values used during installation. Then inspect the installed files and confirm:

- `myrmex-orchestrator` is `mode: primary`;
- all four children are hidden `mode: subagent`;
- worker cannot use `Task`, memory, browser tools, Git commit, or Git push;
- verifier cannot edit, delegate, write memory, commit, or push;
- scout cannot edit, test, delegate, browse, or write memory;
- frontier can use browser tools but cannot read the repository, run Bash, edit, delegate, load skills, or write memory;
- primary denies direct browser tools and delegates transport to `myrmex-frontier`;
- destructive Git commands, force push, `sudo`, and secret reads are denied by policy;
- the four skills and seven commands are installed;
- `myrmex-state doctor` succeeds using the installed executable;
- `myrmex-state` is executable and its bin directory is on `PATH`, or a precise PATH remediation is reported without editing shell startup files;
- existing model, plugin, MCP, and unrelated config remained intact;
- the previous default agent remained unchanged unless `SET_DEFAULT_AGENT=true`.

If both `opencode.json` and `opencode.jsonc` define `default_agent`, remember that later configuration may shadow an earlier value. Report the effective conflict; do not silently rewrite JSONC comments or managed configuration.

### 6. Capability checks

Do not alter a production repository.

- Confirm OpenCode can discover the installed agents after restart/reload when a supported discovery mechanism exists. Otherwise state that restart and manual selection are required.
- Confirm memory executable/tool availability without creating fake memories.
- Confirm browser executable/MCP prerequisites without opening a new page, sending a message, reading browser storage, or entering credentials.
- Run no live browser exchange when `RUN_LIVE_FRONTIER_TEST=false`.
- When `RUN_LIVE_FRONTIER_TEST=true`, use the separate `PROMPT-LIVE-SMOKE-TEST.md` in a non-critical repository and never commit/push.

### 7. Final report

Return a concise evidence-based installation report containing:

- Myrmex version;
- package root, config directory, and bin directory;
- backup directory;
- files/components installed;
- package tests and installed verification results;
- MCP entries preserved or added, without exposing profile internals beyond necessary path-level diagnostics;
- previous and resulting default-agent state;
- state CLI and PATH status;
- memory and browser capability status;
- whether live frontier testing was intentionally not run;
- warnings or incomplete work with exact reasons;
- next action: restart OpenCode, select `myrmex-orchestrator`, and run `/myrmex-doctor`.

Do not state that frontier autonomy is fully operational until a real browser send/wait/parse exchange has passed separately.
