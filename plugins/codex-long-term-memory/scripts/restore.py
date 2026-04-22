#!/usr/bin/env python3
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backup import create_backup
from lib.common import BACKUPS_DIR, STATE_DIR, data_items, ensure_state_dir, size_str


def list_backups() -> None:
    ensure_state_dir()
    backups = sorted([path for path in BACKUPS_DIR.iterdir() if path.is_dir()]) if BACKUPS_DIR.exists() else []
    if not backups:
        print("No backups found.")
        return

    print("Available backups:")
    print()
    for backup in backups:
        meta_path = backup / "_backup_meta.json"
        label = ""
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                label = meta.get("label") or ""
            except Exception:
                label = ""
        line = f"  {backup.name} ({size_str(backup)})"
        if label:
            line += f" [{label}]"
        print(line)


def restore_backup(name: str) -> None:
    ensure_state_dir()
    backup_dir = BACKUPS_DIR / name
    if not backup_dir.exists():
        raise SystemExit(f"Backup not found: {backup_dir}")

    restore_items = []
    for path, item_type in data_items():
        candidate = backup_dir / path.name
        if candidate.exists():
            restore_items.append((candidate, path, item_type))

    if not restore_items:
        print("This backup does not contain restorable items.")
        return

    print(f"Restoring from: {backup_dir}")
    for source, _, _ in restore_items:
        print(f"  {source.name} ({size_str(source)})")

    print()
    print("Proceed? (yes/no)")
    try:
        answer = input("> ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        return

    if answer != "yes":
        print("Restore cancelled.")
        return

    create_backup("pre_restore")

    for source, destination, item_type in restore_items:
        if destination.exists():
            if item_type == "file":
                destination.unlink(missing_ok=True)
            else:
                shutil.rmtree(destination, ignore_errors=True)
        if item_type == "file":
            shutil.copy2(source, destination)
        else:
            shutil.copytree(source, destination)

    print("Restore complete.")


def main() -> None:
    if len(sys.argv) < 2:
        list_backups()
    else:
        restore_backup(sys.argv[1])


if __name__ == "__main__":
    main()
