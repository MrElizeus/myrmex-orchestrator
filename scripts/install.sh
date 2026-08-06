#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_DIR="${OPENCODE_CONFIG_DIR:-$HOME/.config/opencode}"
BIN_DIR="${MYRMEX_BIN_DIR:-$HOME/.local/bin}"
SET_DEFAULT=0
NO_MCP=0
DRY_RUN=0

usage() {
  cat <<USAGE
Usage: $0 [options]
  --config-dir DIR   OpenCode config directory (default: ~/.config/opencode)
  --bin-dir DIR      User binary directory (default: ~/.local/bin)
  --set-default      Set default_agent to myrmex-orchestrator in opencode.json
  --no-mcp           Do not add missing Engram/Playwright MCP entries
  --dry-run          Print intended operations without modifying files
USAGE
}

while (($#)); do
  case "$1" in
    --config-dir) [[ $# -ge 2 ]] || { echo "--config-dir requires a value" >&2; exit 2; }; CONFIG_DIR="$2"; shift 2 ;;
    --bin-dir) [[ $# -ge 2 ]] || { echo "--bin-dir requires a value" >&2; exit 2; }; BIN_DIR="$2"; shift 2 ;;
    --set-default) SET_DEFAULT=1; shift ;;
    --no-mcp) NO_MCP=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

expand_path() { python3 -c 'import os,sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "$1"; }
CONFIG_DIR="$(expand_path "$CONFIG_DIR")"
BIN_DIR="$(expand_path "$BIN_DIR")"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_DIR="$CONFIG_DIR/backups/myrmex-orchestrator/$TIMESTAMP"
META_DIR="$CONFIG_DIR/myrmex-orchestrator"
CONFIG_FILE="$CONFIG_DIR/opencode.json"
CONFIG_RECORD="$META_DIR/config-change.json"
INSTALL_RECORD="$META_DIR/install-record.json"
MYRMEX_CONFIG="$CONFIG_DIR/myrmex.json"
STATE_BIN="$BIN_DIR/myrmex-state"
MEMORY_BIN="$BIN_DIR/myrmex-memory"
CAMPAIGN_BIN="$BIN_DIR/myrmex-campaign"
HEAD_BIN="$BIN_DIR/myrmex-head"

on_error() {
  local code=$?
  echo "Myrmex installation failed (exit $code). Existing files were not intentionally removed without a backup." >&2
  [[ -d "$BACKUP_DIR" ]] && echo "Backup directory: $BACKUP_DIR" >&2
  exit "$code"
}
trap on_error ERR

"$ROOT/scripts/check-package.py" --quick >/dev/null
check_args=(check --config "$CONFIG_FILE")
((SET_DEFAULT)) && check_args+=(--set-default)
python3 "$ROOT/scripts/patch-opencode-config.py" "${check_args[@]}" >/dev/null

AGENTS=(myrmex-orchestrator.md myrmex-worker.md myrmex-verifier.md myrmex-scout.md myrmex-frontier.md)
SKILLS=(myrmex-delegation myrmex-frontier-delegation myrmex-memory myrmex-git-delivery)
COMMANDS=(myrmex-doctor.md myrmex-frontier.md myrmex-frontier-interactive.md myrmex-direct.md myrmex-delegate.md myrmex-resume.md myrmex-status.md)

if ((DRY_RUN)); then
  cat <<PLAN
Would install Myrmex from: $ROOT
OpenCode config:          $CONFIG_DIR
User binary dir:          $BIN_DIR
Backup directory:         $BACKUP_DIR
Set default:              $SET_DEFAULT
Patch missing MCP:        $((1-NO_MCP))
Targets:
  $CONFIG_DIR/agents/myrmex-*.md
  $CONFIG_DIR/skills/myrmex-*
  $CONFIG_DIR/commands/myrmex-*.md
  $CONFIG_DIR/myrmex.json (created only if absent)
  $STATE_BIN
  $MEMORY_BIN
  $CAMPAIGN_BIN
  $HEAD_BIN
  $META_DIR
PLAN
  exit 0
fi

mkdir -p "$CONFIG_DIR/agents" "$CONFIG_DIR/skills" "$CONFIG_DIR/commands" "$BACKUP_DIR" "$BIN_DIR"

backup_config_target() {
  local target="$1"
  [[ -e "$target" ]] || return 0
  local rel="${target#"$CONFIG_DIR"/}"
  mkdir -p "$BACKUP_DIR/$(dirname "$rel")"
  cp -a "$target" "$BACKUP_DIR/$rel"
}

backup_external_target() {
  local target="$1"
  [[ -e "$target" ]] || return 0
  mkdir -p "$BACKUP_DIR/external-bin"
  cp -a "$target" "$BACKUP_DIR/external-bin/$(basename "$target")"
}

for name in "${AGENTS[@]}"; do backup_config_target "$CONFIG_DIR/agents/$name"; done
for name in "${SKILLS[@]}"; do backup_config_target "$CONFIG_DIR/skills/$name"; done
for name in "${COMMANDS[@]}"; do backup_config_target "$CONFIG_DIR/commands/$name"; done
backup_config_target "$META_DIR"
backup_config_target "$CONFIG_FILE"
backup_external_target "$STATE_BIN"
backup_external_target "$MEMORY_BIN"
backup_external_target "$CAMPAIGN_BIN"
backup_external_target "$HEAD_BIN"

for name in "${AGENTS[@]}"; do
  install -m 0644 "$ROOT/agents/$name" "$CONFIG_DIR/agents/$name"
done

for name in "${SKILLS[@]}"; do
  rm -rf "$CONFIG_DIR/skills/$name"
  cp -a "$ROOT/skills/$name" "$CONFIG_DIR/skills/$name"
done

for name in "${COMMANDS[@]}"; do
  install -m 0644 "$ROOT/commands/$name" "$CONFIG_DIR/commands/$name"
done

install -m 0755 "$ROOT/bin/myrmex-state" "$STATE_BIN"
install -m 0755 "$ROOT/bin/myrmex-memory" "$MEMORY_BIN"
install -m 0755 "$ROOT/bin/myrmex-campaign" "$CAMPAIGN_BIN"
install -m 0755 "$ROOT/bin/myrmex-head" "$HEAD_BIN"

MYRMEX_CONFIG_CREATED=0
if [[ ! -e "$MYRMEX_CONFIG" ]]; then
  install -m 0600 "$ROOT/profiles/myrmex-defaults.json" "$MYRMEX_CONFIG"
  MYRMEX_CONFIG_CREATED=1
fi

rm -rf "$META_DIR"
mkdir -p "$META_DIR"
cp -a "$ROOT/README.md" "$ROOT/START-HERE.md" "$ROOT/INSTALL.md" "$ROOT/VERSION" "$ROOT/LICENSE" "$ROOT/NOTICE" \
  "$ROOT/PROMPT-INSTALL-MYRMEX.md" "$ROOT/PROMPT-LIVE-SMOKE-TEST.md" "$META_DIR/"
cp -a "$ROOT/contracts" "$ROOT/docs" "$ROOT/examples" "$ROOT/profiles" "$ROOT/scripts" "$ROOT/bin" "$ROOT/services" "$META_DIR/"

patch_args=(apply --config "$CONFIG_FILE" --record "$CONFIG_RECORD")
((SET_DEFAULT)) && patch_args+=(--set-default)
((NO_MCP)) && patch_args+=(--no-mcp)
python3 "$ROOT/scripts/patch-opencode-config.py" "${patch_args[@]}" >/dev/null

python3 - "$ROOT" "$CONFIG_DIR" "$BACKUP_DIR" "$INSTALL_RECORD" "$NO_MCP" "$STATE_BIN" "$MEMORY_BIN" "$CAMPAIGN_BIN" "$HEAD_BIN" "$MYRMEX_CONFIG" "$MYRMEX_CONFIG_CREATED" <<'PY'
import datetime, hashlib, json, pathlib, sys
root=pathlib.Path(sys.argv[1]); config=pathlib.Path(sys.argv[2]); backup=pathlib.Path(sys.argv[3]); record=pathlib.Path(sys.argv[4])
no_mcp=bool(int(sys.argv[5])); state_bin=pathlib.Path(sys.argv[6]); memory_bin=pathlib.Path(sys.argv[7])
campaign_bin=pathlib.Path(sys.argv[8]); head_bin=pathlib.Path(sys.argv[9])
myrmex_config=pathlib.Path(sys.argv[10]); myrmex_config_created=bool(int(sys.argv[11]))
paths=[]
for n in ['myrmex-orchestrator.md','myrmex-worker.md','myrmex-verifier.md','myrmex-scout.md','myrmex-frontier.md']:
    paths.append(config/'agents'/n)
for skill in ['myrmex-delegation','myrmex-frontier-delegation','myrmex-memory','myrmex-git-delivery']:
    paths.extend(p for p in (config/'skills'/skill).rglob('*') if p.is_file())
for n in ['myrmex-doctor.md','myrmex-frontier.md','myrmex-frontier-interactive.md','myrmex-direct.md','myrmex-delegate.md','myrmex-resume.md','myrmex-status.md']:
    paths.append(config/'commands'/n)
meta=config/'myrmex-orchestrator'
paths.extend(p for p in meta.rglob('*') if p.is_file() and p.name != 'install-record.json')
paths.append(state_bin)
paths.append(memory_bin)
paths.append(campaign_bin)
paths.append(head_bin)
# Only remove myrmex.json on uninstall when this installation created it.
if myrmex_config_created and myrmex_config.is_file():
    paths.append(myrmex_config)
files=[]
for p in sorted(set(paths)):
    files.append({'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
data={
 'schema':'myrmex.install-record/v1',
 'version':(root/'VERSION').read_text().strip(),
 'installed_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
 'config_dir':str(config),
 'backup_dir':str(backup),
 'state_binary':str(state_bin),
 'memory_binary':str(memory_bin),
 'campaign_binary':str(campaign_bin),
 'head_binary':str(head_bin),
 'no_mcp':no_mcp,
 'files':files,
}
record.write_text(json.dumps(data,indent=2)+'\n')
PY

"$ROOT/scripts/verify-install.sh" --config-dir "$CONFIG_DIR" --bin-dir "$BIN_DIR"

PATH_WARNING=""
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) PATH_WARNING="WARNING: $BIN_DIR is not currently on PATH. Add it before using myrmex-state or myrmex-memory." ;;
esac

cat <<DONE

Myrmex Orchestrator installed successfully.
Backup: $BACKUP_DIR
Default agent was $([[ $SET_DEFAULT -eq 1 ]] && echo 'set to myrmex-orchestrator in opencode.json' || echo 'left unchanged').
$PATH_WARNING

Restart OpenCode, select myrmex-orchestrator, and run /myrmex-doctor.
DONE
