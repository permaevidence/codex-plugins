#!/usr/bin/env python3
import json
import urllib.parse
import urllib.request
from typing import Any

TELEGRAM_TEXT_LIMIT = 3900


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


def edit_message_text(token: str, chat_id: str, message_id: int, text: str) -> dict[str, Any]:
    return telegram_request(
        token,
        "editMessageText",
        {
            "chat_id": chat_id,
            "message_id": str(message_id),
            "text": text,
        },
    )


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
) -> list[dict[str, Any]]:
    access = access or {}
    chunks = split_text(
        text,
        limit=int(access.get("textChunkLimit", TELEGRAM_TEXT_LIMIT)),
        chunk_mode=str(access.get("chunkMode", "newline")),
    )
    reply_mode = str(access.get("replyToMode", "first"))
    sent: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks):
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": chunk,
        }
        if reply_to_message_id is not None and (
            reply_mode == "all" or (reply_mode == "first" and index == 0)
        ):
            payload["reply_to_message_id"] = str(reply_to_message_id)
        response = telegram_request(token, "sendMessage", payload)
        if isinstance(response.get("result"), dict):
            sent.append(response["result"])
    return sent
