#!/usr/bin/env python3
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.common import BACKUPS_DIR, STATE_DIR, data_items, ensure_state_dir, size_str


def create_backup(label: str | None = None) -> Path | None:
    ensure_state_dir()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"{timestamp}_{label}" if label else timestamp
    backup_path = BACKUPS_DIR / backup_name
    backup_path.mkdir(parents=True, exist_ok=False)

    copied: list[str] = []
    for item_path, item_type in data_items():
        if not item_path.exists():
            continue
        destination = backup_path / item_path.name
        if item_type == "file":
            shutil.copy2(item_path, destination)
        else:
            shutil.copytree(item_path, destination)
        copied.append(item_path.name)

    if not copied:
        backup_path.rmdir()
        return None

    meta = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "label": label,
        "items": copied,
    }
    (backup_path / "_backup_meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return backup_path


def main() -> None:
    ensure_state_dir()
    label = sys.argv[1] if len(sys.argv) > 1 else None

    print("Codex long-term memory backup")
    print()
    has_data = False
    for item_path, _ in data_items():
        if item_path.exists():
            has_data = True
            print(f"  {item_path.name} ({size_str(item_path)})")

    if not has_data:
        print("No memory data found.")
        return

    backup_path = create_backup(label)
    if backup_path is None:
        print("Nothing to back up.")
        return

    print()
    print(f"Backup created: {backup_path}")


if __name__ == "__main__":
    main()

