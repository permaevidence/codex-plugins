#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import tempfile
from pathlib import Path
from typing import Any

STATE_DIR = Path.home() / ".codex" / "telegram-bridge"
CONFIG_FILE = STATE_DIR / "config.json"
CODEX_CONFIG_FILE = Path.home() / ".codex" / "config.toml"
ACCESS_FILE = STATE_DIR / "access.json"
CHAT_MAP_FILE = STATE_DIR / "chat-map.json"
REMINDERS_FILE = STATE_DIR / "scheduled_reminders.json"
EMAIL_STATE_FILE = STATE_DIR / "email_state.json"
VERSION_STATE_FILE = STATE_DIR / "version_state.json"
RUNTIME_STATE_FILE = STATE_DIR / "runtime_state.json"
ENV_FILE = STATE_DIR / ".env"
INBOX_DIR = STATE_DIR / "inbox"

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
    "model": None,
    "effort": None,
    "approval_policy": "never",
    "personality": "friendly",
    "sandbox_mode": "dangerFullAccess",
    "network_access": True,
    "writable_roots": [],
    "owner_chat_id": "",
    "openai_api_key": "",
    "enable_voice_transcription": True,
    "send_queue_confirmation": False,
    "enable_reminders": True,
    "enable_email_notifications": False,
}


def ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        STATE_DIR.chmod(0o700)
    except OSError:
        pass
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    for path in (
        CONFIG_FILE,
        ACCESS_FILE,
        CHAT_MAP_FILE,
        REMINDERS_FILE,
        EMAIL_STATE_FILE,
        VERSION_STATE_FILE,
        RUNTIME_STATE_FILE,
        ENV_FILE,
    ):
        if path.exists():
            try:
                path.chmod(0o600)
            except OSError:
                pass


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
    descriptor, raw_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temp_path = Path(raw_path)
    try:
        with open(descriptor, "w", encoding="utf-8", closefd=True) as handle:
            handle.write(json.dumps(data, indent=2) + "\n")
        temp_path.chmod(0o600)
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)


def load_config() -> dict[str, Any]:
    ensure_state_dir()
    env = load_env_file()
    data = load_json(CONFIG_FILE, {})
    if not isinstance(data, dict):
        data = {}
    codex_cmd = os.environ.get("CODEX_CMD") or str(data.get("codex_cmd") or DEFAULT_CONFIG["codex_cmd"])
    if not data.get("model") or not data.get("effort"):
        inherited_model, inherited_effort = codex_model_defaults(codex_cmd)
        changed = False
        if not data.get("model") and inherited_model:
            data["model"] = inherited_model
            changed = True
        if not data.get("effort") and inherited_effort:
            data["effort"] = inherited_effort
            changed = True
        if changed:
            save_json(CONFIG_FILE, data)
    merged = dict(DEFAULT_CONFIG)
    merged.update(data)

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or env.get("TELEGRAM_BOT_TOKEN") or merged.get("bot_token")
    merged["bot_token"] = bot_token
    merged["codex_cmd"] = os.environ.get("CODEX_CMD", merged["codex_cmd"])
    merged["openai_api_key"] = os.environ.get("OPENAI_API_KEY") or env.get("OPENAI_API_KEY") or merged.get("openai_api_key")
    return merged


def codex_model_defaults(codex_cmd: str = "codex") -> tuple[str | None, str | None]:
    """Resolve Codex's effective model and effort without imposing bridge defaults."""
    model: str | None = None
    effort = read_top_level_toml_string(CODEX_CONFIG_FILE, "model_reasoning_effort")
    try:
        completed = subprocess.run(
            [codex_cmd, "doctor", "--json"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if completed.returncode == 0:
            payload = json.loads(completed.stdout)
            model = str(
                (((payload.get("checks") or {}).get("config.load") or {}).get("details") or {}).get("model")
                or ""
            ).strip() or None
    except Exception:
        pass

    if model and not effort:
        try:
            completed = subprocess.run(
                [codex_cmd, "debug", "models"],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            if completed.returncode == 0:
                for item in json.loads(completed.stdout).get("models", []):
                    if isinstance(item, dict) and item.get("slug") == model:
                        effort = str(item.get("default_reasoning_level") or "").strip() or None
                        break
        except Exception:
            pass
    return model, effort


def read_top_level_toml_string(path: Path, key: str) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    top_level = text.split("\n[", 1)[0]
    match = re.search(rf'(?m)^\s*{re.escape(key)}\s*=\s*["\']([^"\']+)["\']\s*$', top_level)
    return match.group(1).strip() if match else None


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


def load_runtime_state() -> dict[str, Any]:
    ensure_state_dir()
    return load_json(RUNTIME_STATE_FILE, {})


def save_runtime_state(data: dict[str, Any]) -> None:
    save_json(RUNTIME_STATE_FILE, data)


def make_pair_code() -> str:
    return secrets.token_hex(3)
