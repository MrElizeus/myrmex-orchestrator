#!/usr/bin/env python3
"""Regression tests for strict secret scanning without fixture allowlists."""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCANNER = ROOT / "scripts" / "scan-sensitive-files.py"
PRIVATE_KEY_BEGIN = "-----BEGIN " + "RSA PRIVATE KEY-----"
PRIVATE_KEY_END = "-----END " + "RSA PRIVATE KEY-----"


def run_scan(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCANNER), str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


with tempfile.TemporaryDirectory(prefix="myrmex-sensitive-scan-") as td:
    repository = Path(td)
    tests_dir = repository / "tests"
    tests_dir.mkdir()

    # The source representation is safe while Python still constructs the
    # exact rejection fixture at runtime.  No scanner exemption is involved.
    intentional_fixture = tests_dir / "intentional_fixture.py"
    intentional_fixture.write_text(
        'PRIVATE_KEY = "-----BEGIN " + "RSA PRIVATE KEY-----"\n',
        encoding="utf-8",
    )
    safe = run_scan(repository)
    assert safe.returncode == 0, safe.stdout + safe.stderr

    production_secret = repository / "service.py"
    production_secret.write_text(
        f'PRIVATE_KEY = "{PRIVATE_KEY_BEGIN}\\nmaterial\\n{PRIVATE_KEY_END}"\n',
        encoding="utf-8",
    )
    detected = run_scan(repository)
    assert detected.returncode == 1, detected.stdout + detected.stderr
    assert "service.py" in detected.stdout

    production_secret.unlink()
    test_secret = tests_dir / "accidental_secret.txt"
    test_secret.write_text(
        f"{PRIVATE_KEY_BEGIN}\nmaterial\n{PRIVATE_KEY_END}\n",
        encoding="utf-8",
    )
    test_detected = run_scan(repository)
    assert test_detected.returncode == 1, test_detected.stdout + test_detected.stderr
    assert "tests/accidental_secret.txt" in test_detected.stdout

print("sensitive-file scanner regression: PASS")
