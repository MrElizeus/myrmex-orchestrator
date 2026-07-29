#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_DIR="${OPENCODE_CONFIG_DIR:-$HOME/.config/opencode}"
BIN_DIR="${MYRMEX_BIN_DIR:-$HOME/.local/bin}"

while (($#)); do
  case "$1" in
    --config-dir) [[ $# -ge 2 ]] || { echo "--config-dir requires a value" >&2; exit 2; }; CONFIG_DIR="$2"; shift 2 ;;
    --bin-dir) [[ $# -ge 2 ]] || { echo "--bin-dir requires a value" >&2; exit 2; }; BIN_DIR="$2"; shift 2 ;;
    -h|--help) echo "Usage: $0 [--config-dir DIR] [--bin-dir DIR]"; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

fatal=0
warn=0
say() { printf '%-32s %s\n' "$1" "$2"; }

command -v python3 >/dev/null 2>&1 || { say "Python" "missing"; exit 1; }
CONFIG_DIR="$(python3 -c 'import os,sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "$CONFIG_DIR")"
BIN_DIR="$(python3 -c 'import os,sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "$BIN_DIR")"
CONFIG="$CONFIG_DIR/opencode.json"

if "$ROOT/scripts/check-package.py" >"${TMPDIR:-/tmp}/myrmex-package-check.$$.json"; then
  say "Package" "OK"
else
  say "Package" "INVALID"
  cat "${TMPDIR:-/tmp}/myrmex-package-check.$$.json"
  fatal=1
fi
rm -f "${TMPDIR:-/tmp}/myrmex-package-check.$$.json"

say "Config directory" "$CONFIG_DIR"
if [[ -d "$CONFIG_DIR" ]]; then
  [[ -w "$CONFIG_DIR" ]] && say "Config writable" "yes" || { say "Config writable" "no"; fatal=1; }
else
  parent="$(dirname "$CONFIG_DIR")"
  [[ -d "$parent" && -w "$parent" ]] && say "Config creatable" "yes" || { say "Config creatable" "no"; fatal=1; }
fi

if config_report="$(python3 "$ROOT/scripts/patch-opencode-config.py" check --config "$CONFIG" 2>&1)"; then
  say "OpenCode JSON/JSONC" "valid"
  python3 - "$config_report" <<'PY'
import json, sys
r=json.loads(sys.argv[1])
print(f"{'opencode.json':32} {'present' if r['config_exists'] else 'absent'}")
print(f"{'opencode.jsonc':32} {'present' if r['jsonc_exists'] else 'absent'}")
print(f"{'MCP Engram':32} {'present' if 'engram' in set(r['mcp_json']+r['mcp_jsonc']) else 'absent'}")
print(f"{'MCP Playwright':32} {'present' if 'playwright' in set(r['mcp_json']+r['mcp_jsonc']) else 'absent'}")
print(f"{'default_agent in JSON':32} {r['default_agent_json'] or '<unset>'}")
print(f"{'default_agent in JSONC':32} {r['default_agent_jsonc'] or '<unset>'}")
PY
else
  say "OpenCode JSON/JSONC" "INVALID"
  printf '%s\n' "$config_report" >&2
  fatal=1
fi

if command -v opencode >/dev/null 2>&1; then
  if command -v timeout >/dev/null 2>&1; then
    opencode_version="$(timeout 10s opencode --version 2>/dev/null || true)"
  else
    opencode_version="$(python3 -c 'import subprocess; print(subprocess.run(["opencode","--version"],capture_output=True,text=True,timeout=10).stdout.strip())' 2>/dev/null || true)"
  fi
  say "OpenCode" "${opencode_version:-available but version probe timed out}"
else
  say "OpenCode" "not found in this shell"
  warn=1
fi
say "Python" "$(python3 --version 2>&1)"

if command -v node >/dev/null 2>&1; then
  node_version="$(node -p 'process.versions.node' 2>/dev/null || true)"
  say "Node" "$node_version"
  node_major="${node_version%%.*}"
  [[ "$node_major" =~ ^[0-9]+$ && "$node_major" -ge 18 ]] || { say "Playwright Node" "warning: Node 18+ required"; warn=1; }
else
  say "Node" "missing; Playwright MCP unavailable"
  warn=1
fi
command -v npx >/dev/null 2>&1 && say "npx" "$(command -v npx)" || { say "npx" "missing"; warn=1; }
command -v engram >/dev/null 2>&1 && say "Engram" "$(command -v engram)" || { say "Engram" "missing from PATH"; warn=1; }

[[ -x "$ROOT/bin/myrmex-state" ]] && say "Packaged myrmex-state" "executable" || { say "Packaged myrmex-state" "missing/not executable"; fatal=1; }
say "Target user bin" "$BIN_DIR"
case ":$PATH:" in
  *":$BIN_DIR:"*) say "Target bin on PATH" "yes" ;;
  *) say "Target bin on PATH" "no (installation can continue)"; warn=1 ;;
esac

resolution_report=""
if resolution_report="$("$ROOT/scripts/inspect-agent-resolution.py" --workspace "$ROOT" --config-dir "$CONFIG_DIR" 2>/dev/null)"; then
  say "Agent resolution" "report available"
else
  say "Agent resolution" "policy warnings or unresolved models"
  warn=1
fi
printf '%s\n' "$resolution_report" | python3 -c 'import json,sys; d=json.load(sys.stdin); [print("Agent status: "+a["agent"]+" -> "+a["status"]) for a in d.get("agents",[])]' 2>/dev/null || true

for target in \
  "$CONFIG_DIR/agents/myrmex-orchestrator.md" \
  "$CONFIG_DIR/agents/myrmex-scout.md" \
  "$CONFIG_DIR/agents/myrmex-worker.md" \
  "$CONFIG_DIR/agents/myrmex-verifier.md" \
  "$CONFIG_DIR/agents/myrmex-frontier.md" \
  "$CONFIG_DIR/skills/myrmex-frontier-delegation" \
  "$BIN_DIR/myrmex-state"; do
  [[ -e "$target" ]] && say "Existing collision" "$target (will be backed up)"
done

if ((fatal)); then
  echo "Preflight failed." >&2
  exit 1
fi
if ((warn)); then
  echo "Preflight completed with warnings. Existing MCP definitions are preserved; missing entries can be added by install.sh."
else
  echo "Preflight passed."
fi
