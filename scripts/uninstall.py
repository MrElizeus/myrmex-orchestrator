#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--record", required=True)
    args = p.parse_args()
    record_path = Path(args.record).expanduser().resolve()
    if not record_path.is_file():
        print(json.dumps({"removed": [], "preserved": [], "warning": "install record not found"}, indent=2))
        return 0

    data = json.loads(record_path.read_text(encoding="utf-8"))
    config_dir = Path(data.get("config_dir", record_path.parent.parent)).expanduser().resolve()
    removed: list[str] = []
    preserved: list[str] = []
    config_parents: set[Path] = set()

    for item in sorted(data.get("files", []), key=lambda x: len(x["path"]), reverse=True):
        path = Path(item["path"]).expanduser().resolve()
        if not path.exists():
            continue
        if not path.is_file() or sha(path) != item["sha256"]:
            preserved.append(str(path))
            continue
        path.unlink()
        removed.append(str(path))
        try:
            path.parent.relative_to(config_dir)
        except ValueError:
            # Never prune parents of external files such as ~/.local/bin/myrmex-state.
            continue
        config_parents.add(path.parent)

    for directory in sorted(config_parents, key=lambda p: len(p.parts), reverse=True):
        current = directory
        while current != config_dir:
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent

    print(json.dumps({"removed": removed, "preserved": preserved}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
