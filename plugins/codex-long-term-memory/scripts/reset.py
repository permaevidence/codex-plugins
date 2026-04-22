#!/usr/bin/env python3
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backup import create_backup
from lib.common import data_items, ensure_state_dir, size_str

CONFIRMATION_PHRASE = "DELETE ALL HISTORY"


def main() -> None:
    ensure_state_dir()
    print("Codex long-term memory reset")
    print()
    print("WARNING: This will delete all saved memory data.")
    print()

    existing = [(path, item_type) for path, item_type in data_items() if path.exists()]
    if not existing:
        print("No memory data found.")
        return

    for path, _ in existing:
        print(f"  {path.name} ({size_str(path)})")

    print()
    print(f"Type exactly: {CONFIRMATION_PHRASE}")
    try:
        answer = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        return

    if answer != CONFIRMATION_PHRASE:
        print("Confirmation phrase did not match. Cancelled.")
        return

    backup_path = create_backup("pre_reset")
    if backup_path is None:
        print("Backup failed or no data available. Aborting reset.")
        return

    for path, item_type in existing:
        if item_type == "file":
            path.unlink(missing_ok=True)
        else:
            shutil.rmtree(path, ignore_errors=True)

    print()
    print(f"Reset complete. Backup: {backup_path}")


if __name__ == "__main__":
    main()

