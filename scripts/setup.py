#!/usr/bin/env python3
"""Interactive setup wizard for the Perma Evidence Codex plugins."""

from __future__ import annotations

import argparse
import getpass
import json
import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_PLUGINS = REPO_ROOT / "scripts" / "install_plugins.py"
MEMORY_INSTALLER = REPO_ROOT / "plugins" / "codex-long-term-memory" / "scripts" / "install.py"
BRIDGE_CLI = REPO_ROOT / "plugins" / "codex-telegram-bridge" / "scripts" / "bridge.py"

CODEX_DIR = Path.home() / ".codex"
MEMORY_DIR = CODEX_DIR / "long-term-memory"
MEMORY_ENV = MEMORY_DIR / ".env"
MEMORY_CONFIG = MEMORY_DIR / "config.json"
TELEGRAM_DIR = CODEX_DIR / "telegram-bridge"
TELEGRAM_ENV = TELEGRAM_DIR / ".env"
TELEGRAM_CONFIG = TELEGRAM_DIR / "config.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Interactively install and configure the long-term-memory and Telegram "
            "bridge Codex plugins."
        )
    )
    parser.add_argument(
        "--project-dir",
        help=(
            "Starting folder for Telegram-launched Codex sessions and AGENTS.md memory. "
            "In dangerFullAccess mode this is not a permission boundary."
        ),
    )
    parser.add_argument("--telegram-token", help="Telegram bot token from BotFather.")
    parser.add_argument("--openai-api-key", help="OpenAI API key. Required for setup.")
    parser.add_argument("--model", default="gpt-5.5", help="Codex model for Telegram sessions.")
    parser.add_argument("--effort", default="high", choices=["minimal", "low", "medium", "high"], help="Codex reasoning effort.")
    parser.add_argument(
        "--sandbox-mode",
        choices=["workspaceWrite", "dangerFullAccess"],
        help="Codex sandbox mode for Telegram-launched sessions.",
    )
    parser.add_argument(
        "--network-access",
        choices=["yes", "no"],
        help="Whether Telegram-launched Codex sessions may use network access.",
    )
    parser.add_argument(
        "--start-bridge",
        choices=["yes", "no"],
        help="Start the bridge after writing configuration.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    print("Perma Evidence Codex plugin setup")
    print()
    print("This wizard will:")
    print("- install the local Codex plugins from this repo")
    print("- configure long-term memory with AGENTS.md transport")
    print("- require one OpenAI API key for memory summaries and voice transcription")
    print("- configure the Telegram bridge using your BotFather token")
    print("- default to broad autonomous Codex permissions for a dedicated remote-control computer")
    print("- use your home folder as Codex's starting point unless you choose another folder")
    print("- optionally start the bridge and run the doctor check")
    print()

    require_codex()

    project_dir = resolve_project_dir(args.project_dir)
    agents_md_path = project_dir / "AGENTS.md"
    telegram_token = resolve_secret(
        supplied=args.telegram_token,
        existing=read_env_value(TELEGRAM_ENV, "TELEGRAM_BOT_TOKEN"),
        label="Telegram bot token from BotFather",
        required_message="A Telegram bot token is required.",
    )
    if ":" not in telegram_token:
        if not prompt_yes_no("That token does not look like a normal BotFather token. Continue anyway?", default=False):
            raise SystemExit("Setup cancelled.")

    openai_key = resolve_secret(
        supplied=args.openai_api_key,
        existing=read_env_value(MEMORY_ENV, "OPENAI_API_KEY") or read_env_value(TELEGRAM_ENV, "OPENAI_API_KEY"),
        label="OpenAI API key",
        required_message="An OpenAI API key is required for memory summaries and voice transcription.",
    )
    if not openai_key.startswith("sk-"):
        if not prompt_yes_no("That OpenAI key does not start with 'sk-'. Continue anyway?", default=False):
            raise SystemExit("Setup cancelled.")

    sandbox_mode = args.sandbox_mode or choose_sandbox_mode()
    network_access = resolve_network_access(args.network_access, sandbox_mode)
    start_bridge = resolve_start_bridge(args.start_bridge)

    print()
    print("Setup summary")
    print(f"- repo: {REPO_ROOT}")
    print(f"- Telegram/Codex starting folder: {project_dir}")
    print("- access scope: whole Mac as your local user when dangerFullAccess is selected")
    print(f"- AGENTS.md memory target: {agents_md_path}")
    print(f"- Telegram token: {'set' if telegram_token else 'missing'}")
    print(f"- OpenAI API key: {'set' if openai_key else 'missing'}")
    print(f"- sandbox: {sandbox_mode}")
    print(f"- network access for Codex sessions: {network_access}")
    print(f"- start bridge after setup: {start_bridge}")
    print()
    if not prompt_yes_no("Proceed with these changes?", default=True):
        raise SystemExit("Setup cancelled.")

    run([sys.executable, str(INSTALL_PLUGINS)])
    run([sys.executable, str(MEMORY_INSTALLER)])

    configure_memory(openai_key=openai_key, agents_md_path=agents_md_path)
    configure_telegram(
        telegram_token=telegram_token,
        openai_key=openai_key,
        project_dir=project_dir,
        model=args.model,
        effort=args.effort,
        sandbox_mode=sandbox_mode,
        network_access=network_access,
    )

    print()
    print("Configuration written.")
    print(f"- memory env:   {MEMORY_ENV}")
    print(f"- memory config:{MEMORY_CONFIG}")
    print(f"- Telegram env: {TELEGRAM_ENV}")
    print(f"- Telegram cfg: {TELEGRAM_CONFIG}")

    if start_bridge:
        run([sys.executable, str(BRIDGE_CLI), "start"], check=False)

    print()
    run([sys.executable, str(BRIDGE_CLI), "doctor"], check=False)

    print()
    print("Next:")
    print("1. Send a Telegram DM to your bot.")
    print("2. If the bot replies with a pairing code, approve it locally:")
    print(f"   python3 {REPO_ROOT}/plugins/codex-telegram-bridge/scripts/access.py pair <code>")
    print("3. Send /newsession in Telegram after pairing so Codex starts fresh with the new plugin setup.")
    return 0


def require_codex() -> None:
    if not shutil.which("codex"):
        raise SystemExit("codex CLI was not found on PATH. Install Codex and run `codex login` first.")


def resolve_project_dir(value: str | None) -> Path:
    default = str(Path.home())
    while True:
        raw = value or prompt(
            "Codex starting folder (press Enter for your home folder; not a permission limit in autonomous mode)",
            default=default,
        )
        path = Path(raw).expanduser().resolve()
        if path.is_dir():
            return path
        print(f"Directory does not exist: {path}")
        if value:
            value = None


def resolve_secret(*, supplied: str | None, existing: str, label: str, required_message: str) -> str:
    if supplied:
        return supplied.strip()
    if existing:
        if prompt_yes_no(f"Use existing {label} already found on disk?", default=True):
            return existing
    while True:
        value = getpass.getpass(f"{label}: ").strip()
        if value:
            return value
        print(required_message)


def choose_sandbox_mode() -> str:
    print("Choose Telegram-launched Codex permissions:")
    print("1. dangerFullAccess (recommended for a dedicated remote-control machine): broad local filesystem access")
    print("2. workspaceWrite: narrower mode; can edit only the chosen starting folder")
    while True:
        choice = prompt("Sandbox mode", default="1")
        if choice in {"1", "dangerFullAccess", "dangerfullaccess"}:
            return "dangerFullAccess"
        if choice in {"2", "workspaceWrite", "workspacewrite"}:
            return "workspaceWrite"
        print("Choose 1 or 2.")


def resolve_network_access(value: str | None, sandbox_mode: str) -> bool:
    if value:
        return value == "yes"
    default = sandbox_mode == "dangerFullAccess"
    return prompt_yes_no("Allow network access for Telegram-launched Codex sessions?", default=default)


def resolve_start_bridge(value: str | None) -> bool:
    if value:
        return value == "yes"
    return prompt_yes_no("Start the Telegram bridge after setup?", default=True)


def configure_memory(*, openai_key: str, agents_md_path: Path) -> None:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    update_env_file(MEMORY_ENV, {"OPENAI_API_KEY": openai_key})
    config = load_json(MEMORY_CONFIG)
    config.update(
        {
            "enable_model_summaries": True,
            "enable_model_file_descriptions": True,
            "enable_model_user_facts": True,
            "openai_api_key": "",
            "openai_api_key_env": "OPENAI_API_KEY",
            "injection_transport": "agents_md",
            "agents_md_path": display_path(agents_md_path),
            "agents_project_doc_max_bytes": 524288,
        }
    )
    write_json(MEMORY_CONFIG, config)


def configure_telegram(
    *,
    telegram_token: str,
    openai_key: str,
    project_dir: Path,
    model: str,
    effort: str,
    sandbox_mode: str,
    network_access: bool,
) -> None:
    TELEGRAM_DIR.mkdir(parents=True, exist_ok=True)
    update_env_file(
        TELEGRAM_ENV,
        {
            "TELEGRAM_BOT_TOKEN": telegram_token,
            "OPENAI_API_KEY": openai_key,
        },
    )
    config = load_json(TELEGRAM_CONFIG)
    existing_owner = str(config.get("owner_chat_id") or "")
    config.update(
        {
            "default_cwd": str(project_dir),
            "model": model,
            "effort": effort,
            "approval_policy": "never",
            "personality": str(config.get("personality") or "friendly"),
            "sandbox_mode": sandbox_mode,
            "network_access": network_access,
            "writable_roots": [] if sandbox_mode == "dangerFullAccess" else [str(project_dir)],
            "owner_chat_id": existing_owner,
            "enable_voice_transcription": True,
            "send_queue_confirmation": bool(config.get("send_queue_confirmation", False)),
            "enable_reminders": bool(config.get("enable_reminders", True)),
            "enable_email_notifications": bool(config.get("enable_email_notifications", False)),
        }
    )
    write_json(TELEGRAM_CONFIG, config)


def update_env_file(path: Path, updates: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            output.append(line)
            continue
        key, _ = line.split("=", 1)
        key = key.strip()
        if key in updates:
            output.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            output.append(line)
    if output and output[-1].strip():
        output.append("")
    for key, value in updates.items():
        if key not in seen:
            output.append(f"{key}={value}")
    path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def read_env_value(path: Path, key: str) -> str:
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            found_key, value = line.split("=", 1)
            if found_key.strip() == key:
                return value.strip()
    except FileNotFoundError:
        pass
    return ""


def load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Could not parse {path}: {exc}") from exc


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def prompt(label: str, *, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or default


def prompt_yes_no(label: str, *, default: bool) -> bool:
    suffix = "Y/n" if default else "y/N"
    while True:
        value = input(f"{label} [{suffix}]: ").strip().lower()
        if not value:
            return default
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        print("Please answer yes or no.")


def run(command: list[str], *, check: bool = True) -> int:
    print()
    print("+", " ".join(command))
    completed = subprocess.run(command, check=False)
    if check and completed.returncode != 0:
        raise SystemExit(completed.returncode)
    return completed.returncode


def display_path(path: Path) -> str:
    try:
        home = Path.home().resolve()
        resolved = path.resolve()
        if resolved == home:
            return "~"
        if home in resolved.parents:
            return "~/" + str(resolved.relative_to(home))
    except OSError:
        pass
    return str(path)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nSetup interrupted.", file=sys.stderr)
        raise SystemExit(130)
