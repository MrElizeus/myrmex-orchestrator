#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_DIR="${OPENCODE_CONFIG_DIR:-$HOME/.config/opencode}"

while (($#)); do
  case "$1" in
    --config-dir) CONFIG_DIR="$2"; shift 2 ;;
    -h|--help) echo "Usage: $0 [--config-dir DIR]"; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

CONFIG_DIR="$(python3 -c 'import os,sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "$CONFIG_DIR")"
META="$CONFIG_DIR/myrmex-orchestrator"
RECORD="$META/install-record.json"
CONFIG_RECORD="$META/config-change.json"

python3 "$ROOT/scripts/patch-opencode-config.py" undo --record "$CONFIG_RECORD"
python3 "$ROOT/scripts/uninstall.py" --record "$RECORD"

# The record and metadata root are deliberately kept if modified files remain.
if [[ -f "$RECORD" ]]; then rm -f "$RECORD"; fi
find "$META" -depth -type d -empty -delete 2>/dev/null || true

echo "Myrmex uninstall completed. Modified files and timestamped backups were preserved. Restart OpenCode."
