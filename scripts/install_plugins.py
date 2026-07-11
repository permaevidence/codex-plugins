#!/usr/bin/env python3
"""Install the local Perma Evidence Codex plugins from this repo marketplace."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


MARKETPLACE_NAME = "permaevidence-local"
PLUGIN_NAMES = (
    "codex-long-term-memory",
    "codex-telegram-bridge",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Register this repository as a local Codex plugin marketplace and install "
            "the bundled long-term-memory and Telegram bridge plugins."
        )
    )
    parser.add_argument(
        "--skip-marketplace",
        action="store_true",
        help="Do not run `codex plugin marketplace add`; only run plugin add commands.",
    )
    parser.add_argument(
        "--only",
        choices=PLUGIN_NAMES,
        action="append",
        help="Install only the named plugin. May be provided more than once.",
    )
    return parser.parse_args()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def run(command: list[str]) -> None:
    print("+", " ".join(command))
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def main() -> None:
    args = parse_args()
    codex = shutil.which("codex")
    if not codex:
        raise SystemExit("codex CLI was not found on PATH. Install and log in to Codex first.")

    root = repo_root()
    marketplace = root / ".agents" / "plugins" / "marketplace.json"
    if not marketplace.exists():
        raise SystemExit(f"missing marketplace file: {marketplace}")

    if not args.skip_marketplace:
        run([codex, "plugin", "marketplace", "add", str(root)])

    selected = tuple(args.only) if args.only else PLUGIN_NAMES
    for plugin_name in selected:
        run([codex, "plugin", "add", f"{plugin_name}@{MARKETPLACE_NAME}"])

    print()
    print("Plugins installed from the local marketplace.")
    print("Next steps:")
    if "codex-long-term-memory" in selected:
        print(
            "- Run the memory installer: "
            f"python3 {root}/plugins/codex-long-term-memory/scripts/install.py"
        )
    if "codex-telegram-bridge" in selected:
        print(
            "- Configure Telegram, then run: "
            f"python3 {root}/plugins/codex-telegram-bridge/scripts/bridge.py start"
        )
        print(
            "- Check setup with: "
            f"python3 {root}/plugins/codex-telegram-bridge/scripts/bridge.py doctor"
        )
    print("- Start a new Codex thread or send /newsession after changing plugin setup.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        raise SystemExit(130)
