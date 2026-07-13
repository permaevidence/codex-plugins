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
GOOGLE_PLUGIN_NAMES = (
    "gmail@openai-curated",
    "google-calendar@openai-curated",
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
        "--with-google-apps",
        action="store_true",
        help=(
            "Also install OpenAI's curated Gmail and Google Calendar plugins. "
            "Users still connect their Google account through OpenAI's OAuth flow."
        ),
    )
    parser.add_argument(
        "--replace-marketplace",
        action="store_true",
        help="Replace an existing marketplace registration with this repository path.",
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
        if args.replace_marketplace and marketplace_is_registered(codex):
            run([codex, "plugin", "marketplace", "remove", MARKETPLACE_NAME])
        run([codex, "plugin", "marketplace", "add", str(root)])

    selected = tuple(args.only) if args.only else PLUGIN_NAMES
    for plugin_name in selected:
        run([codex, "plugin", "add", f"{plugin_name}@{MARKETPLACE_NAME}"])
    if args.with_google_apps:
        for plugin_name in GOOGLE_PLUGIN_NAMES:
            run([codex, "plugin", "add", plugin_name])

    print()
    print("Plugins installed from the local marketplace.")
    print("Next steps:")
    if "codex-long-term-memory" in selected:
        print(
            "- Run the memory installer: "
            f"python3 {root}/plugins/codex-long-term-memory/scripts/install.py"
        )
        print("- Required memory OpenAI key: ~/.codex/long-term-memory/.env")
    if "codex-telegram-bridge" in selected:
        print("- Telegram bot token goes in: ~/.codex/telegram-bridge/.env")
        print("- Basic DM setup does not require knowing your chat ID upfront; use pairing.")
        print(
            "- Configure Telegram, then run: "
            f"python3 {root}/plugins/codex-telegram-bridge/scripts/bridge.py install-service"
        )
    if args.with_google_apps:
        print("- Gmail and Google Calendar curated plugins were installed.")
        print("- Google authorization is still required once; plugin installation does not grant mailbox access.")
        print("- Run `codex`, enter `/apps`, then connect Gmail and Google Calendar in the browser flow.")
        print("- No custom Google Cloud project, client ID, or client secret is required.")
        print(
            "- Check setup with: "
            f"python3 {root}/plugins/codex-telegram-bridge/scripts/bridge.py doctor"
        )
    print("- Start a new Codex thread or send /newsession after changing plugin setup.")


def marketplace_is_registered(codex: str) -> bool:
    completed = subprocess.run(
        [codex, "plugin", "marketplace", "list"],
        capture_output=True,
        text=True,
        check=False,
    )
    output = (completed.stdout or "") + "\n" + (completed.stderr or "")
    return MARKETPLACE_NAME in output


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        raise SystemExit(130)
