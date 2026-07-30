#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONDONTWRITEBYTECODE=1

cleanup_caches() {
  find "$ROOT" -path "$ROOT/external-sources" -prune -o -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
  find "$ROOT" -path "$ROOT/external-sources" -prune -o -type f -name '*.pyc' -delete 2>/dev/null || true
}
trap cleanup_caches EXIT
cleanup_caches

printf '%s\n' '[1/7] Package structure, contracts, and assets'
"$ROOT/scripts/check-package.py" --quick

printf '%s\n' '[2/7] Shell and Python syntax'
for script in "$ROOT"/scripts/*.sh "$ROOT"/tests/*.sh; do bash -n "$script"; done
python3 - "$ROOT" <<'PY'
from pathlib import Path
import sys
root=Path(sys.argv[1])
for path in [*root.glob('scripts/*.py'), *root.glob('tests/*.py'), root/'bin/myrmex-state']:
    compile(path.read_text(encoding='utf-8'), str(path), 'exec')
print('syntax compile: PASS')
PY

printf '%s\n' '[3/7] Config preservation and rollback patcher'
python3 "$ROOT/tests/test-config-patcher.py"

printf '%s\n' '[4/7] Atomic state, schema validity, and exact frontier DOM parsing'
python3 - "$ROOT" <<'PYSCHEMA'
import json, sys
from pathlib import Path
root=Path(sys.argv[1])
def package_files(pattern):
    ignored = {'.git', 'external-sources'}
    return [path for path in root.rglob(pattern) if not any(part in ignored for part in path.relative_to(root).parts)]
try:
    import jsonschema
except ImportError:
    print('jsonschema unavailable: schema meta-validation skipped')
else:
    checked=0
    for path in sorted(package_files('*.schema.json')):
        jsonschema.Draft202012Validator.check_schema(json.loads(path.read_text(encoding='utf-8')))
        checked += 1
    print(f'schema meta-validation: PASS ({checked} schemas)')
PYSCHEMA
python3 "$ROOT/tests/test-state-cli.py"
if command -v node >/dev/null 2>&1; then
  node "$ROOT/tests/test-frontier-dom.js"
else
  echo 'Node unavailable: DOM runtime test skipped (package check already reports this warning).'
fi

printf '%s\n' '[5/7] Isolated installation, preservation, verification, and uninstall'
"$ROOT/tests/test-isolated-install.sh"

printf '%s\n' '[6/9] Evidence, size policy, and public identity'
python3 "$ROOT/tests/test-evidence-and-size.py"
python3 "$ROOT/tests/test-public-policy.py"
python3 "$ROOT/tests/test-agent-resolution.py"
python3 "$ROOT/tests/test-github-pr-recovery.py"

printf '%s\n' '[7/9] Sensitive-file scan'
python3 - "$ROOT" <<'PYSCAN'
import re, sys
from pathlib import Path
root=Path(sys.argv[1])
ignored = {'.git', 'external-sources'}
patterns=[
    re.compile(rb'-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----'),
    re.compile(rb'AKIA[0-9A-Z]{16}'),
    re.compile(rb'(?<![A-Za-z0-9])sk-[A-Za-z0-9]{32,}'),
]
hits=[]
for path in root.rglob('*'):
    if any(part in ignored for part in path.relative_to(root).parts):
        continue
    if not path.is_file() or path.name in {'MANIFEST.sha256','PACKAGE-MANIFEST.json'} or '__pycache__' in path.parts:
        continue
    try: data=path.read_bytes()
    except OSError: continue
    if any(pattern.search(data) for pattern in patterns):
        hits.append(str(path.relative_to(root)))
if hits:
    print('Potential secret-like material found:', file=sys.stderr)
    for hit in hits: print('  '+hit, file=sys.stderr)
    raise SystemExit(1)
print('sensitive-file scan: PASS')
PYSCAN

printf '%s\n' '[8/9] Release builder dry-run'
python3 "$ROOT/scripts/build-release.py" --skip-tests >/dev/null
python3 "$ROOT/scripts/build-release.py" --skip-tests --reproducibility-check >/dev/null
python3 - "$ROOT" <<'PYRELEASE'
from pathlib import Path
import sys, zipfile
root=Path(sys.argv[1]); archives=list((root/'dist').glob('*.zip')); assert archives
with zipfile.ZipFile(archives[-1]) as z:
    names=z.namelist(); assert not any('/.git/' in n or n.endswith('/.git') for n in names)
    assert not any('external-sources' in n for n in names)
print('release archive shape: PASS')
PYRELEASE

printf '%s\n' '[9/9] Final reproducibility check'
cleanup_caches
"$ROOT/scripts/check-package.py" --quick
echo 'Myrmex package tests: PASS'
