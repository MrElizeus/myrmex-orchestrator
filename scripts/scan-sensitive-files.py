#!/usr/bin/env python3
"""Fail when a repository contains raw material shaped like a real secret."""
from __future__ import annotations

import argparse
import re
from pathlib import Path


IGNORED_DIRECTORIES = {".git", "external-sources", ".playwright-mcp", ".atl", ".myrmex-work"}
IGNORED_FILES = {"MANIFEST.sha256", "PACKAGE-MANIFEST.json"}
PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----"),
    re.compile(rb"AKIA[0-9A-Z]{16}"),
    re.compile(rb"(?<![A-Za-z0-9])sk-[A-Za-z0-9]{32,}"),
)


def scan_repository(root: Path) -> list[str]:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"repository root is not a directory: {root}")
    hits: list[str] = []
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if any(part in IGNORED_DIRECTORIES for part in relative.parts):
            continue
        if (
            not path.is_file()
            or path.name in IGNORED_FILES
            or "__pycache__" in relative.parts
        ):
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if any(pattern.search(data) for pattern in PATTERNS):
            hits.append(relative.as_posix())
    return sorted(hits)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        hits = scan_repository(Path(args.root))
    except ValueError as error:
        parser.error(str(error))
    if hits:
        print("Potential secret-like material found:")
        for hit in hits:
            print(f"  {hit}")
        return 1
    print("sensitive-file scan: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
