#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG_DIR="$(printenv OPENCODE_CONFIG_DIR 2>/dev/null || printf '%s' "$HOME/.config/opencode")"

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
RESULT="$(mktemp /tmp/myrmex-uninstall.XXXXXX)"
trap 'rm -f "$RESULT"' EXIT

python3 "$ROOT/scripts/patch-opencode-config.py" undo --record "$CONFIG_RECORD"
python3 "$ROOT/scripts/uninstall.py" --record "$RECORD" >"$RESULT"

python3 - "$RESULT" "$RECORD" "$META" <<'PY'
import json
import pathlib
import sys

result_path, record_path, meta = map(pathlib.Path, sys.argv[1:])
result = json.loads(result_path.read_text(encoding="utf-8"))
preserved = result.get("preserved", [])
if preserved:
    print("Preserved install record because modified files remain:")
    for path in preserved:
        print("  " + path)
else:
    if record_path.exists():
        record_path.unlink()
    for directory in sorted([path for path in meta.glob("**/*") if path.is_dir()] + ([meta] if meta.is_dir() else []), key=lambda path: len(path.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass
PY

echo "Myrmex uninstall completed. Modified files and timestamped backups were preserved. Restart OpenCode."
