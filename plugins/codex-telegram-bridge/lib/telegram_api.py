#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import mimetypes
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

TELEGRAM_TEXT_LIMIT = 3900
PHOTO_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def telegram_request(token: str, method: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=data,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def telegram_get_json(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def send_chat_action(token: str, chat_id: str, action: str = "typing") -> None:
    try:
        telegram_request(token, "sendChatAction", {"chat_id": chat_id, "action": action})
    except Exception:
        return


def set_bot_commands(token: str, commands: list[dict[str, str]]) -> dict[str, Any]:
    return telegram_request(
        token,
        "setMyCommands",
        {
            "commands": json.dumps(commands),
        },
    )


def answer_callback_query(token: str, callback_query_id: str, text: str | None = None) -> None:
    payload: dict[str, Any] = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    try:
        telegram_request(token, "answerCallbackQuery", payload)
    except Exception:
        return


def set_message_reaction(token: str, chat_id: str, message_id: int, emoji: str) -> None:
    if not emoji:
        return
    payload = {
        "chat_id": chat_id,
        "message_id": str(message_id),
        "reaction": json.dumps([{"type": "emoji", "emoji": emoji}]),
    }
    try:
        telegram_request(token, "setMessageReaction", payload)
    except Exception:
        return


def edit_message_text(
    token: str,
    chat_id: str,
    message_id: int,
    text: str,
    parse_mode: str | None = None,
    reply_markup: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": str(message_id),
        "text": text,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    return telegram_request(token, "editMessageText", payload)


def split_text(text: str, limit: int = TELEGRAM_TEXT_LIMIT, chunk_mode: str = "newline") -> list[str]:
    value = (text or "").strip()
    if not value:
        return ["(no text)"]

    try:
        limit = max(256, min(int(limit), 4096))
    except Exception:
        limit = TELEGRAM_TEXT_LIMIT

    chunks: list[str] = []
    remainder = value
    while len(remainder) > limit:
        split_at = limit
        if chunk_mode == "newline":
            split_at = remainder.rfind("\n\n", 0, limit)
            if split_at < limit // 2:
                split_at = remainder.rfind("\n", 0, limit)
            if split_at < limit // 2:
                split_at = limit
        chunk = remainder[:split_at].strip()
        chunks.append(chunk)
        remainder = remainder[split_at:].strip()
    if remainder:
        chunks.append(remainder)
    return chunks


def send_message(
    token: str,
    chat_id: str,
    text: str,
    reply_to_message_id: int | None = None,
    access: dict[str, Any] | None = None,
    parse_mode: str | None = None,
    files: list[str] | None = None,
    reply_markup: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    access = access or {}
    files = files or []
    value = (text or "").strip()
    if value:
        chunks = split_text(
            value,
            limit=int(access.get("textChunkLimit", TELEGRAM_TEXT_LIMIT)),
            chunk_mode=str(access.get("chunkMode", "newline")),
        )
    elif files:
        chunks = []
    else:
        chunks = ["(no text)"]
    reply_mode = str(access.get("replyToMode", "first"))
    sent: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks):
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": chunk,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup and index == 0:
            payload["reply_markup"] = json.dumps(reply_markup)
        if reply_to_message_id is not None and (
            reply_mode == "all" or (reply_mode == "first" and index == 0)
        ):
            payload["reply_to_message_id"] = str(reply_to_message_id)
        response = telegram_request(token, "sendMessage", payload)
        if isinstance(response.get("result"), dict):
            sent.append(response["result"])

    for file_path in files:
        should_reply = reply_to_message_id is not None and reply_mode != "off"
        response = send_attachment(
            token,
            chat_id,
            file_path,
            reply_to_message_id=reply_to_message_id if should_reply else None,
        )
        if isinstance(response.get("result"), dict):
            sent.append(response["result"])
    return sent


def send_attachment(
    token: str,
    chat_id: str,
    file_path: str,
    reply_to_message_id: int | None = None,
) -> dict[str, Any]:
    path = Path(file_path).expanduser()
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"Attachment not found: {path}")

    endpoint = "sendPhoto" if path.suffix.lower() in PHOTO_EXTS else "sendDocument"
    field_name = "photo" if endpoint == "sendPhoto" else "document"
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    fields = {"chat_id": chat_id}
    if reply_to_message_id is not None:
        fields["reply_to_message_id"] = str(reply_to_message_id)
    payload, boundary = encode_multipart(fields, field_name, path.read_bytes(), path.name, mime_type)
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{endpoint}",
        data=payload,
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_telegram_file(token: str, file_id: str) -> tuple[bytes, str, str] | None:
    try:
        info = telegram_request(token, "getFile", {"file_id": file_id})
        file_path = (info.get("result") or {}).get("file_path")
        if not file_path:
            return None
        url = f"https://api.telegram.org/file/bot{token}/{file_path}"
        with urllib.request.urlopen(url, timeout=120) as response:
            data = response.read()
        filename = Path(file_path).name
        mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        return data, filename, mime_type
    except Exception:
        return None


def download_attachment_to_dir(
    token: str,
    file_id: str,
    directory: str | Path,
    preferred_name: str | None = None,
) -> str | None:
    fetched = fetch_telegram_file(token, file_id)
    if fetched is None:
        return None
    file_bytes, filename, _ = fetched
    target_dir = Path(directory).expanduser()
    target_dir.mkdir(parents=True, exist_ok=True)

    source_name = preferred_name or filename or "attachment.bin"
    safe_name = sanitize_filename(source_name)
    suffix = Path(safe_name).suffix or Path(filename).suffix or ".bin"
    stem = Path(safe_name).stem or "attachment"
    digest = hashlib.sha256(file_bytes).hexdigest()[:12]
    destination = target_dir / f"{stem}_{digest}{suffix}"
    if not destination.exists():
        destination.write_bytes(file_bytes)
    return str(destination)


def sanitize_filename(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {".", "-", "_"} else "_" for ch in value)
    cleaned = cleaned.strip("._") or "attachment"
    if len(cleaned) > 120:
        suffix = Path(cleaned).suffix
        stem = Path(cleaned).stem[: max(1, 120 - len(suffix))]
        cleaned = stem + suffix
    return cleaned


def encode_multipart(
    fields: dict[str, str],
    file_field: str,
    file_bytes: bytes,
    filename: str,
    mime_type: str,
) -> tuple[bytes, str]:
    boundary = f"----CodexTelegramBridge{hashlib.sha256(file_bytes).hexdigest()[:16]}"
    body = bytearray()

    for name, value in fields.items():
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        body.extend(str(value).encode("utf-8"))
        body.extend(b"\r\n")

    body.extend(f"--{boundary}\r\n".encode("utf-8"))
    body.extend(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{sanitize_filename(filename)}"\r\n'.encode(
            "utf-8"
        )
    )
    body.extend(f"Content-Type: {mime_type}\r\n\r\n".encode("utf-8"))
    body.extend(file_bytes)
    body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode("utf-8"))

    return bytes(body), boundary
