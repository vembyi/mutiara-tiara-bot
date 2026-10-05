#!/usr/bin/env python3
"""Publish the latest Mutiara Graphify report into the local Obsidian vault."""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

PROJECT = Path("/root/mutiara")
VAULT = Path("/root/Documents/Obsidian Vault")
SOURCE = PROJECT / "graphify-out" / "GRAPH_REPORT.md"
TARGET_DIR = VAULT / "Mutiara Bogor" / "Graphify"
TARGET = TARGET_DIR / "Architecture Graph Report.md"
STATE = TARGET_DIR / ".source.sha256"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    if not SOURCE.exists():
        raise SystemExit(f"Graphify report is missing: {SOURCE}")
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    source_hash = digest(SOURCE)
    if STATE.exists() and STATE.read_text().strip() == source_hash and TARGET.exists():
        print("unchanged")
        return 0
    shutil.copy2(SOURCE, TARGET)
    STATE.write_text(source_hash + "\n")
    print(f"updated {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
