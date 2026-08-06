#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
CONFIG="$TMP/opencode"
BIN="$TMP/bin"
mkdir -p "$CONFIG/agents" "$CONFIG/skills/existing-frontier-skill" "$CONFIG/plugins"
cat > "$CONFIG/opencode.json" <<'JSON'
{
  "$schema": "https://opencode.ai/config.json",
  "default_agent": "existing-default-agent",
  "model": "openai/test-model",
  "mcp": {
    "engram": {
      "type": "local",
      "enabled": true,
      "command": ["/custom/engram", "mcp", "--tools=agent"]
    },
    "playwright": {
      "type": "local",
      "enabled": true,
      "command": ["npx", "@playwright/mcp@pinned", "--user-data-dir=/custom/profile"]
    }
  },
  "unrelated": {"preserve": true}
}
JSON
printf '%s\n' 'legacy agent' > "$CONFIG/agents/existing-default-agent.md"
printf '%s\n' 'legacy frontier skill' > "$CONFIG/skills/existing-frontier-skill/SKILL.md"
cp "$ROOT/profiles/myrmex-defaults.json" "$CONFIG/myrmex.json"

echo "  - scenario 1: preserve existing agent, skill, MCP, and Myrmex config"
"$ROOT/scripts/install.sh" --config-dir "$CONFIG" --bin-dir "$BIN" >"$TMP/install-1.log"
echo "    install: OK"
"$ROOT/scripts/verify-install.sh" --config-dir "$CONFIG" --bin-dir "$BIN" --json > "$TMP/verify.json"
python3 - "$CONFIG" "$TMP/verify.json" <<'PY'
import json, pathlib, sys
config=pathlib.Path(sys.argv[1])
verify=json.loads(pathlib.Path(sys.argv[2]).read_text())
assert verify['ok'], verify
cfg=json.loads((config/'opencode.json').read_text())
assert cfg['default_agent']=='existing-default-agent'
assert cfg['mcp']['engram']['command'][0]=='/custom/engram'
assert cfg['mcp']['playwright']['command'][1]=='@playwright/mcp@pinned'
assert cfg['unrelated']['preserve'] is True
assert (config/'agents/existing-default-agent.md').read_text().strip()=='legacy agent'
assert (config/'skills/existing-frontier-skill/SKILL.md').read_text().strip()=='legacy frontier skill'
assert (config/'myrmex.json').is_file()
assert (config/'commands/myrmex-frontier.md').is_file()
assert (config.parent/'bin/myrmex-state').is_file()
assert (config.parent/'bin/myrmex-memory').is_file()
assert (config.parent/'bin/myrmex-campaign').is_file()
assert (config.parent/'bin/myrmex-head').is_file()
record=json.loads((config/'myrmex-orchestrator/install-record.json').read_text())
recorded={item['path'] for item in record['files']}
assert str(config.parent/'bin/myrmex-memory') in recorded
assert str(config.parent/'bin/myrmex-campaign') in recorded
assert str(config.parent/'bin/myrmex-head') in recorded
PY

"$ROOT/scripts/uninstall.sh" --config-dir "$CONFIG" >"$TMP/uninstall-1.log"
echo "    uninstall and preservation: OK"
python3 - "$CONFIG" <<'PY'
import json, pathlib, sys
config=pathlib.Path(sys.argv[1])
cfg=json.loads((config/'opencode.json').read_text())
assert cfg['default_agent']=='existing-default-agent'
assert cfg['mcp']['engram']['command'][0]=='/custom/engram'
assert cfg['mcp']['playwright']['command'][1]=='@playwright/mcp@pinned'
assert cfg['unrelated']['preserve'] is True
assert (config/'agents/existing-default-agent.md').is_file()
assert (config/'skills/existing-frontier-skill/SKILL.md').is_file()
assert (config/'myrmex.json').is_file(), 'pre-existing identical myrmex.json must be preserved'
assert not (config/'agents/myrmex-orchestrator.md').exists()
assert not (config/'commands/myrmex-frontier.md').exists()
assert not (config.parent/'bin/myrmex-state').exists()
assert not (config.parent/'bin/myrmex-memory').exists()
assert not (config.parent/'bin/myrmex-campaign').exists()
assert not (config.parent/'bin/myrmex-head').exists()
PY

# Test added MCP entries and default-agent rollback in a second clean config.
CONFIG2="$TMP/opencode2"
mkdir -p "$CONFIG2"
printf '%s\n' '{"$schema":"https://opencode.ai/config.json","default_agent":"existing-default-agent","model":"openai/test-model"}' > "$CONFIG2/opencode.json"
echo "  - scenario 2: add missing MCP entries, set default, and roll back"
"$ROOT/scripts/install.sh" --config-dir "$CONFIG2" --bin-dir "$BIN" --set-default >"$TMP/install-2.log"
echo "    install with default-agent patch: OK"
python3 - "$CONFIG2" <<'PY'
import json, pathlib, sys
config=pathlib.Path(sys.argv[1])
cfg=json.loads((config/'opencode.json').read_text())
assert cfg['default_agent']=='myrmex-orchestrator'
assert 'engram' in cfg.get('mcp', {})
assert 'playwright' in cfg.get('mcp', {})
assert cfg['mcp']['playwright']['command'][:3] == ['npx', '-y', '@playwright/mcp@0.0.78']
assert (config/'myrmex.json').is_file()
PY
"$ROOT/scripts/uninstall.sh" --config-dir "$CONFIG2" >"$TMP/uninstall-2.log"
echo "    uninstall and config rollback: OK"
python3 - "$CONFIG2" <<'PY'
import json, pathlib, sys
cfg=json.loads((pathlib.Path(sys.argv[1])/'opencode.json').read_text())
assert cfg['default_agent']=='existing-default-agent'
assert 'engram' not in cfg.get('mcp', {})
assert 'playwright' not in cfg.get('mcp', {})
assert not (pathlib.Path(sys.argv[1])/'myrmex.json').exists(), 'package-created myrmex.json should be removed'
PY

echo "isolated install/uninstall test: PASS"

# Test that modified installed files retain the record needed for future cleanup.
CONFIG3="$TMP/opencode3"
mkdir -p "$CONFIG3"
printf '%s\n' '{"$schema":"https://opencode.ai/config.json","model":"openai/test-model"}' > "$CONFIG3/opencode.json"
echo "  - scenario 3: preserve tracking when an installed file is modified"
"$ROOT/scripts/install.sh" --config-dir "$CONFIG3" --bin-dir "$BIN" >"$TMP/install-3.log"
printf '%s\n' '# user modification' >> "$CONFIG3/agents/myrmex-worker.md"
"$ROOT/scripts/uninstall.sh" --config-dir "$CONFIG3" >"$TMP/uninstall-3.log"
python3 - "$CONFIG3" <<'PY'
import pathlib, sys
config=pathlib.Path(sys.argv[1])
assert (config/'agents/myrmex-worker.md').is_file()
assert (config/'myrmex-orchestrator/install-record.json').is_file()
assert (config/'myrmex-orchestrator').is_dir()
PY
echo "    modified-file tracking preservation: OK"
