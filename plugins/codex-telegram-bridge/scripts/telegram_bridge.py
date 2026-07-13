#!/usr/bin/env python3
from __future__ import annotations

import json
import calendar
import mimetypes
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.telegram_api import (
    PHOTO_EXTS,
    answer_callback_query,
    download_attachment_to_dir,
    edit_message_text,
    fetch_telegram_file,
    send_chat_action,
    send_message,
    set_bot_commands,
    set_message_reaction,
    telegram_request,
)
from lib.gmail_imap import GmailImapError, poll_unread_messages
from lib.telegram_common import (
    CONFIG_FILE,
    INBOX_DIR,
    STATE_DIR,
    load_access,
    load_chat_map,
    load_config,
    load_email_state,
    load_reminders,
    load_runtime_state,
    load_version_state,
    load_json,
    make_pair_code,
    save_access,
    save_chat_map,
    save_email_state,
    save_reminders,
    save_runtime_state,
    save_version_state,
    save_json,
)

EMAIL_CHECK_INTERVAL = 5 * 60
REMINDER_CHECK_INTERVAL = 60
VERSION_CHECK_INTERVAL = 5 * 60
HEALTH_CHECK_INTERVAL = 30
TRANSCRIPTION_MAX_BYTES = 25 * 1024 * 1024
TRANSCRIPTION_MAX_ATTEMPTS = 3
TRANSCRIPTION_RETRY_DELAYS = (1, 2)
TRANSCRIPTION_RETRYABLE_CATEGORIES = {
    "api_error",
    "network",
    "rate_limit",
    "service_unavailable",
    "timeout",
}
GENERATED_IMAGES_DIR = Path.home() / ".codex" / "generated_images"
LONG_TERM_MEMORY_AGENTS_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "codex-long-term-memory"
    / "scripts"
    / "update_agents_injection.py"
)

BOT_COMMANDS = [
    {"command": "start", "description": "Show the welcome/help message"},
    {"command": "help", "description": "Show available commands"},
    {"command": "status", "description": "Show current Codex status"},
    {"command": "health", "description": "Check Codex, memory, Google, and transcription health"},
    {"command": "model", "description": "Choose Codex model and thinking effort"},
    {"command": "resume", "description": "Retry a parked interrupted task"},
    {"command": "retrymemory", "description": "Retry parked memory maintenance"},
    {"command": "stop", "description": "Interrupt the active Codex turn"},
    {"command": "newsession", "description": "Restart Codex and start a fresh thread"},
    {"command": "update", "description": "Update the plugins runtime and restart the bridge"},
]

MODEL_CALLBACK_PREFIX = "model:"
PENDING_UPDATE_FILE = STATE_DIR / "pending_update.json"
FAILED_UPDATES_DIR = STATE_DIR / "failed_updates"
TURN_RECOVERY_FILE = STATE_DIR / "turn_recovery_queue.json"
UPDATE_STATE_FILE = STATE_DIR / "update_state.json"
HEALTH_STATE_FILE = STATE_DIR / "health.json"
HEALTH_NOTIFICATION_FILE = STATE_DIR / "health_notifications.json"
HEALTH_STATE_LOCK = threading.Lock()
MEMORY_STATE_DIR = Path.home() / ".codex" / "long-term-memory"
MEMORY_HEALTH_FILE = MEMORY_STATE_DIR / "health.json"
MEMORY_TASK_FILE = MEMORY_STATE_DIR / "pending" / "memory-maintenance.json"
MEMORY_PID_FILE = MEMORY_STATE_DIR / "pending" / "memory-maintenance.pid"
MEMORY_ALERT_FILE = MEMORY_STATE_DIR / "pending" / "memory-maintenance.stuck.json"
MEMORY_COMMON_SCRIPT = (
    Path(__file__).resolve().parents[2] / "codex-long-term-memory" / "lib" / "common.py"
)
UPDATE_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "update.py"
SETUP_SCRIPT = UPDATE_SCRIPT.parent / "setup.py"


def rate_limit_retry_at(snapshot: dict[str, Any], *, now: float | None = None, buffer_seconds: int = 60) -> float | None:
    """Return a safe retry timestamp when the account is currently exhausted."""
    current = time.time() if now is None else now
    candidates: list[dict[str, Any]] = []
    by_id = snapshot.get("rateLimitsByLimitId")
    if isinstance(by_id, dict):
        candidates.extend(item for item in by_id.values() if isinstance(item, dict))
    legacy = snapshot.get("rateLimits")
    if isinstance(legacy, dict):
        candidates.append(legacy)

    exhausted_resets: list[float] = []
    reached_without_window = False
    for limits in candidates:
        reached = bool(limits.get("rateLimitReachedType"))
        exhausted_here = False
        for name in ("primary", "secondary"):
            window = limits.get(name)
            if not isinstance(window, dict) or int(window.get("usedPercent") or 0) < 100:
                continue
            exhausted_here = True
            reset = window.get("resetsAt")
            if isinstance(reset, (int, float)):
                exhausted_resets.append(float(reset))
        individual = limits.get("individualLimit")
        if isinstance(individual, dict) and int(individual.get("remainingPercent") or 0) <= 0:
            exhausted_here = True
            reset = individual.get("resetsAt")
            if isinstance(reset, (int, float)):
                exhausted_resets.append(float(reset))
        reached_without_window = reached_without_window or (reached and not exhausted_here)

    if exhausted_resets:
        return max(current + 1, max(exhausted_resets) + max(0, buffer_seconds))
    if reached_without_window:
        return current + 15 * 60
    return None


def recovery_prompt(record: dict[str, Any]) -> str:
    original = str(record.get("original_input") or "").strip()
    return (
        "The previous Codex turn ended before it could deliver a final assistant response, "
        "most likely because the account usage window was exhausted. Resume the same task now. "
        "Inspect the conversation, repository, filesystem, and any existing partial work first; "
        "do not repeat actions that already completed. Finish all remaining work, verify it, and "
        "send the user a self-contained final response.\n\n"
        f"Original Telegram request:\n{original}"
    )


def normalize_command(text: str, bot_username: str) -> str:
    token = (text or "").strip().split()[0] if text else ""
    if not token.startswith("/"):
        return token
    lowered = token.lower()
    suffix = f"@{bot_username.lower()}"
    if lowered.endswith(suffix):
        return token[: -len(suffix)]
    return token


def is_mentioned(message: dict[str, Any], bot_username: str, extra_patterns: list[str]) -> bool:
    text = message.get("text") or message.get("caption") or ""
    entities = message.get("entities") or message.get("caption_entities") or []
    for entity in entities:
        entity_type = entity.get("type")
        offset = entity.get("offset", 0)
        length = entity.get("length", 0)
        if entity_type == "mention":
            mentioned = text[offset : offset + length]
            if mentioned.lower() == f"@{bot_username.lower()}":
                return True
        if entity_type == "text_mention":
            user = entity.get("user", {})
            if user.get("is_bot") and (user.get("username") or "").lower() == bot_username.lower():
                return True

    reply_from = ((message.get("reply_to_message") or {}).get("from") or {}).get("username")
    if (reply_from or "").lower() == bot_username.lower():
        return True

    for pattern in extra_patterns:
        try:
            if re.search(pattern, text, flags=re.IGNORECASE):
                return True
        except re.error:
            continue
    return False


def gate_message(message: dict[str, Any], token: str, bot_username: str) -> bool:
    access = load_access()
    chat = message.get("chat", {})
    chat_type = chat.get("type")
    chat_id = str(chat.get("id"))
    sender_id = str((message.get("from") or {}).get("id"))

    if access.get("dmPolicy") == "disabled":
        return False

    if chat_type == "private":
        if sender_id in access.get("allowFrom", []):
            return True
        if access.get("dmPolicy") == "allowlist":
            return False

        pending = access.setdefault("pending", {})
        for code, entry in pending.items():
            if str(entry.get("senderId")) == sender_id:
                send_message(token, chat_id, f"Pairing pending. Approve this code locally:\n{code}")
                return False

        code = make_pair_code()
        pending[code] = {
            "senderId": sender_id,
            "chatId": chat_id,
            "createdAt": int(time.time()),
        }
        save_access(access)
        send_message(
            token,
            chat_id,
            "This bot is locked down.\n"
            f"Approve this pairing code locally to allow this Telegram account:\n{code}",
        )
        return False

    if chat_type in {"group", "supergroup"}:
        policy = access.get("groups", {}).get(chat_id)
        if not policy:
            return False
        allow_from = policy.get("allowFrom") or []
        if allow_from and sender_id not in allow_from:
            return False
        require_mention = policy.get("requireMention", True)
        if require_mention and not is_mentioned(message, bot_username, access.get("mentionPatterns", [])):
            return False
        return True

    return False


def list_codex_models(codex_cmd: str) -> list[dict[str, Any]]:
    try:
        proc = subprocess.run(
            [codex_cmd, "debug", "models"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    try:
        data = json.loads(proc.stdout)
    except Exception:
        return []

    models: list[dict[str, Any]] = []
    for raw_model in data.get("models", []):
        if not isinstance(raw_model, dict):
            continue
        slug = str(raw_model.get("slug") or "").strip()
        if not slug or raw_model.get("visibility") == "hide":
            continue
        efforts = [
            str(item.get("effort") or "").strip()
            for item in raw_model.get("supported_reasoning_levels", [])
            if isinstance(item, dict) and str(item.get("effort") or "").strip()
        ]
        models.append(
            {
                "slug": slug,
                "display_name": str(raw_model.get("display_name") or slug).strip(),
                "description": str(raw_model.get("description") or "").strip(),
                "default_effort": str(raw_model.get("default_reasoning_level") or "").strip(),
                "efforts": efforts,
                "priority": raw_model.get("priority", 9999),
            }
        )
    return sorted(models, key=lambda item: (item.get("priority", 9999), item["slug"]))


def find_codex_model(models: list[dict[str, Any]], slug: str) -> dict[str, Any] | None:
    wanted = slug.strip().lower()
    for model in models:
        if str(model.get("slug") or "").lower() == wanted:
            return model
    return None


def current_model_text(config: dict[str, Any]) -> str:
    model = str(config.get("model") or "Codex default").strip()
    effort = str(config.get("effort") or "default effort").strip()
    return f"{model} / {effort}"


def model_keyboard(models: list[dict[str, Any]]) -> dict[str, Any]:
    rows: list[list[dict[str, str]]] = []
    for model in models:
        rows.append(
            [
                {
                    "text": str(model.get("display_name") or model.get("slug")),
                    "callback_data": f"{MODEL_CALLBACK_PREFIX}choose:{model['slug']}",
                }
            ]
        )
    return {"inline_keyboard": rows}


def effort_keyboard(model: dict[str, Any]) -> dict[str, Any]:
    efforts = model.get("efforts") or []
    rows: list[list[dict[str, str]]] = []
    for index in range(0, len(efforts), 3):
        rows.append(
            [
                {
                    "text": str(effort),
                    "callback_data": f"{MODEL_CALLBACK_PREFIX}effort:{model['slug']}:{effort}",
                }
                for effort in efforts[index : index + 3]
            ]
        )
    return {"inline_keyboard": rows}


def save_model_selection(config: dict[str, Any], model: str, effort: str) -> None:
    current = load_json(CONFIG_FILE, {})
    if not isinstance(current, dict):
        current = {}
    current["model"] = model
    current["effort"] = effort
    save_json(CONFIG_FILE, current)
    config["model"] = model
    config["effort"] = effort


def model_menu_text(config: dict[str, Any], models: list[dict[str, Any]]) -> str:
    lines = [f"Current model: {current_model_text(config)}", "", "Choose a model:"]
    for model in models:
        efforts = ", ".join(model.get("efforts") or []) or "default"
        lines.append(f"- {model['display_name']} (`{model['slug']}`): {efforts}")
    return "\n".join(lines)


def effort_menu_text(model: dict[str, Any]) -> str:
    efforts = ", ".join(model.get("efforts") or []) or "default"
    return (
        f"Choose thinking effort for {model['display_name']} (`{model['slug']}`).\n"
        f"Available: {efforts}"
    )


def handle_model_command(
    token: str,
    chat_id: str,
    text: str,
    reply_to_message_id: int | None,
    config: dict[str, Any],
    access: dict[str, Any],
) -> None:
    models = list_codex_models(str(config.get("codex_cmd") or "codex"))
    if not models:
        send_message(
            token,
            chat_id,
            f"Current model: {current_model_text(config)}\n\nCould not read Codex model catalog.",
            reply_to_message_id,
            access=access,
        )
        return

    args = (text or "").strip().split()[1:]
    if not args or args[0].lower() in {"current", "list", "menu"}:
        send_message(
            token,
            chat_id,
            model_menu_text(config, models),
            reply_to_message_id,
            access=access,
            reply_markup=model_keyboard(models),
        )
        return

    selected_model = find_codex_model(models, args[0])
    if not selected_model:
        send_message(
            token,
            chat_id,
            f"Unknown model: {args[0]}\n\nUse /model to choose from the available models.",
            reply_to_message_id,
            access=access,
        )
        return

    if len(args) == 1:
        send_message(
            token,
            chat_id,
            effort_menu_text(selected_model),
            reply_to_message_id,
            access=access,
            reply_markup=effort_keyboard(selected_model),
        )
        return

    effort = args[1].strip()
    if selected_model.get("efforts") and effort not in selected_model["efforts"]:
        send_message(
            token,
            chat_id,
            f"{selected_model['display_name']} does not support effort `{effort}`.\n"
            f"Available: {', '.join(selected_model['efforts'])}",
            reply_to_message_id,
            access=access,
        )
        return

    save_model_selection(config, str(selected_model["slug"]), effort)
    send_message(
        token,
        chat_id,
        f"Updated Codex model for future turns:\n{current_model_text(config)}",
        reply_to_message_id,
        access=access,
    )


def handle_model_callback(
    token: str,
    callback: dict[str, Any],
    config: dict[str, Any],
    access: dict[str, Any],
) -> None:
    callback_id = str(callback.get("id") or "")
    data = str(callback.get("data") or "")
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    message_id = message.get("message_id")
    sender_id = str((callback.get("from") or {}).get("id") or "")

    if sender_id not in [str(item) for item in access.get("allowFrom", [])]:
        answer_callback_query(token, callback_id, "Not authorized.")
        return
    if not chat_id or not isinstance(message_id, int) or not data.startswith(MODEL_CALLBACK_PREFIX):
        answer_callback_query(token, callback_id, "Invalid selection.")
        return

    models = list_codex_models(str(config.get("codex_cmd") or "codex"))
    if not models:
        answer_callback_query(token, callback_id, "Could not read models.")
        return

    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action == "choose" and len(parts) == 3:
        selected_model = find_codex_model(models, parts[2])
        if not selected_model:
            answer_callback_query(token, callback_id, "Unknown model.")
            return
        answer_callback_query(token, callback_id, f"Selected {selected_model['display_name']}")
        edit_message_text(
            token,
            chat_id,
            message_id,
            effort_menu_text(selected_model),
            reply_markup=effort_keyboard(selected_model),
        )
        return

    if action == "effort" and len(parts) == 4:
        selected_model = find_codex_model(models, parts[2])
        effort = parts[3]
        if not selected_model or (selected_model.get("efforts") and effort not in selected_model["efforts"]):
            answer_callback_query(token, callback_id, "Invalid effort.")
            return
        save_model_selection(config, str(selected_model["slug"]), effort)
        answer_callback_query(token, callback_id, "Model updated.")
        edit_message_text(
            token,
            chat_id,
            message_id,
            f"Updated Codex model for future turns:\n{current_model_text(config)}",
        )
        return

    answer_callback_query(token, callback_id, "Invalid selection.")


def encode_multipart(fields: dict[str, str], file_bytes: bytes, filename: str, mime_type: str) -> tuple[bytes, str]:
    boundary = f"----CodexTelegramBridge{int(time.time() * 1000)}"
    body = bytearray()

    for name, value in fields.items():
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        body.extend(value.encode("utf-8"))
        body.extend(b"\r\n")

    body.extend(f"--{boundary}\r\n".encode("utf-8"))
    body.extend(
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode("utf-8")
    )
    body.extend(f"Content-Type: {mime_type}\r\n\r\n".encode("utf-8"))
    body.extend(file_bytes)
    body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode("utf-8"))

    return bytes(body), boundary


def classify_api_failure(exc: Exception, *, component: str) -> tuple[str, str, int | None]:
    status = int(exc.code) if isinstance(exc, HTTPError) else None
    body = ""
    if isinstance(exc, HTTPError):
        try:
            body = exc.read().decode("utf-8", errors="replace")[:2000]
        except Exception:
            body = ""
    searchable = f"{exc} {body}".lower()
    label = "voice transcription" if component == "transcription" else component
    if status in {401, 403}:
        return "authentication", f"The OpenAI API key for {label} was rejected.", status
    if status == 429 and any(word in searchable for word in ("quota", "credit", "billing", "insufficient")):
        return "insufficient_credit", f"The OpenAI API account has insufficient credit for {label}.", status
    if status == 429:
        return "rate_limit", f"OpenAI temporarily rate-limited {label}.", status
    if status == 404:
        return "model_unavailable", f"The OpenAI model used for {label} is unavailable to this account.", status
    if status is not None and status >= 500:
        return "service_unavailable", f"OpenAI returned HTTP {status} for {label}.", status
    if isinstance(exc, TimeoutError):
        return "timeout", f"The OpenAI request for {label} timed out.", status
    if isinstance(exc, URLError):
        return "network", f"Could not reach OpenAI for {label}: {exc.reason}", status
    return "api_error", f"{label.capitalize()} failed: {type(exc).__name__}: {exc}"[:500], status


def classify_codex_failure(error: str) -> tuple[str, str]:
    lowered = error.lower()
    if re.search(r"usage|rate.?limit|quota|credit|capacity", lowered):
        return "usage_limit", "Codex usage capacity is temporarily exhausted."
    if re.search(r"unauthori[sz]ed|authentication|not logged in|login required|sign.?in|token expired", lowered):
        return "authentication", "Codex is no longer authenticated. Run `codex login` on the computer, then use /resume."
    if re.search(r"subscription|plan|payment|billing", lowered):
        return "subscription", "The Codex subscription or billing needs attention. Fix it in the OpenAI account, then use /resume."
    if re.search(r"network|connection|timed? out|temporarily unavailable|service unavailable", lowered):
        return "network", "Codex could not reach its service. Check the computer's network, then use /resume."
    return "turn_failure", f"Codex reported: {error[:500]}"


def set_component_health(
    component: str,
    status: str,
    *,
    category: str = "",
    detail: str = "",
    http_status: int | None = None,
) -> bool:
    with HEALTH_STATE_LOCK:
        state = load_json(HEALTH_STATE_FILE, {})
        if not isinstance(state, dict):
            state = {}
        components = state.setdefault("components", {})
        if not isinstance(components, dict):
            components = {}
            state["components"] = components
        previous = components.get(component, {})
        fingerprint = f"{status}|{category}|{detail}"
        changed = not isinstance(previous, dict) or previous.get("fingerprint") != fingerprint
        payload: dict[str, Any] = {
            "status": status,
            "category": category,
            "detail": detail[:1000],
            "fingerprint": fingerprint,
            "updated_at": datetime.now().astimezone().isoformat(),
        }
        if http_status is not None:
            payload["http_status"] = http_status
        components[component] = payload
        save_json(HEALTH_STATE_FILE, state)
    return changed


def component_health(component: str) -> dict[str, Any]:
    state = load_json(HEALTH_STATE_FILE, {})
    components = state.get("components", {}) if isinstance(state, dict) else {}
    value = components.get(component, {}) if isinstance(components, dict) else {}
    return value if isinstance(value, dict) else {}


def transcription_failure_message(detail: str) -> str:
    return (
        f"Voice transcription failed: {detail}\n"
        "Your audio was not sent to Codex as text. Text messages still work. "
        "Check the OpenAI API key and API billing, then send the voice message again."
    )


def notify_transcription_failure(
    on_notice: Callable[[str], None] | None,
    detail: str,
    *,
    changed: bool,
) -> None:
    if not on_notice:
        return
    if changed:
        on_notice(transcription_failure_message(detail))
    else:
        on_notice("Voice transcription is still unavailable, so this voice message was not sent to Codex. Use /health for details.")


def transcribe_audio(
    file_bytes: bytes,
    filename: str,
    mime_type: str,
    api_key: str,
    on_notice: Callable[[str], None] | None = None,
) -> str | None:
    if not api_key:
        detail = "The OpenAI API key is missing."
        changed = set_component_health("transcription", "error", category="configuration", detail=detail)
        notify_transcription_failure(on_notice, detail, changed=changed)
        return None
    if len(file_bytes) > TRANSCRIPTION_MAX_BYTES:
        detail = "The voice file is larger than the 25 MB transcription limit."
        changed = set_component_health("transcription", "error", category="file_too_large", detail=detail)
        notify_transcription_failure(on_notice, detail, changed=changed)
        return None

    suffix = Path(filename or "audio").suffix.lower()
    if mime_type == "audio/ogg" or suffix in {".oga", ".ogg"}:
        converted = convert_audio_for_transcription(file_bytes, suffix or ".oga")
        if converted is not None:
            file_bytes, filename, mime_type = converted

    fields = {"model": "gpt-4o-transcribe"}
    payload, boundary = encode_multipart(fields, file_bytes, filename, mime_type)
    last_failure: tuple[str, str, int | None] | None = None
    for attempt in range(1, TRANSCRIPTION_MAX_ATTEMPTS + 1):
        req = urllib.request.Request(
            "https://api.openai.com/v1/audio/transcriptions",
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as response:
                result = json.loads(response.read().decode("utf-8"))
            text = (result.get("text") or "").strip()
            if not text:
                detail = "OpenAI returned no transcription text."
                changed = set_component_health(
                    "transcription", "error", category="invalid_response", detail=detail
                )
                notify_transcription_failure(on_notice, detail, changed=changed)
                return None
            was_error = component_health("transcription").get("status") == "error"
            set_component_health(
                "transcription",
                "ok",
                detail="The most recent voice transcription succeeded.",
            )
            if was_error and on_notice:
                on_notice("Voice transcription is working again.")
            return text
        except Exception as exc:
            last_failure = classify_api_failure(exc, component="transcription")
            category, _, _ = last_failure
            if category not in TRANSCRIPTION_RETRYABLE_CATEGORIES or attempt >= TRANSCRIPTION_MAX_ATTEMPTS:
                break
            time.sleep(TRANSCRIPTION_RETRY_DELAYS[attempt - 1])

    category, detail, http_status = last_failure or (
        "api_error",
        "Voice transcription failed for an unknown reason.",
        None,
    )
    changed = set_component_health(
        "transcription",
        "error",
        category=category,
        detail=detail,
        http_status=http_status,
    )
    notify_transcription_failure(on_notice, detail, changed=changed)
    return None


def convert_audio_for_transcription(file_bytes: bytes, suffix: str) -> tuple[bytes, str, str] | None:
    try:
        with tempfile.TemporaryDirectory(prefix="telegram-audio-") as tmp_dir:
            source = Path(tmp_dir) / f"input{suffix}"
            target = Path(tmp_dir) / "transcription.mp3"
            source.write_bytes(file_bytes)
            proc = subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-y",
                    "-loglevel",
                    "error",
                    "-i",
                    str(source),
                    str(target),
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
            if proc.returncode != 0 or not target.exists():
                return None
            converted = target.read_bytes()
            return converted, target.name, "audio/mpeg"
    except Exception:
        return None


def list_generated_images(thread_id: str) -> list[str]:
    directory = GENERATED_IMAGES_DIR / str(thread_id or "").strip()
    if not directory.exists() or not directory.is_dir():
        return []
    files: list[Path] = []
    try:
        for path in directory.iterdir():
            if path.is_file() and path.suffix.lower() in PHOTO_EXTS:
                files.append(path)
    except Exception:
        return []
    files.sort(key=lambda item: (item.stat().st_mtime, item.name))
    return [str(path.resolve()) for path in files]


class CodexAppServerClient:
    def __init__(self, config: dict[str, Any], send_callback) -> None:
        self.config = config
        self.send_callback = send_callback
        default_cwd = Path(str(config.get("default_cwd") or Path.home())).expanduser()
        app_server_cwd = str(default_cwd if default_cwd.is_dir() else Path.home())
        self.process = subprocess.Popen(
            [config["codex_cmd"], "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            text=True,
            bufsize=1,
            cwd=app_server_cwd,
        )
        self._lock = threading.Lock()
        self._next_id = 1
        self._pending: dict[int, queue.Queue] = {}
        self._turns: dict[str, dict[str, Any]] = {}
        self._active_turn_by_chat: dict[str, str] = {}
        self._thread_to_chat: dict[str, str] = {}
        self._recovery_lock = threading.Lock()
        self._restart_on_exit = True
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._initialize()

    def _initialize(self) -> None:
        self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "permaevidence_telegram",
                    "title": "Perma Evidence Telegram Bridge",
                    "version": "0.2.0",
                }
            },
        )
        self.notify("initialized", {})

    def shutdown(self, timeout: float = 5.0) -> None:
        self._restart_on_exit = False
        if self.process.poll() is not None:
            return
        try:
            if self.process.stdin is not None:
                self.process.stdin.close()
        except Exception:
            pass
        self.process.terminate()
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=timeout)

    def request(self, method: str, params: Any, timeout: float = 180.0) -> Any:
        request_id = self._reserve_id()
        reply_queue: queue.Queue = queue.Queue(maxsize=1)
        self._pending[request_id] = reply_queue
        self._send_json(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        try:
            result = reply_queue.get(timeout=timeout)
        except queue.Empty as exc:
            self._pending.pop(request_id, None)
            raise RuntimeError(f"Codex app-server request timed out: {method}") from exc
        if "error" in result:
            raise RuntimeError(result["error"])
        return result["result"]

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send_json({"jsonrpc": "2.0", "method": method, "params": params})

    def list_apps(self) -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        cursor: str | None = None
        for page in range(6):
            params: dict[str, Any] = {"limit": 200, "forceRefetch": page == 0}
            if cursor:
                params["cursor"] = cursor
            result = self.request("app/list", params, timeout=15)
            for app in (result or {}).get("data", []):
                if not isinstance(app, dict):
                    continue
                app_id = str(app.get("id") or "").strip()
                if app_id:
                    found[app_id] = app
            cursor = str((result or {}).get("nextCursor") or "").strip() or None
            if not cursor:
                break
        return found

    def _reserve_id(self) -> int:
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            return request_id

    def _send_json(self, payload: dict[str, Any]) -> None:
        if self.process.stdin is None:
            raise RuntimeError("app-server stdin is unavailable")
        line = json.dumps(payload, ensure_ascii=False)
        with self._lock:
            self.process.stdin.write(line + "\n")
            self.process.stdin.flush()

    def _read_loop(self) -> None:
        if self.process.stdout is None:
            return
        try:
            for raw_line in self.process.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._handle_message(message)
        finally:
            error = {"error": {"code": -32000, "message": "Codex app-server exited before replying"}}
            for request_id, pending in list(self._pending.items()):
                self._pending.pop(request_id, None)
                try:
                    pending.put_nowait(error)
                except queue.Full:
                    pass
            # A live bridge with a dead app-server cannot recover useful work.
            # Exit the child so the supervisor/platform service restarts the complete
            # process tree. Intentional /newsession shutdown disables this path.
            if getattr(self, "_restart_on_exit", False):
                set_component_health(
                    "codex",
                    "error",
                    category="app_server_exit",
                    detail="Codex app-server exited unexpectedly; the bridge supervisor is restarting it.",
                )
                os._exit(75)

    def _handle_message(self, message: dict[str, Any]) -> None:
        if "id" in message and "method" not in message:
            pending = self._pending.pop(message["id"], None)
            if pending is not None:
                pending.put(message)
            return

        if "id" in message and "method" in message:
            self._handle_server_request(message)
            return

        method = message.get("method")
        params = message.get("params", {})

        if method == "item/agentMessage/delta":
            state = self._turns.get(params.get("turnId"))
            if state is not None:
                state["text"] += params.get("delta", "")
        elif method == "item/completed":
            item = params.get("item", {})
            if item.get("type") == "agentMessage":
                state = self._turns.get(params.get("turnId"))
                if state is not None:
                    state["text"] = item.get("text", state["text"])
        elif method == "turn/completed":
            turn = params.get("turn", {})
            turn_id = turn.get("id")
            if turn_id in self._turns:
                self._finish_turn(turn_id, turn)
        elif method == "error":
            error = params.get("error", {})
            turn_id = params.get("turnId")
            thread_id = params.get("threadId")
            if turn_id and turn_id in self._turns:
                self._turns[turn_id]["error"] = error.get("message", "Unknown error")
            elif thread_id:
                chat_id = self._thread_to_chat.get(thread_id)
                if chat_id:
                    self.send_callback(chat_id, f"Codex error: {error.get('message', 'Unknown error')}")

    def _handle_server_request(self, message: dict[str, Any]) -> None:
        method = message["method"]
        request_id = message["id"]
        if method == "item/commandExecution/requestApproval":
            self._send_json({"jsonrpc": "2.0", "id": request_id, "result": {"decision": "acceptForSession"}})
            return
        if method == "item/fileChange/requestApproval":
            self._send_json({"jsonrpc": "2.0", "id": request_id, "result": {"decision": "acceptForSession"}})
            return
        if method == "item/permissions/requestApproval":
            permissions = (message.get("params") or {}).get("permissions", {})
            self._send_json(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"permissions": permissions, "scope": "session"},
                }
            )
            return
        if method == "item/tool/requestUserInput":
            auto_answers = build_auto_user_input_answers(message.get("params") or {})
            if auto_answers:
                self._send_json({"jsonrpc": "2.0", "id": request_id, "result": {"answers": auto_answers}})
            else:
                self._send_json({"jsonrpc": "2.0", "id": request_id, "result": {"answers": {}}})
            params = message.get("params", {})
            chat_id = self._thread_to_chat.get(params.get("threadId"))
            if chat_id:
                if auto_answers:
                    self.send_callback(chat_id, "Codex requested interactive tool input. The bridge auto-answered with the default options.")
                else:
                    self.send_callback(
                        chat_id,
                        "Codex asked for interactive tool input, and the bridge could not infer safe answers.",
                    )
            return
        if method == "mcpServer/elicitation/request":
            self._send_json({"jsonrpc": "2.0", "id": request_id, "result": {"action": "accept"}})
            return
        self._send_json(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"Unsupported server request: {method}"},
            }
        )

    def ensure_thread(self, chat_id: str, chat_map: dict[str, Any]) -> str:
        entry = chat_map.get(chat_id)
        if entry and entry.get("thread_id"):
            thread_id = entry["thread_id"]
            if thread_id not in self._thread_to_chat:
                try:
                    self.request("thread/resume", {"threadId": thread_id})
                except RuntimeError as exc:
                    # A newly created thread may have no rollout yet if the bridge
                    # restarts before the first real turn starts. In that case Codex
                    # cannot resume it, so replace the stale mapping with a fresh thread.
                    if "no rollout found for thread id" not in str(exc):
                        raise
                    self._thread_to_chat.pop(thread_id, None)
                    result = self.request("thread/start", {})
                    thread_id = result["thread"]["id"]
                    chat_map[chat_id] = {"thread_id": thread_id, "created_at": time.time()}
            self._thread_to_chat[thread_id] = chat_id
            return thread_id

        result = self.request("thread/start", {})
        thread_id = result["thread"]["id"]
        chat_map[chat_id] = {"thread_id": thread_id, "created_at": time.time()}
        self._thread_to_chat[thread_id] = chat_id
        return thread_id

    def new_thread(self, chat_id: str, chat_map: dict[str, Any]) -> str:
        result = self.request("thread/start", {})
        thread_id = result["thread"]["id"]
        chat_map[chat_id] = {"thread_id": thread_id, "created_at": time.time()}
        self._thread_to_chat[thread_id] = chat_id
        self._active_turn_by_chat.pop(chat_id, None)
        return thread_id

    def start_turn(
        self,
        chat_id: str,
        thread_id: str,
        text: str,
        *,
        recovery_id: str | None = None,
        original_input: str | None = None,
    ) -> str:
        recovery_id = recovery_id or str(uuid.uuid4())
        result = self.request("turn/start", self._turn_params(thread_id, text))
        turn = result["turn"]
        turn_id = turn["id"]
        source_input = original_input if original_input is not None else text
        self._turns[turn_id] = {
            "chat_id": chat_id,
            "thread_id": thread_id,
            "text": "",
            "started_at": time.time(),
            "status": turn.get("status", "inProgress"),
            "error": None,
            "generated_images_seen": set(list_generated_images(thread_id)),
            "input_text": source_input,
            "recovery_id": recovery_id,
        }
        self._track_inflight_turn(recovery_id, chat_id, thread_id, turn_id, source_input)
        self._active_turn_by_chat[chat_id] = turn_id
        save_runtime_state(
            {
                **load_runtime_state(),
                "active_chat_id": chat_id,
                "active_thread_id": thread_id,
                "active_turn_id": turn_id,
                "updated_at": time.time(),
            }
        )
        return turn_id

    def has_active_turn(self, chat_id: str) -> bool:
        return bool(self._active_turn_by_chat.get(chat_id))

    def pending_recovery_count(self, chat_id: str | None = None) -> int:
        with self._recovery_lock:
            records = load_json(TURN_RECOVERY_FILE, [])
        if not isinstance(records, list):
            return 0
        if chat_id is None:
            return len(records)
        return sum(1 for item in records if isinstance(item, dict) and str(item.get("chat_id")) == chat_id)

    def parked_recovery_count(self, chat_id: str) -> int:
        with self._recovery_lock:
            records = load_json(TURN_RECOVERY_FILE, [])
        if not isinstance(records, list):
            return 0
        return sum(
            1
            for item in records
            if isinstance(item, dict)
            and str(item.get("chat_id")) == chat_id
            and item.get("state") == "parked"
        )

    def cancel_recovery(self, chat_id: str) -> bool:
        with self._recovery_lock:
            records = load_json(TURN_RECOVERY_FILE, [])
            if not isinstance(records, list):
                records = []
            kept = [item for item in records if not isinstance(item, dict) or str(item.get("chat_id")) != chat_id]
            changed = len(kept) != len(records)
            if changed:
                save_json(TURN_RECOVERY_FILE, kept)
        return changed

    def retry_parked_recovery(self, chat_id: str) -> bool:
        with self._recovery_lock:
            records = load_json(TURN_RECOVERY_FILE, [])
            if not isinstance(records, list):
                return False
            changed = False
            for record in records:
                if (
                    isinstance(record, dict)
                    and str(record.get("chat_id")) == chat_id
                    and record.get("state") == "parked"
                ):
                    record.update(
                        {
                            "state": "pending",
                            "attempts": 0,
                            "due_at": time.time(),
                            "parked_at": None,
                            "waiting_for_rate_limit_reset": False,
                        }
                    )
                    changed = True
            if changed:
                save_json(TURN_RECOVERY_FILE, records)
            return changed

    def _remove_recovery(self, recovery_id: str) -> None:
        with self._recovery_lock:
            records = load_json(TURN_RECOVERY_FILE, [])
            if not isinstance(records, list):
                return
            save_json(
                TURN_RECOVERY_FILE,
                [item for item in records if not isinstance(item, dict) or str(item.get("id")) != recovery_id],
            )

    def _track_inflight_turn(
        self,
        recovery_id: str,
        chat_id: str,
        thread_id: str,
        turn_id: str,
        original_input: str,
    ) -> None:
        with self._recovery_lock:
            records = load_json(TURN_RECOVERY_FILE, [])
            if not isinstance(records, list):
                records = []
            record = next(
                (item for item in records if isinstance(item, dict) and str(item.get("id")) == recovery_id),
                None,
            )
            if record is None:
                record = {
                    "id": recovery_id,
                    "chat_id": chat_id,
                    "thread_id": thread_id,
                    "original_turn_id": turn_id,
                    "original_input": original_input,
                    "created_at": time.time(),
                    "attempts": 0,
                }
                records.append(record)
            record.update(
                {
                    "active_retry_turn_id": turn_id,
                    "due_at": time.time() + 10 * 60,
                    "state": "in_progress",
                }
            )
            save_json(TURN_RECOVERY_FILE, records)

    def _queue_recovery(self, state: dict[str, Any], reason: str, *, force_park: bool = False) -> str:
        if not self.config.get("enable_turn_recovery", True):
            return "disabled"
        with self._recovery_lock:
            records = load_json(TURN_RECOVERY_FILE, [])
            if not isinstance(records, list):
                records = []
            recovery_id = str(state.get("recovery_id") or uuid.uuid4())
            existing = next(
                (item for item in records if isinstance(item, dict) and str(item.get("id")) == recovery_id),
                None,
            )
            record = existing if existing is not None else {
                "id": recovery_id,
                "chat_id": state["chat_id"],
                "thread_id": state["thread_id"],
                "original_turn_id": state.get("turn_id"),
                "original_input": state.get("input_text", ""),
                "created_at": time.time(),
                "attempts": 0,
            }
            attempts = int(record.get("attempts") or 0)
            maximum = max(1, int(self.config.get("turn_recovery_max_attempts") or 5))
            parked = force_park or attempts >= maximum
            record.update(
                {
                    "reason": reason,
                    "due_at": None if parked else time.time() + 5,
                    "last_failed_at": time.time(),
                    "active_retry_turn_id": None,
                    "state": "parked" if parked else "pending",
                    "parked_at": time.time() if parked else None,
                }
            )
            if existing is None:
                records.append(record)
            save_json(TURN_RECOVERY_FILE, records)
            return "parked" if parked else "pending"

    def start_recovery_loop(self) -> None:
        if not self.config.get("enable_turn_recovery", True):
            return

        def worker() -> None:
            poll_seconds = max(5, int(self.config.get("turn_recovery_poll_seconds") or 30))
            reset_buffer = max(0, int(self.config.get("turn_recovery_reset_buffer_seconds") or 60))
            while True:
                try:
                    with self._recovery_lock:
                        records = load_json(TURN_RECOVERY_FILE, [])
                        if not isinstance(records, list):
                            records = []
                        now = time.time()
                        record = next(
                            (
                                item for item in records
                                if isinstance(item, dict)
                                and item.get("state") != "parked"
                                and float(item.get("due_at") or 0) <= now
                                and not self._active_turn_by_chat.get(str(item.get("chat_id")))
                            ),
                            None,
                        )
                        record = dict(record) if record is not None else None
                    if record is not None:
                        maximum = max(1, int(self.config.get("turn_recovery_max_attempts") or 5))
                        if int(record.get("attempts") or 0) >= maximum:
                            self._update_recovery_record(
                                str(record["id"]),
                                state="parked",
                                due_at=None,
                                parked_at=now,
                                reason=str(record.get("reason") or "recovery could not start successfully"),
                            )
                            self.send_callback(
                                str(record.get("chat_id")),
                                f"Automatic recovery stopped after {maximum} unsuccessful attempts. The task remains "
                                "saved without consuming more quota. Fix the underlying issue, then use /resume.",
                            )
                            time.sleep(poll_seconds)
                            continue
                        try:
                            limits = self.request("account/rateLimits/read", None, timeout=30)
                        except Exception:
                            limits = {}
                        retry_at = rate_limit_retry_at(limits if isinstance(limits, dict) else {}, now=now, buffer_seconds=reset_buffer)
                        if retry_at is not None:
                            self._update_recovery_record(
                                str(record["id"]),
                                due_at=retry_at,
                                waiting_for_rate_limit_reset=True,
                                state="pending",
                            )
                        else:
                            chat_id = str(record.get("chat_id"))
                            thread_id = str(record.get("thread_id"))
                            if thread_id not in self._thread_to_chat:
                                self.request("thread/resume", {"threadId": thread_id})
                                self._thread_to_chat[thread_id] = chat_id
                            attempts = int(record.get("attempts") or 0) + 1
                            self._update_recovery_record(
                                str(record["id"]),
                                attempts=attempts,
                                last_attempt_at=now,
                                waiting_for_rate_limit_reset=False,
                                due_at=now + min(3600, 60 * (2 ** min(attempts, 6))),
                                state="starting",
                            )
                            retry_turn_id = self.start_turn(
                                chat_id,
                                thread_id,
                                recovery_prompt(record),
                                recovery_id=str(record["id"]),
                                original_input=str(record.get("original_input") or ""),
                            )
                            self._update_recovery_record(str(record["id"]), active_retry_turn_id=retry_turn_id, state="in_progress")
                            self.send_callback(chat_id, "Codex capacity is available again. Resuming the interrupted task automatically.")
                except Exception as exc:
                    print(f"turn recovery loop failed: {exc}", file=sys.stderr)
                time.sleep(poll_seconds)

        threading.Thread(target=worker, daemon=True, name="turn-recovery").start()

    def _update_recovery_record(self, recovery_id: str, **changes: Any) -> None:
        with self._recovery_lock:
            records = load_json(TURN_RECOVERY_FILE, [])
            if not isinstance(records, list):
                return
            for record in records:
                if isinstance(record, dict) and str(record.get("id")) == recovery_id:
                    record.update(changes)
                    save_json(TURN_RECOVERY_FILE, records)
                    return

    def steer_turn(self, chat_id: str, text: str) -> str | None:
        turn_id = self._active_turn_by_chat.get(chat_id)
        if not turn_id:
            return None
        state = self._turns.get(turn_id)
        if not state:
            return None
        self.request(
            "turn/steer",
            {
                "threadId": state["thread_id"],
                "input": [{"type": "text", "text": text}],
                "expectedTurnId": turn_id,
            },
        )
        save_runtime_state(
            {
                **load_runtime_state(),
                "active_chat_id": chat_id,
                "active_thread_id": state["thread_id"],
                "active_turn_id": turn_id,
                "updated_at": time.time(),
            }
        )
        return turn_id

    def interrupt_turn(self, chat_id: str) -> bool:
        turn_id = self._active_turn_by_chat.get(chat_id)
        if not turn_id:
            return False
        state = self._turns.get(turn_id)
        if not state:
            return False
        self.request("turn/interrupt", {"threadId": state["thread_id"]})
        save_runtime_state(
            {
                **load_runtime_state(),
                "active_chat_id": chat_id,
                "active_thread_id": state["thread_id"],
                "active_turn_id": turn_id,
                "last_turn_status": "interruptRequested",
                "updated_at": time.time(),
            }
        )
        return True

    def inject_external_message(self, chat_id: str, chat_map: dict[str, Any], text: str) -> None:
        if self.steer_turn(chat_id, text):
            return
        thread_id = self.ensure_thread(chat_id, chat_map)
        self.start_turn(chat_id, thread_id, text)

    def status_text(self, chat_id: str, chat_map: dict[str, Any]) -> str:
        entry = chat_map.get(chat_id)
        version = current_codex_version(str(self.config.get("codex_cmd") or "codex"))
        last_seen = ""
        if entry and entry.get("last_seen_at"):
            try:
                age = int(time.time() - float(entry["last_seen_at"]))
                last_seen = f"\nLast inbound message: {age}s ago"
            except Exception:
                last_seen = ""
        turn_id = self._active_turn_by_chat.get(chat_id)
        model_line = f"\nModel: {current_model_text(self.config)}"
        health_line = f"\n{compact_health_summary()}"
        if turn_id and turn_id in self._turns:
            state = self._turns[turn_id]
            elapsed = int(time.time() - state["started_at"])
            suffix = f"\nCLI: {version}" if version else ""
            return (
                f"Active turn: {turn_id}\nThread: {state['thread_id']}\nRunning for: {elapsed}s"
                f"{model_line}{last_seen}{health_line}{suffix}"
            )
        if entry and entry.get("thread_id"):
            suffix = f"\nCLI: {version}" if version else ""
            recovery = self.pending_recovery_count(chat_id)
            parked = self.parked_recovery_count(chat_id)
            recovery_line = f"\nSaved interrupted tasks: {recovery}" if recovery else ""
            if parked:
                recovery_line += f" ({parked} parked; use /resume after fixing the issue)"
            return f"Idle.\nCurrent thread: {entry['thread_id']}{model_line}{last_seen}{recovery_line}{health_line}{suffix}"
        suffix = f"\nCLI: {version}" if version else ""
        return f"Idle.\nNo thread has been created for this chat yet.{model_line}{health_line}{suffix}"

    def _turn_params(self, thread_id: str, text: str) -> dict[str, Any]:
        params = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": text}],
            "approvalPolicy": self.config.get("approval_policy", "never"),
            "cwd": self.config.get("default_cwd"),
            "personality": self.config.get("personality"),
            "sandboxPolicy": self._sandbox_policy(),
        }
        if self.config.get("model"):
            params["model"] = self.config["model"]
        if self.config.get("effort"):
            params["effort"] = self.config["effort"]
        return params

    def _sandbox_policy(self) -> dict[str, Any]:
        mode = self.config.get("sandbox_mode", "workspaceWrite")
        if mode == "dangerFullAccess":
            return {"type": "dangerFullAccess"}
        if mode == "readOnly":
            return {"type": "readOnly", "networkAccess": bool(self.config.get("network_access", False))}
        writable_roots = [str(Path(root).expanduser()) for root in self.config.get("writable_roots", [])]
        return {
            "type": "workspaceWrite",
            "networkAccess": bool(self.config.get("network_access", False)),
            "writableRoots": writable_roots,
        }

    def _finish_turn(self, turn_id: str, turn: dict[str, Any]) -> None:
        state = self._turns.get(turn_id)
        if state is None:
            return
        state["status"] = turn.get("status")
        state["turn_id"] = turn_id
        chat_id = state["chat_id"]
        self._active_turn_by_chat.pop(chat_id, None)

        text = state["text"].strip()
        current_images = list_generated_images(state["thread_id"])
        prior_images = state.get("generated_images_seen") or set()
        files = [path for path in current_images if path not in prior_images]
        status = turn.get("status")
        runtime_state = load_runtime_state()
        runtime_state["active_chat_id"] = chat_id
        runtime_state["active_thread_id"] = state["thread_id"]
        runtime_state["active_turn_id"] = None
        runtime_state["last_turn_status"] = status
        runtime_state["updated_at"] = time.time()
        save_runtime_state(runtime_state)
        if status == "completed":
            if text or files:
                set_component_health("codex", "ok", detail="The most recent Codex turn completed successfully.")
                if state.get("recovery_id"):
                    self._remove_recovery(str(state["recovery_id"]))
                self.send_callback(chat_id, text, files)
            else:
                set_component_health(
                    "codex",
                    "warning",
                    category="empty_response",
                    detail="The last Codex turn ended without a final response; automatic recovery is active.",
                )
                recovery_state = self._queue_recovery(state, "completed without final assistant text")
                if recovery_state == "parked":
                    maximum = max(1, int(self.config.get("turn_recovery_max_attempts") or 5))
                    self.send_callback(
                        chat_id,
                        f"Automatic recovery stopped after {maximum} unsuccessful attempts. The task remains saved "
                        "without consuming more quota. Fix the underlying issue, then use /resume to retry or "
                        "/newsession to discard it.",
                    )
                else:
                    self.send_callback(
                        chat_id,
                        "Codex stopped before delivering a final response. The task was saved and will resume "
                        "automatically in the same thread when usage capacity is available. Use /stop to cancel it.",
                    )
        elif status == "interrupted":
            if state.get("recovery_id"):
                self._remove_recovery(str(state["recovery_id"]))
            self.send_callback(chat_id, "Turn interrupted.")
        elif status == "failed":
            turn_error = turn.get("error")
            turn_error_message = turn_error.get("message") if isinstance(turn_error, dict) else turn_error
            error = str(state.get("error") or turn_error_message or "Unknown error")
            category, guidance = classify_codex_failure(error)
            if category == "usage_limit":
                set_component_health("codex", "warning", category=category, detail=guidance)
                recovery_state = self._queue_recovery(state, error)
                if recovery_state == "parked":
                    maximum = max(1, int(self.config.get("turn_recovery_max_attempts") or 5))
                    self.send_callback(
                        chat_id,
                        f"Automatic recovery stopped after {maximum} unsuccessful attempts. The task remains saved. "
                        "Use /resume after fixing the underlying issue.",
                    )
                else:
                    self.send_callback(chat_id, "Codex hit a usage limit. The task was saved and will resume automatically after the limit resets.")
            else:
                set_component_health("codex", "error", category=category, detail=guidance)
                self._queue_recovery(state, error, force_park=True)
                self.send_callback(
                    chat_id,
                    f"The Codex turn failed, but your task was saved. {guidance} "
                    "Send /health for details or /stop to discard the saved task.",
                )
        else:
            self.send_callback(chat_id, f"Turn ended with status: {status}")


def maybe_start_reminder_loop(
    config: dict[str, Any],
    codex: CodexAppServerClient,
    chat_map: dict[str, Any],
    chat_map_lock: threading.Lock,
) -> None:
    if not config.get("enable_reminders", True):
        return

    def worker() -> None:
        while True:
            try:
                reminders = load_reminders()
                if reminders:
                    now = time.time()
                    kept: list[dict[str, Any]] = []
                    changed = False
                    for reminder in reminders:
                        if reminder.get("fired"):
                            changed = True
                            continue
                        due = parse_due(reminder.get("due"))
                        if due is None or now < due:
                            kept.append(reminder)
                            continue

                        chat_id = str(reminder.get("chat_id") or config.get("owner_chat_id") or "")
                        prompt = str(reminder.get("prompt") or "").strip()
                        if chat_id and prompt:
                            event_text = f'[SYSTEM EVENT source="reminder"] {prompt}'
                            with chat_map_lock:
                                codex.inject_external_message(chat_id, chat_map, event_text)
                                save_chat_map(chat_map)

                        recurring = str(reminder.get("recurring") or "").strip()
                        if recurring in {"daily", "weekly", "monthly"}:
                            reminder["due"] = advance_recurring(reminder["due"], recurring, after=now)
                            kept.append(reminder)
                        elif recurring:
                            print(
                                f"reminder {reminder.get('id', '?')} has unsupported recurrence {recurring!r}; treating as one-off",
                                file=sys.stderr,
                            )
                        changed = True

                    if changed:
                        # Preserve reminders concurrently added by Codex after this
                        # poll began. Reminder ids are required by the documented
                        # format and provide a stable merge key.
                        original_ids = {str(item.get("id") or "") for item in reminders}
                        current = load_reminders()
                        additions = [
                            item for item in current
                            if str(item.get("id") or "") not in original_ids
                        ]
                        save_reminders(kept + additions)
            except Exception as exc:
                print(f"reminder loop failed: {exc}", file=sys.stderr)
            time.sleep(REMINDER_CHECK_INTERVAL)

    threading.Thread(target=worker, daemon=True).start()


def parse_due(value: Any) -> float | None:
    if not value:
        return None
    text = str(value).strip()
    try:
        return time.mktime(time.strptime(text, "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        try:
            return time.mktime(time.strptime(text, "%Y-%m-%dT%H:%M"))
        except ValueError:
            return None


def advance_recurring(due: str, interval: str, *, after: float | None = None) -> str:
    try:
        current = datetime.strptime(str(due), "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        current = datetime.strptime(str(due), "%Y-%m-%dT%H:%M")
    after_dt = datetime.fromtimestamp(after if after is not None else time.time())

    def advance_once(value: datetime) -> datetime:
        if interval == "daily":
            return value + timedelta(days=1)
        if interval == "weekly":
            return value + timedelta(weeks=1)
        if interval == "monthly":
            year = value.year + (1 if value.month == 12 else 0)
            month = 1 if value.month == 12 else value.month + 1
            day = min(value.day, calendar.monthrange(year, month)[1])
            return value.replace(year=year, month=month, day=day)
        raise ValueError(f"Unsupported recurrence: {interval}")

    current = advance_once(current)
    while current <= after_dt:
        current = advance_once(current)
    return current.strftime("%Y-%m-%dT%H:%M:%S")


def maybe_start_email_loop(
    config: dict[str, Any],
    codex: CodexAppServerClient,
    chat_map: dict[str, Any],
    chat_map_lock: threading.Lock,
) -> None:
    if not config.get("enable_email_notifications"):
        return
    if not config.get("owner_chat_id"):
        return
    email_address = str(config.get("gmail_imap_email") or "").strip()
    app_password = str(config.get("gmail_imap_app_password") or "").strip()
    if not email_address or not app_password:
        set_component_health(
            "email",
            "error",
            category="configuration",
            detail="Proactive email notifications are enabled, but Gmail IMAP credentials are missing.",
        )
        return

    def worker() -> None:
        while True:
            try:
                owner_chat_id = str(config.get("owner_chat_id"))
                state = load_email_state()
                fresh, next_state = poll_unread_messages(
                    email_address,
                    app_password,
                    state,
                    max_results=10,
                )
                save_email_state(next_state)
                set_component_health(
                    "email",
                    "ok",
                    detail="The most recent read-only Gmail IMAP poll succeeded.",
                )
                if fresh:
                    lines = []
                    for message in fresh:
                        message_id = str(message.get("message_id") or "")
                        parts = []
                        if message_id:
                            parts.append(f"Message-ID: {message_id}")
                            parts.append(f"Gmail search: rfc822msgid:{message_id}")
                        parts.extend(
                            [
                                f"From: {message.get('from', '?')}",
                                f"Subject: {message.get('subject', '(no subject)')}",
                                f"Date: {message.get('date', '')}",
                            ]
                        )
                        lines.append("\n".join(parts))
                    content = (
                        "[SYSTEM EVENT source=\"email\"] New unread email(s):\n\n"
                        + "\n\n".join(lines)
                    )
                    with chat_map_lock:
                        codex.inject_external_message(owner_chat_id, chat_map, content)
                        save_chat_map(chat_map)
            except GmailImapError as exc:
                set_component_health(
                    "email",
                    "error",
                    category="imap",
                    detail=str(exc),
                )
                print(f"email loop failed: {exc}", file=sys.stderr)
            except Exception as exc:
                set_component_health(
                    "email",
                    "error",
                    category="unexpected",
                    detail=f"{type(exc).__name__}: {exc}",
                )
                print(f"email loop failed: {exc}", file=sys.stderr)
            time.sleep(EMAIL_CHECK_INTERVAL)

    threading.Thread(target=worker, daemon=True).start()


def maybe_start_version_monitor_loop(
    config: dict[str, Any],
    token: str,
    codex: CodexAppServerClient,
    chat_map: dict[str, Any],
    chat_map_lock: threading.Lock,
) -> None:
    owner_chat_id = str(config.get("owner_chat_id") or "").strip()
    codex_cmd = str(config.get("codex_cmd") or "codex")
    if not owner_chat_id:
        return

    state = load_version_state()
    initial_version = current_codex_version(codex_cmd)
    if initial_version:
        state["last_known_version"] = initial_version
        save_version_state(state)

    def worker() -> None:
        while True:
            try:
                current_version = current_codex_version(codex_cmd)
                if not current_version:
                    time.sleep(VERSION_CHECK_INTERVAL)
                    continue

                version_state = load_version_state()
                previous_version = str(version_state.get("last_known_version") or "").strip()
                if not previous_version:
                    version_state["last_known_version"] = current_version
                    save_version_state(version_state)
                elif previous_version != current_version:
                    version_state["last_known_version"] = current_version
                    save_version_state(version_state)
                    send_message(
                        token,
                        owner_chat_id,
                        (
                            f"Codex CLI updated on disk:\n{previous_version} -> {current_version}\n\n"
                            "The running bridge may still be using the older app-server process. "
                            "Send /newsession to restart the bridge/app-server and make the next turn fresh."
                        ),
                    )
                    with chat_map_lock:
                        codex.inject_external_message(
                            owner_chat_id,
                            chat_map,
                            (
                                f'[SYSTEM EVENT source="version-monitor"] Codex CLI changed from '
                                f"{previous_version} to {current_version}. "
                                "Audit for hook, app-server, or approval-flow regressions and report any findings."
                            ),
                        )
                        save_chat_map(chat_map)
            except Exception as exc:
                print(f"version monitor failed: {exc}", file=sys.stderr)
            time.sleep(VERSION_CHECK_INTERVAL)

    threading.Thread(target=worker, daemon=True).start()


def shutil_which(name: str) -> str | None:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / name
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def current_codex_version(codex_cmd: str) -> str | None:
    try:
        proc = subprocess.run(
            [codex_cmd, "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except Exception:
        return None
    output = (proc.stdout or proc.stderr or "").strip()
    return output or None


def update_long_term_memory_agents_file(config: dict[str, Any]) -> None:
    if not LONG_TERM_MEMORY_AGENTS_SCRIPT.exists():
        return
    try:
        proc = subprocess.run(
            [
                sys.executable,
                str(LONG_TERM_MEMORY_AGENTS_SCRIPT),
                "--cwd",
                str(config.get("default_cwd") or Path.home()),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=45,
        )
    except Exception as exc:
        print(f"long-term-memory AGENTS.md update failed: {exc}", file=sys.stderr)
        return

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        print(f"long-term-memory AGENTS.md update failed: {detail}", file=sys.stderr)


def get_bot_username(token: str) -> str:
    result = telegram_request(token, "getMe", {})
    return ((result.get("result") or {}).get("username") or "").strip()


def extract_message_text(
    message: dict[str, Any],
    token: str,
    config: dict[str, Any],
    on_transcription_notice: Callable[[str], None] | None = None,
) -> str:
    text = (message.get("text") or message.get("caption") or "").strip()

    voice = message.get("voice")
    if voice:
        if config.get("enable_voice_transcription", True):
            api_key = str(config.get("openai_api_key") or "")
            if not api_key:
                detail = "The OpenAI API key is missing."
                changed = set_component_health("transcription", "error", category="configuration", detail=detail)
                notify_transcription_failure(on_transcription_notice, detail, changed=changed)
                return text or "(voice message; transcription unavailable)"
            fetched = fetch_telegram_file(token, voice.get("file_id"))
            if fetched is not None:
                file_bytes, filename, mime_type = fetched
                transcript = transcribe_audio(
                    file_bytes,
                    filename,
                    mime_type,
                    api_key,
                    on_notice=on_transcription_notice,
                )
                if transcript:
                    return f"🎤 {transcript}"
            else:
                detail = "Telegram could not download the voice file for transcription."
                changed = set_component_health("transcription", "error", category="telegram_download", detail=detail)
                notify_transcription_failure(on_transcription_notice, detail, changed=changed)
        return text or "(voice message; transcription unavailable)"

    audio = message.get("audio")
    if audio:
        return text or f"(audio: {audio.get('title') or audio.get('file_name') or 'audio'})"

    video = message.get("video")
    if video:
        return text or "(video)"

    video_note = message.get("video_note")
    if video_note:
        return text or "(video note)"

    document = message.get("document")
    if document:
        return text or f"(document: {document.get('file_name') or 'document'})"

    photo = message.get("photo")
    if photo:
        return text or "(photo)"

    sticker = message.get("sticker")
    if sticker:
        emoji = str(sticker.get("emoji") or "").strip()
        return text or (f"(sticker {emoji})" if emoji else "(sticker)")

    return text


def extract_attachment_meta(message: dict[str, Any], token: str) -> dict[str, Any]:
    photo = message.get("photo")
    if isinstance(photo, list) and photo:
        selected = photo[-1]
        file_id = str(selected.get("file_id") or "").strip()
        if file_id:
            image_path = download_attachment_to_dir(token, file_id, INBOX_DIR, preferred_name="telegram-photo.jpg")
            meta: dict[str, Any] = {
                "attachment_kind": "photo",
                "attachment_file_id": file_id,
            }
            if image_path:
                meta["image_path"] = image_path
            size = selected.get("file_size")
            if size is not None:
                meta["attachment_size"] = str(size)
            return meta

    document = message.get("document")
    if isinstance(document, dict) and document.get("file_id"):
        meta = attachment_meta(
            kind="document",
            file_id=document.get("file_id"),
            size=document.get("file_size"),
            mime=document.get("mime_type"),
            name=document.get("file_name"),
        )
        file_path = download_attachment_to_dir(
            token,
            str(document.get("file_id") or ""),
            INBOX_DIR,
            preferred_name=document.get("file_name"),
        )
        if file_path:
            meta["file_path"] = file_path
        return meta

    audio = message.get("audio")
    if isinstance(audio, dict) and audio.get("file_id"):
        return attachment_meta(
            kind="audio",
            file_id=audio.get("file_id"),
            size=audio.get("file_size"),
            mime=audio.get("mime_type"),
            name=audio.get("file_name") or audio.get("title"),
        )

    video = message.get("video")
    if isinstance(video, dict) and video.get("file_id"):
        return attachment_meta(
            kind="video",
            file_id=video.get("file_id"),
            size=video.get("file_size"),
            mime=video.get("mime_type"),
            name=video.get("file_name"),
        )

    video_note = message.get("video_note")
    if isinstance(video_note, dict) and video_note.get("file_id"):
        return attachment_meta(
            kind="video_note",
            file_id=video_note.get("file_id"),
            size=video_note.get("file_size"),
            mime=video_note.get("mime_type"),
            name="video-note.mp4",
        )

    voice = message.get("voice")
    if isinstance(voice, dict) and voice.get("file_id"):
        return attachment_meta(
            kind="voice",
            file_id=voice.get("file_id"),
            size=voice.get("file_size"),
            mime=voice.get("mime_type"),
            name="voice.ogg",
        )

    return {}


def attachment_meta(kind: str, file_id: Any, size: Any, mime: Any, name: Any) -> dict[str, Any]:
    meta = {
        "attachment_kind": kind,
        "attachment_file_id": str(file_id or ""),
    }
    if size not in (None, ""):
        meta["attachment_size"] = str(size)
    if mime:
        meta["attachment_mime"] = safe_attr(str(mime))
    if name:
        meta["attachment_name"] = safe_name(str(name))
    return meta


def message_timezone(timezone_name: str = "") -> Any:
    if timezone_name:
        try:
            return ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return datetime.now().astimezone().tzinfo or timezone.utc


def format_telegram_sent_at(value: Any, timezone_name: str = "") -> str:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).astimezone(
            message_timezone(timezone_name)
        ).strftime("%Y-%m-%d %H:%M %Z")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def build_channel_message(
    message: dict[str, Any],
    text: str,
    attachment: dict[str, Any],
    timezone_name: str = "",
) -> str:
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    telegram_timestamp = message.get("date")
    attrs = {
        "source": "telegram",
        "chat_id": str(chat.get("id") or ""),
        "message_id": str(message.get("message_id") or ""),
        "user": str(sender.get("id") or ""),
        "ts": str(telegram_timestamp or ""),
        "sent_at": format_telegram_sent_at(telegram_timestamp, timezone_name),
    }
    attrs.update({key: str(value) for key, value in attachment.items() if value not in (None, "")})
    attr_text = " ".join(f'{key}="{safe_attr(value)}"' for key, value in attrs.items() if value)
    body = text.strip() or "(empty message)"
    return f"<channel {attr_text}>{body}"


def safe_attr(value: str) -> str:
    return value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def safe_name(value: str) -> str:
    return re.sub(r'[<>\[\]\r\n;"]', "_", value)


def build_auto_user_input_answers(params: dict[str, Any]) -> dict[str, str]:
    questions = params.get("questions")
    if not isinstance(questions, list):
        request = params.get("request")
        if isinstance(request, dict):
            questions = request.get("questions")
    if not isinstance(questions, list):
        return {}

    answers: dict[str, str] = {}
    for question in questions:
        if not isinstance(question, dict):
            continue
        question_id = str(question.get("id") or question.get("key") or question.get("name") or "").strip()
        if not question_id:
            continue
        options = question.get("options")
        if isinstance(options, list) and options:
            recommended = next(
                (
                    option for option in options
                    if isinstance(option, dict)
                    and "recommended" in str(option.get("label") or "").lower()
                ),
                None,
            )
            if recommended is not None:
                answer = recommended.get("label") or recommended.get("value") or recommended.get("id") or ""
            else:
                answer = question.get("default") or ""
        else:
            answer = question.get("default") or ""
        answers[question_id] = str(answer)
    return {key: value for key, value in answers.items() if value != ""}


def update_outcome_message(state: dict[str, Any]) -> str | None:
    """Build the owner notification for a finished runtime update, if one is due."""
    if not isinstance(state, dict) or state.get("announced"):
        return None
    status = state.get("status")
    commit = str(state.get("commit") or "")[:12]
    if status == "completed":
        return (
            f"Runtime update complete at commit {commit}. The bridge restarted; "
            "any task interrupted by the restart resumes automatically.\n"
            "Send /newsession before testing updated MCP tools or skills."
        )
    if status == "failed":
        reason = str(state.get("error") or "unknown error")
        return (
            f"Runtime update to {state.get('ref') or commit or 'requested ref'} FAILED: {reason}\n"
            "The previous runtime was restored and is still running. "
            "Log: ~/.codex/telegram-bridge/update-handoff.log"
        )
    return None


def announce_update_outcome(token: str, config: dict[str, Any]) -> bool:
    owner_chat_id = str(config.get("owner_chat_id") or "").strip()
    if not owner_chat_id:
        return False
    state = load_json(UPDATE_STATE_FILE, {})
    text = update_outcome_message(state if isinstance(state, dict) else {})
    if not text:
        return False
    try:
        send_message(token, owner_chat_id, text)
    except Exception as exc:
        print(f"update announcement failed: {exc}", file=sys.stderr)
        return False
    state["announced"] = True
    save_json(UPDATE_STATE_FILE, state)
    return True


def start_update_announcement_loop(token: str, config: dict[str, Any]) -> None:
    """Watch for a finished runtime update and notify the owner exactly once.

    A periodic check (not just a startup check) is required: the updater
    restarts the bridge midway through and only writes the final
    completed/failed status after the new bridge is already running.
    """
    if not str(config.get("owner_chat_id") or "").strip():
        return

    def worker() -> None:
        while True:
            try:
                announce_update_outcome(token, config)
            except Exception as exc:
                print(f"update announcement loop failed: {exc}", file=sys.stderr)
            time.sleep(30)

    threading.Thread(target=worker, daemon=True, name="update-announce").start()


def memory_health_snapshot() -> dict[str, Any]:
    health = load_json(MEMORY_HEALTH_FILE, {})
    alert = load_json(MEMORY_ALERT_FILE, {})
    return {
        "health": health if isinstance(health, dict) else {},
        "alert": alert if isinstance(alert, dict) else {},
        "pending": MEMORY_TASK_FILE.exists(),
        "worker_running": _pid_file_alive(MEMORY_PID_FILE),
    }


def _pid_file_alive(path: Path) -> bool:
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def compact_health_summary() -> str:
    problems: list[str] = []
    bridge_config = load_json(CONFIG_FILE, {})
    memory_config = load_json(MEMORY_STATE_DIR / "config.json", {})
    memory = memory_health_snapshot()
    if memory["alert"]:
        problems.append("memory summaries parked; use /retrymemory")
    elif memory["health"].get("status") == "error":
        problems.append("memory API retrying")
    transcription = component_health("transcription")
    if transcription.get("status") == "error":
        problems.append("voice transcription unavailable")
    codex_health = component_health("codex")
    if codex_health.get("status") == "error":
        problems.append("Codex needs attention")
    email_health = component_health("email")
    if bridge_config.get("enable_email_notifications") and email_health.get("status") == "error":
        problems.append("email polling unavailable")
    calendar_health = load_json(MEMORY_STATE_DIR / "calendar_health.json", {})
    if (
        memory_config.get("enable_calendar")
        and isinstance(calendar_health, dict)
        and calendar_health.get("status") == "error"
    ):
        problems.append("calendar feed unavailable")
    return "Health: OK" if not problems else f"Health: ATTENTION — {'; '.join(problems)}. Use /health."


def setup_repair_command() -> str:
    return f'python3 "{SETUP_SCRIPT}"'


def find_google_app(apps: dict[str, dict[str, Any]], kind: str) -> dict[str, Any]:
    wanted = str(kind or "").strip().lower()
    for app in apps.values():
        if not isinstance(app, dict):
            continue
        identity = " ".join(
            str(app.get(field) or "")
            for field in ("id", "name", "title", "displayName", "slug", "pluginName")
        ).lower()
        compact = re.sub(r"[^a-z0-9]+", "", identity)
        if wanted == "gmail" and "gmail" in compact:
            return app
        if wanted == "calendar" and "googlecalendar" in compact:
            return app
    return {}


def google_app_is_connected(app: dict[str, Any]) -> bool:
    return bool(app.get("isEnabled")) and bool(app.get("isAccessible"))


def health_text(
    *,
    google_apps: dict[str, dict[str, Any]] | None = None,
    google_apps_error: str = "",
) -> str:
    lines = ["System health", "", "✅ Bridge: running", "✅ Codex app-server: connected"]
    bridge_config = load_json(CONFIG_FILE, {})
    memory_config = load_json(MEMORY_STATE_DIR / "config.json", {})
    codex_health = component_health("codex")
    if codex_health.get("status") == "error":
        lines[-1] = f"❌ Codex: {codex_health.get('detail') or 'the last turn failed'}"
    elif codex_health.get("status") == "warning":
        lines[-1] = f"⚠️ Codex: {codex_health.get('detail') or 'temporarily unavailable'}"

    memory = memory_health_snapshot()
    memory_health = memory["health"]
    if memory["alert"]:
        detail = str(memory["alert"].get("last_error") or "maintenance made no progress")
        lines.append(f"❌ Memory summaries: parked — {detail[:300]}")
        lines.append(
            "   Raw conversation is still being saved. Rerun setup to replace the OpenAI key if needed, then use /retrymemory."
        )
    elif memory["pending"]:
        detail = str(memory_health.get("detail") or "background work is queued")
        worker = "worker running" if memory["worker_running"] else "waiting for retry worker"
        lines.append(f"⚠️ Memory summaries: pending ({worker}) — {detail[:240]}")
    elif memory_health.get("status") == "error":
        lines.append(f"⚠️ Memory summaries: last API request failed — {memory_health.get('detail') or 'unknown error'}")
    else:
        lines.append("✅ Memory summaries: ready")

    transcription = component_health("transcription")
    if transcription.get("status") == "error":
        lines.append(f"❌ Voice transcription: {transcription.get('detail') or 'unavailable'}")
        lines.append(
            "   Text messages still work. Check API billing or rerun setup to replace the OpenAI key, then resend the voice message."
        )
    elif transcription.get("status") == "ok":
        lines.append("✅ Voice transcription: working")
    else:
        lines.append("✅ Voice transcription: configured (not tested since this runtime started)")

    email = component_health("email")
    if not bridge_config.get("enable_email_notifications"):
        lines.append("ℹ️ Email notifications: disabled")
    elif email.get("status") == "error":
        lines.append(f"❌ Email notifications: {email.get('detail') or 'Gmail IMAP polling failed'}")
        lines.append(f"   The bridge keeps retrying. Repair only the affected setting by rerunning: {setup_repair_command()}")
    elif email.get("status") == "ok":
        lines.append("✅ Email notifications: read-only Gmail IMAP polling works")
    else:
        lines.append("ℹ️ Email notifications: not enabled or not tested")

    calendar_health = load_json(MEMORY_STATE_DIR / "calendar_health.json", {})
    if not memory_config.get("enable_calendar"):
        lines.append("ℹ️ Calendar context: disabled")
    elif isinstance(calendar_health, dict) and calendar_health.get("status") == "error":
        lines.append(f"❌ Calendar context: {calendar_health.get('detail') or 'private iCal retrieval failed'}")
        lines.append(f"   Rerun setup to keep working values and replace the private iCal URL: {setup_repair_command()}")
    elif isinstance(calendar_health, dict) and calendar_health.get("status") == "warning":
        lines.append(f"⚠️ Calendar context: {calendar_health.get('detail') or 'using a cached calendar feed'}")
        lines.append("   Recent cached calendar data remains available while the plugin retries.")
    elif isinstance(calendar_health, dict) and calendar_health.get("status") == "ok":
        lines.append("✅ Calendar context: private iCal retrieval works")
    else:
        lines.append("ℹ️ Calendar context: not enabled or not tested")

    update_state = load_json(UPDATE_STATE_FILE, {})
    if isinstance(update_state, dict) and update_state.get("status") == "failed":
        lines.append(f"⚠️ Last update: failed — {str(update_state.get('error') or 'unknown error')[:240]}")
    else:
        lines.append("✅ Runtime updates: no unresolved failure")

    if not bridge_config.get("enable_google_apps"):
        lines.append("ℹ️ Official Gmail/Calendar apps: disabled")
    elif google_apps_error:
        lines.append(f"⚠️ Official Gmail/Calendar apps: could not check — {google_apps_error[:240]}")
    elif google_apps is None:
        lines.append("⚠️ Official Gmail/Calendar apps: connection status was not checked")
    else:
        gmail = find_google_app(google_apps, "gmail")
        calendar_app = find_google_app(google_apps, "calendar")
        if google_app_is_connected(gmail):
            lines.append("✅ Official Gmail app: connected and accessible")
        else:
            lines.append("❌ Official Gmail app: not connected or inaccessible — run codex, enter /apps, and reconnect Gmail")
        if google_app_is_connected(calendar_app):
            lines.append("✅ Official Google Calendar app: connected and accessible")
        else:
            lines.append(
                "❌ Official Google Calendar app: not connected or inaccessible — run codex, enter /apps, and reconnect Google Calendar"
            )

    if any(line.startswith(("❌", "⚠️")) for line in lines):
        lines.extend(
            [
                "",
                "Saved working settings, pairing, allowlists, and memory history are preserved when setup is rerun.",
                f"Setup/repair command: {setup_repair_command()}",
            ]
        )
    return "\n".join(lines)


def retry_memory_maintenance() -> tuple[bool, str]:
    if not MEMORY_COMMON_SCRIPT.is_file():
        return False, f"Memory worker not found at {MEMORY_COMMON_SCRIPT}."
    if not MEMORY_ALERT_FILE.exists() and not MEMORY_TASK_FILE.exists():
        return False, "Memory maintenance is not parked or pending."
    try:
        proc = subprocess.run(
            [sys.executable, str(MEMORY_COMMON_SCRIPT), "--retry-memory-maintenance"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except Exception as exc:
        return False, f"Could not start memory retry: {exc}"
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "memory retry failed").strip()
    return True, "Memory maintenance retry started. Use /health to check its progress."


def _component_alert_transition(
    notifications: dict[str, Any],
    *,
    key: str,
    enabled: bool,
    health: dict[str, Any],
    problem_statuses: set[str],
    problem_message: Callable[[str, str], str],
    recovery_message: str,
) -> list[str]:
    fingerprint_key = f"{key}_fingerprint"
    active_key = f"{key}_active"
    if not enabled:
        notifications.pop(fingerprint_key, None)
        notifications.pop(active_key, None)
        return []

    status = str(health.get("status") or "").strip().lower()
    detail = str(health.get("detail") or "").strip()
    if status in problem_statuses:
        fingerprint = f"{status}|{detail}"
        if notifications.get(fingerprint_key) == fingerprint:
            return []
        notifications[fingerprint_key] = fingerprint
        notifications[active_key] = True
        return [problem_message(status, detail)]

    if status == "ok" and notifications.get(active_key):
        notifications[active_key] = False
        notifications.pop(fingerprint_key, None)
        return [recovery_message]
    return []


def google_background_alert_messages(
    notifications: dict[str, Any],
    bridge_config: dict[str, Any],
    memory_config: dict[str, Any],
    email_health: dict[str, Any],
    calendar_health: dict[str, Any],
) -> list[str]:
    repair = setup_repair_command()
    messages = _component_alert_transition(
        notifications,
        key="email",
        enabled=bool(bridge_config.get("enable_email_notifications")),
        health=email_health,
        problem_statuses={"error"},
        problem_message=lambda _status, detail: (
            "Proactive Gmail notifications stopped working.\n"
            f"Reason: {(detail or 'Gmail IMAP polling failed')[:500]}\n"
            "The bridge will keep retrying and will not advance its email checkpoint. "
            "Rerun setup, keep every working value, and replace only the Gmail address or app password if needed:\n"
            f"{repair}\n"
            "Use /health for the latest status."
        ),
        recovery_message="Proactive Gmail notifications are working again. Any queued unread notices will continue draining normally.",
    )

    def calendar_problem(status: str, detail: str) -> str:
        if status == "warning":
            heading = "Calendar context is temporarily degraded; recent cached data is still being used when available."
        else:
            heading = "Calendar context is unavailable, so upcoming events may be absent from Codex memory."
        return (
            f"{heading}\n"
            f"Reason: {(detail or 'private iCal retrieval failed')[:500]}\n"
            "The plugin will keep retrying. If the private calendar address changed, rerun setup, keep every working value, "
            "and replace only the iCal URL:\n"
            f"{repair}\n"
            "Use /health for the latest status."
        )

    messages.extend(
        _component_alert_transition(
            notifications,
            key="calendar",
            enabled=bool(memory_config.get("enable_calendar")),
            health=calendar_health,
            problem_statuses={"warning", "error"},
            problem_message=calendar_problem,
            recovery_message="Private calendar context is working again and the next memory snapshot will use fresh events.",
        )
    )
    return messages


def start_health_alert_loop(token: str, config: dict[str, Any]) -> None:
    owner_chat_id = str(config.get("owner_chat_id") or "").strip()
    if not owner_chat_id:
        return

    def worker() -> None:
        while True:
            try:
                notifications = load_json(HEALTH_NOTIFICATION_FILE, {})
                if not isinstance(notifications, dict):
                    notifications = {}
                alert = load_json(MEMORY_ALERT_FILE, {})
                alert = alert if isinstance(alert, dict) else {}
                memory_health = load_json(MEMORY_HEALTH_FILE, {})
                memory_health = memory_health if isinstance(memory_health, dict) else {}
                if alert:
                    fingerprint = str(alert.get("stuck_at") or json.dumps(alert, sort_keys=True))
                    if notifications.get("memory_alert") != fingerprint:
                        detail = str(alert.get("last_error") or "maintenance made no progress")[:500]
                        send_message(
                            token,
                            owner_chat_id,
                            "Long-term memory summaries have paused after repeated failures.\n"
                            f"Reason: {detail}\n"
                            "Raw conversation is still being saved, so nothing is being discarded. "
                            "Fix the OpenAI API key, billing, model, or network problem, then send /retrymemory.",
                        )
                        notifications["memory_alert"] = fingerprint
                        notifications["memory_alert_active"] = True
                        save_json(HEALTH_NOTIFICATION_FILE, notifications)
                elif memory_health.get("status") == "error":
                    error_fingerprint = f"{memory_health.get('category')}|{memory_health.get('detail')}"
                    if notifications.get("memory_error") != error_fingerprint:
                        detail = str(memory_health.get("detail") or "the OpenAI memory request failed")[:500]
                        send_message(
                            token,
                            owner_chat_id,
                            "Long-term memory had an API failure and is retrying in the background.\n"
                            f"Reason: {detail}\n"
                            "Raw conversation is still being saved. Use /health to follow its status.",
                        )
                        notifications["memory_error"] = error_fingerprint
                        notifications["memory_error_active"] = True
                        save_json(HEALTH_NOTIFICATION_FILE, notifications)
                elif memory_health.get("status") == "ok" and (
                    notifications.get("memory_alert_active") or notifications.get("memory_error_active")
                ):
                    send_message(token, owner_chat_id, "Long-term memory summaries are working again.")
                    notifications["memory_alert_active"] = False
                    notifications["memory_error_active"] = False
                    save_json(HEALTH_NOTIFICATION_FILE, notifications)

                bridge_config = load_json(CONFIG_FILE, {})
                bridge_config = bridge_config if isinstance(bridge_config, dict) else {}
                memory_config = load_json(MEMORY_STATE_DIR / "config.json", {})
                memory_config = memory_config if isinstance(memory_config, dict) else {}
                email_health = component_health("email")
                calendar_health = load_json(MEMORY_STATE_DIR / "calendar_health.json", {})
                calendar_health = calendar_health if isinstance(calendar_health, dict) else {}
                google_notification_state_before = json.dumps(notifications, sort_keys=True)
                google_messages = google_background_alert_messages(
                    notifications,
                    bridge_config,
                    memory_config,
                    email_health,
                    calendar_health,
                )
                for message in google_messages:
                    send_message(token, owner_chat_id, message)
                if google_messages or json.dumps(notifications, sort_keys=True) != google_notification_state_before:
                    save_json(HEALTH_NOTIFICATION_FILE, notifications)
            except Exception as exc:
                print(f"health alert loop failed: {exc}", file=sys.stderr)
            time.sleep(HEALTH_CHECK_INTERVAL)

    threading.Thread(target=worker, daemon=True, name="health-alerts").start()


def help_text() -> str:
    command_lines = "\n".join(f"/{item['command']} - {item['description']}" for item in BOT_COMMANDS)
    return (
        f"Available commands:\n{command_lines}\n\n"
        "Text, voice, photos, and file metadata are forwarded to Codex."
    )


def configure_bot_command_menu(token: str) -> bool:
    try:
        set_bot_commands(token, BOT_COMMANDS)
        return True
    except Exception as exc:
        print(f"telegram command menu setup failed: {exc}", file=sys.stderr)
        return False


def main() -> None:
    config = load_config()
    token = config.get("bot_token")
    if not token:
        raise SystemExit(
            "Missing Telegram bot token. Set bot_token in ~/.codex/telegram-bridge/config.json or TELEGRAM_BOT_TOKEN in ~/.codex/telegram-bridge/.env."
        )

    bot_username = get_bot_username(str(token))
    configure_bot_command_menu(str(token))
    start_update_announcement_loop(str(token), config)
    start_health_alert_loop(str(token), config)
    chat_map = load_chat_map()
    chat_map_lock = threading.Lock()
    # Resume from the last seen Telegram update to avoid re-processing
    # commands (like /newsession) that would cause a restart loop.
    runtime = load_runtime_state()
    offset = int(runtime.get("telegram_update_offset") or 0)

    def send_callback(chat_id: str, text: str, files: list[str] | None = None) -> None:
        access = load_access()
        with chat_map_lock:
            entry = dict(chat_map.get(chat_id, {}))
        reply_to_message_id = entry.get("last_message_id")
        if not isinstance(reply_to_message_id, int):
            reply_to_message_id = None
        sent = send_message(
            str(token),
            chat_id,
            text,
            reply_to_message_id=reply_to_message_id,
            access=access,
            files=files or [],
        )
        if sent:
            with chat_map_lock:
                state_entry = chat_map.setdefault(chat_id, {})
                state_entry["last_outbound_message_ids"] = [
                    int(item["message_id"]) for item in sent if isinstance(item.get("message_id"), int)
                ]
                if state_entry["last_outbound_message_ids"]:
                    state_entry["last_outbound_message_id"] = state_entry["last_outbound_message_ids"][-1]
                save_chat_map(chat_map)
                save_runtime_state(
                    {
                        **load_runtime_state(),
                        "active_chat_id": chat_id,
                        "last_outbound_message_id": state_entry.get("last_outbound_message_id"),
                        "last_outbound_message_ids": state_entry.get("last_outbound_message_ids", []),
                        "last_inbound_message_id": state_entry.get("last_message_id"),
                        "updated_at": time.time(),
                    }
                )

    update_long_term_memory_agents_file(config)
    codex = CodexAppServerClient(config, send_callback)
    codex.start_recovery_loop()
    maybe_start_reminder_loop(config, codex, chat_map, chat_map_lock)
    maybe_start_email_loop(config, codex, chat_map, chat_map_lock)
    maybe_start_version_monitor_loop(config, str(token), codex, chat_map, chat_map_lock)

    print("Codex Telegram bridge is running.")
    while True:
        pending_update = load_json(PENDING_UPDATE_FILE, {})
        if isinstance(pending_update, dict) and pending_update.get("update_id") is not None:
            result = {"result": [pending_update]}
        else:
            try:
                result = telegram_request(
                    str(token),
                    "getUpdates",
                    {
                        "timeout": "30",
                        "offset": str(offset),
                    },
                )
            except Exception as exc:
                print(f"telegram poll failed: {exc}", file=sys.stderr)
                time.sleep(3)
                continue

        for update in result.get("result", []):
            try:
                # Journal before acknowledging the Telegram offset. If processing
                # crashes, the bridge replays this update before polling again.
                save_json(PENDING_UPDATE_FILE, update)
                offset = max(offset, int(update["update_id"]) + 1)
                # Telegram itself may advance because the durable journal now owns
                # retry responsibility for this update.
                save_runtime_state({**load_runtime_state(), "telegram_update_offset": offset})
                callback = update.get("callback_query")
                if callback:
                    access = load_access()
                    handle_model_callback(str(token), callback, config, access)
                    PENDING_UPDATE_FILE.unlink(missing_ok=True)
                    continue

                message = update.get("message") or update.get("edited_message")
                if not message:
                    PENDING_UPDATE_FILE.unlink(missing_ok=True)
                    continue

                if not gate_message(message, str(token), bot_username):
                    PENDING_UPDATE_FILE.unlink(missing_ok=True)
                    continue

                chat = message.get("chat", {})
                chat_id = str(chat.get("id"))
                sender_id = str((message.get("from") or {}).get("id"))
                access = load_access()
                text = extract_message_text(
                    message,
                    str(token),
                    config,
                    on_transcription_notice=lambda notice: send_message(
                        str(token),
                        chat_id,
                        notice,
                        message.get("message_id"),
                        access=access,
                    ),
                ).strip()
                attachment = extract_attachment_meta(message, str(token))

                if message.get("voice") and text == "(voice message; transcription unavailable)":
                    # The user has already received a precise transcription
                    # failure notice. Do not spend a Codex turn on a placeholder.
                    PENDING_UPDATE_FILE.unlink(missing_ok=True)
                    continue

                with chat_map_lock:
                    entry = chat_map.setdefault(chat_id, {})
                    entry["last_message_id"] = message.get("message_id")
                    entry["last_sender_id"] = sender_id
                    entry["last_seen_at"] = time.time()
                    entry["chat_type"] = chat.get("type")
                    save_chat_map(chat_map)
                    save_runtime_state(
                        {
                            **load_runtime_state(),
                            "active_chat_id": chat_id,
                            "last_inbound_message_id": message.get("message_id"),
                            "last_sender_id": sender_id,
                            "chat_type": chat.get("type"),
                            "updated_at": time.time(),
                        }
                    )

                if not text:
                    send_message(str(token), chat_id, "This message type is not supported yet.", access=access)
                    PENDING_UPDATE_FILE.unlink(missing_ok=True)
                    continue

                command = normalize_command(text, bot_username)
                if command.startswith("/") and chat.get("type") != "private":
                    PENDING_UPDATE_FILE.unlink(missing_ok=True)
                    continue
                if command in {"/start", "/help"}:
                    send_message(str(token), chat_id, help_text(), message.get("message_id"), access=access)
                    PENDING_UPDATE_FILE.unlink(missing_ok=True)
                    continue
                if command == "/status":
                    send_message(
                        str(token),
                        chat_id,
                        codex.status_text(chat_id, chat_map),
                        message.get("message_id"),
                        access=access,
                    )
                    PENDING_UPDATE_FILE.unlink(missing_ok=True)
                    continue
                if command == "/health":
                    google_apps = None
                    google_apps_error = ""
                    if config.get("enable_google_apps"):
                        try:
                            google_apps = codex.list_apps()
                        except Exception as exc:
                            google_apps_error = f"{type(exc).__name__}: {exc}"
                    send_message(
                        str(token),
                        chat_id,
                        health_text(
                            google_apps=google_apps,
                            google_apps_error=google_apps_error,
                        ),
                        message.get("message_id"),
                        access=access,
                    )
                    PENDING_UPDATE_FILE.unlink(missing_ok=True)
                    continue
                if command == "/model":
                    handle_model_command(
                        str(token),
                        chat_id,
                        text,
                        message.get("message_id"),
                        config,
                        access,
                    )
                    PENDING_UPDATE_FILE.unlink(missing_ok=True)
                    continue
                if command == "/resume":
                    if codex.retry_parked_recovery(chat_id):
                        send_message(
                            str(token),
                            chat_id,
                            "The parked task is queued for another recovery cycle.",
                            message.get("message_id"),
                            access=access,
                        )
                    else:
                        send_message(
                            str(token),
                            chat_id,
                            "There is no parked task to resume.",
                            message.get("message_id"),
                            access=access,
                        )
                    PENDING_UPDATE_FILE.unlink(missing_ok=True)
                    continue
                if command == "/retrymemory":
                    ok, detail = retry_memory_maintenance()
                    send_message(
                        str(token),
                        chat_id,
                        detail,
                        message.get("message_id"),
                        access=access,
                    )
                    PENDING_UPDATE_FILE.unlink(missing_ok=True)
                    continue
                if command == "/update":
                    parts = text.strip().split()
                    ref = parts[1] if len(parts) > 1 else "main"
                    if not re.fullmatch(r"[A-Za-z0-9._/-]{1,64}", ref):
                        send_message(str(token), chat_id, "That does not look like a valid git ref.", message.get("message_id"), access=access)
                        continue
                    if codex.has_active_turn(chat_id):
                        send_message(
                            str(token),
                            chat_id,
                            "A turn is still running. Send /stop first, or wait for it to finish — the update restart would kill it mid-flight.",
                            message.get("message_id"),
                            access=access,
                        )
                        continue
                    if not UPDATE_SCRIPT.is_file():
                        send_message(str(token), chat_id, f"Updater not found at {UPDATE_SCRIPT}.", message.get("message_id"), access=access)
                        continue
                    detail = ""
                    try:
                        scheduled = subprocess.run(
                            [sys.executable, str(UPDATE_SCRIPT), "--ref", ref, "--defer-seconds", "10"],
                            capture_output=True,
                            text=True,
                            timeout=60,
                        )
                        ok = scheduled.returncode == 0
                        detail = (scheduled.stderr or scheduled.stdout or "").strip()
                    except Exception as exc:
                        ok = False
                        detail = str(exc)
                    if ok:
                        send_message(
                            str(token),
                            chat_id,
                            f"Update to '{ref}' scheduled. The bridge restarts in about 10 seconds and will confirm here once the update finishes.",
                            message.get("message_id"),
                            access=access,
                        )
                    else:
                        send_message(str(token), chat_id, f"Could not schedule the update: {detail or 'unknown error'}", message.get("message_id"), access=access)
                    PENDING_UPDATE_FILE.unlink(missing_ok=True)
                    continue
                if command == "/newsession":
                    codex.cancel_recovery(chat_id)
                    update_long_term_memory_agents_file(config)
                    with chat_map_lock:
                        entry = chat_map.setdefault(chat_id, {})
                        entry.pop("thread_id", None)
                        entry.pop("created_at", None)
                        save_chat_map(chat_map)
                        save_runtime_state(
                            {
                                **load_runtime_state(),
                                "active_chat_id": chat_id,
                                "active_thread_id": None,
                                "active_turn_id": None,
                                "updated_at": time.time(),
                            }
                        )
                    send_message(
                        str(token),
                        chat_id,
                        "Restarting the bridge and Codex app-server.\n"
                        "Your next message will start a fresh Codex thread.",
                        message.get("message_id"),
                        access=access,
                    )
                    PENDING_UPDATE_FILE.unlink(missing_ok=True)
                    codex.shutdown()
                    return
                if command == "/stop":
                    interrupted = codex.interrupt_turn(chat_id)
                    cancelled = codex.cancel_recovery(chat_id)
                    if interrupted:
                        suffix = " Pending automatic recovery was also cancelled." if cancelled else ""
                        send_message(str(token), chat_id, f"Interrupt requested.{suffix}", message.get("message_id"), access=access)
                    elif cancelled:
                        send_message(str(token), chat_id, "Cancelled the pending automatic recovery.", message.get("message_id"), access=access)
                    else:
                        send_message(
                            str(token),
                            chat_id,
                            "No active turn to interrupt.",
                            message.get("message_id"),
                            access=access,
                        )
                    PENDING_UPDATE_FILE.unlink(missing_ok=True)
                    continue

                text = build_channel_message(
                    message,
                    text,
                    attachment,
                    str(config.get("timezone") or ""),
                )

                send_chat_action(str(token), chat_id, "typing")
                if message.get("message_id") is not None and access.get("ackReaction"):
                    set_message_reaction(
                        str(token),
                        chat_id,
                        int(message["message_id"]),
                        str(access.get("ackReaction") or ""),
                    )

                active_turn = codex.steer_turn(chat_id, text)
                if active_turn:
                    send_message(
                        str(token),
                        chat_id,
                        "Added your message to the active turn.",
                        message.get("message_id"),
                        access=access,
                    )
                    PENDING_UPDATE_FILE.unlink(missing_ok=True)
                    continue

                with chat_map_lock:
                    thread_id = codex.ensure_thread(chat_id, chat_map)
                    save_chat_map(chat_map)
                    save_runtime_state(
                        {
                            **load_runtime_state(),
                            "active_chat_id": chat_id,
                            "active_thread_id": thread_id,
                            "updated_at": time.time(),
                        }
                    )
                    codex.start_turn(chat_id, thread_id, text)
                if config.get("send_queue_confirmation", False):
                    send_message(
                        str(token),
                        chat_id,
                        "Sent to Codex. Use /status or /stop while it runs.",
                        message.get("message_id"),
                        access=access,
                    )
                PENDING_UPDATE_FILE.unlink(missing_ok=True)
            except Exception as exc:
                print(f"telegram update handling failed: {exc}", file=sys.stderr)
                traceback.print_exc()
                chat_id = str((update.get("message") or update.get("edited_message") or {}).get("chat", {}).get("id", ""))
                if chat_id:
                    try:
                        send_message(str(token), chat_id, f"Bridge error: {exc}")
                    except Exception:
                        pass
                runtime_state = load_runtime_state()
                update_id = str(update.get("update_id") or "unknown")
                previous_id = str(runtime_state.get("pending_update_id") or "")
                attempts = int(runtime_state.get("pending_update_attempts") or 0) + 1 if previous_id == update_id else 1
                runtime_state.update(
                    {
                        "pending_update_id": update_id,
                        "pending_update_attempts": attempts,
                        "pending_update_error": str(exc)[:1000],
                    }
                )
                save_runtime_state(runtime_state)
                # Leave the journal in place for bounded retry. Persistent bad
                # updates are quarantined rather than blocking all later chats.
                if attempts >= 5:
                    FAILED_UPDATES_DIR.mkdir(parents=True, exist_ok=True)
                    failed_path = FAILED_UPDATES_DIR / f"update-{update_id}.json"
                    PENDING_UPDATE_FILE.replace(failed_path)
                    print(f"telegram update {update_id} quarantined after {attempts} attempts", file=sys.stderr)
                else:
                    time.sleep(min(3 * attempts, 15))
                break


if __name__ == "__main__":
    main()
