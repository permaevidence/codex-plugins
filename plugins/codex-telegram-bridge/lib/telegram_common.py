#!/usr/bin/env python3
import json
import os
import secrets
from pathlib import Path
from typing import Any

STATE_DIR = Path.home() / ".codex" / "telegram-bridge"
CONFIG_FILE = STATE_DIR / "config.json"
ACCESS_FILE = STATE_DIR / "access.json"
CHAT_MAP_FILE = STATE_DIR / "chat-map.json"
REMINDERS_FILE = STATE_DIR / "scheduled_reminders.json"
EMAIL_STATE_FILE = STATE_DIR / "email_state.json"
VERSION_STATE_FILE = STATE_DIR / "version_state.json"
ENV_FILE = STATE_DIR / ".env"

DEFAULT_ACCESS = {
    "dmPolicy": "pairing",
    "allowFrom": [],
    "groups": {},
    "mentionPatterns": [],
    "ackReaction": "",
    "replyToMode": "first",
    "textChunkLimit": 3900,
    "chunkMode": "newline",
    "pending": {},
}

DEFAULT_CONFIG = {
    "codex_cmd": "codex",
    "default_cwd": str(Path.home()),
    "model": "gpt-5.4",
    "effort": "medium",
    "approval_policy": "never",
    "personality": "friendly",
    "sandbox_mode": "workspaceWrite",
    "network_access": False,
    "writable_roots": [],
    "owner_chat_id": "",
    "openai_api_key": "",
    "enable_voice_transcription": True,
    "enable_reminders": True,
    "enable_email_notifications": False,
}


def ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def load_env_file() -> dict[str, str]:
    values: dict[str, str] = {}
    if not ENV_FILE.exists():
        return values
    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def load_config() -> dict[str, Any]:
    ensure_state_dir()
    env = load_env_file()
    data = load_json(CONFIG_FILE, {})
    merged = dict(DEFAULT_CONFIG)
    merged.update(data)

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or env.get("TELEGRAM_BOT_TOKEN") or merged.get("bot_token")
    merged["bot_token"] = bot_token
    merged["codex_cmd"] = os.environ.get("CODEX_CMD", merged["codex_cmd"])
    merged["openai_api_key"] = os.environ.get("OPENAI_API_KEY") or env.get("OPENAI_API_KEY") or merged.get("openai_api_key")
    return merged


def load_access() -> dict[str, Any]:
    ensure_state_dir()
    data = load_json(ACCESS_FILE, {})
    merged = dict(DEFAULT_ACCESS)
    merged.update(data)
    merged.setdefault("allowFrom", [])
    merged.setdefault("groups", {})
    merged.setdefault("mentionPatterns", [])
    merged.setdefault("pending", {})
    return merged


def save_access(data: dict[str, Any]) -> None:
    save_json(ACCESS_FILE, data)


def load_chat_map() -> dict[str, Any]:
    ensure_state_dir()
    return load_json(CHAT_MAP_FILE, {})


def save_chat_map(data: dict[str, Any]) -> None:
    save_json(CHAT_MAP_FILE, data)


def load_reminders() -> list[dict[str, Any]]:
    ensure_state_dir()
    return load_json(REMINDERS_FILE, [])


def save_reminders(data: list[dict[str, Any]]) -> None:
    save_json(REMINDERS_FILE, data)


def load_email_state() -> dict[str, Any]:
    ensure_state_dir()
    return load_json(EMAIL_STATE_FILE, {"notified_ids": []})


def save_email_state(data: dict[str, Any]) -> None:
    save_json(EMAIL_STATE_FILE, data)


def load_version_state() -> dict[str, Any]:
    ensure_state_dir()
    return load_json(VERSION_STATE_FILE, {})


def save_version_state(data: dict[str, Any]) -> None:
    save_json(VERSION_STATE_FILE, data)


def make_pair_code() -> str:
    return secrets.token_hex(3)
