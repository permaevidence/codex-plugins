#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.telegram_api import download_attachment_to_dir, edit_message_text, send_message, set_message_reaction
from lib.telegram_common import (
    INBOX_DIR,
    load_access,
    load_chat_map,
    load_config,
    load_runtime_state,
    save_chat_map,
    save_runtime_state,
)

SERVER_NAME = "telegram-actions"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = "2025-06-18"


def send_json(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def respond(request_id: Any, result: dict[str, Any] | None = None, error: dict[str, Any] | None = None) -> None:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result or {}
    send_json(payload)


def tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": "reply",
            "description": "Send a Telegram message, defaulting to the currently active Telegram chat.",
            "inputSchema": {
                "type": "object",
                "required": ["text"],
                "properties": {
                    "text": {"type": "string", "description": "Message text to send."},
                    "chat_id": {"type": "string", "description": "Optional explicit Telegram chat id."},
                    "reply_to_message_id": {
                        "type": "integer",
                        "description": "Optional explicit Telegram message id to reply to.",
                    },
                    "reply_to": {
                        "type": "integer",
                        "description": "Alias for reply_to_message_id.",
                    },
                    "files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional absolute file paths to attach as photos or documents.",
                    },
                    "format": {
                        "type": "string",
                        "enum": ["text", "markdownv2"],
                        "description": "Optional Telegram formatting mode for the text payload.",
                    },
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "edit_message",
            "description": "Edit a previously sent Telegram message, defaulting to the latest outbound message in the active chat.",
            "inputSchema": {
                "type": "object",
                "required": ["text"],
                "properties": {
                    "text": {"type": "string", "description": "Replacement message text."},
                    "chat_id": {"type": "string", "description": "Optional explicit Telegram chat id."},
                    "message_id": {
                        "type": "integer",
                        "description": "Optional explicit Telegram message id to edit.",
                    },
                    "format": {
                        "type": "string",
                        "enum": ["text", "markdownv2"],
                        "description": "Optional Telegram formatting mode for the edited text.",
                    },
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "react",
            "description": "Add a Telegram emoji reaction, defaulting to the latest inbound message in the active chat.",
            "inputSchema": {
                "type": "object",
                "required": ["emoji"],
                "properties": {
                    "emoji": {"type": "string", "description": "Reaction emoji to apply."},
                    "chat_id": {"type": "string", "description": "Optional explicit Telegram chat id."},
                    "message_id": {
                        "type": "integer",
                        "description": "Optional explicit Telegram message id to react to.",
                    },
                    "target": {
                        "type": "string",
                        "enum": ["inbound", "outbound"],
                        "description": "Which recent message to use when message_id is omitted.",
                    },
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "download_attachment",
            "description": "Download a Telegram attachment by file_id into the local bridge inbox and return the local path.",
            "inputSchema": {
                "type": "object",
                "required": ["file_id"],
                "properties": {
                    "file_id": {"type": "string", "description": "Telegram attachment file_id from an inbound <channel ...> message."},
                    "filename": {"type": "string", "description": "Optional preferred filename."},
                },
                "additionalProperties": False,
            },
        },
    ]


def current_context() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], str]:
    config = load_config()
    access = load_access()
    chat_map = load_chat_map()
    runtime = load_runtime_state()
    authorized = authorized_chat_ids(access)
    active = str(runtime.get("active_chat_id") or config.get("owner_chat_id") or "").strip()
    # A global "last active" chat is safe only in the common single-chat
    # deployment. With multiple authorized chats the caller must bind the tool
    # call explicitly, preventing one concurrent turn from replying to another.
    chat_id = active if len(authorized) == 1 and active in authorized else ""
    return config, access, chat_map, runtime, chat_id


def authorized_chat_ids(access: dict[str, Any]) -> set[str]:
    result = {str(item) for item in access.get("allowFrom", []) if str(item).strip()}
    groups = access.get("groups") or {}
    if isinstance(groups, dict):
        result.update(str(item) for item in groups if str(item).strip())
    return result


def require_authorized_chat(access: dict[str, Any], chat_id: str) -> None:
    if chat_id not in authorized_chat_ids(access):
        raise RuntimeError("Telegram destination is not authorized by access.json.")


def require_token(config: dict[str, Any]) -> str:
    token = str(config.get("bot_token") or "").strip()
    if not token:
        raise RuntimeError("Telegram bot token is not configured.")
    return token


def resolve_chat(chat_id: Any, default_chat_id: str) -> str:
    resolved = str(chat_id or default_chat_id or "").strip()
    if not resolved:
        raise RuntimeError(
            "No unambiguous Telegram destination is available. Pass the chat_id from the active <channel ...> message."
        )
    return resolved


def resolve_entry(chat_map: dict[str, Any], chat_id: str) -> dict[str, Any]:
    return dict(chat_map.get(chat_id, {}))


def handle_reply(arguments: dict[str, Any]) -> dict[str, Any]:
    config, access, chat_map, runtime, default_chat_id = current_context()
    token = require_token(config)
    chat_id = resolve_chat(arguments.get("chat_id"), default_chat_id)
    require_authorized_chat(access, chat_id)
    entry = resolve_entry(chat_map, chat_id)

    reply_to_message_id = arguments.get("reply_to_message_id")
    if not isinstance(reply_to_message_id, int):
        alias_value = arguments.get("reply_to")
        if isinstance(alias_value, int):
            reply_to_message_id = alias_value
    if reply_to_message_id is None:
        candidate = entry.get("last_message_id") or runtime.get("last_inbound_message_id")
        if isinstance(candidate, int):
            reply_to_message_id = candidate

    format_value = str(arguments.get("format") or "text").strip().lower()
    parse_mode = "MarkdownV2" if format_value == "markdownv2" else None
    files = arguments.get("files") or []
    if not isinstance(files, list) or not all(isinstance(item, str) for item in files):
        raise RuntimeError("files must be an array of absolute file paths.")

    sent = send_message(
        token,
        chat_id,
        str(arguments.get("text") or ""),
        reply_to_message_id=reply_to_message_id if isinstance(reply_to_message_id, int) else None,
        access=access,
        parse_mode=parse_mode,
        files=list(files),
    )
    message_ids = [int(item["message_id"]) for item in sent if isinstance(item.get("message_id"), int)]

    chat_map.setdefault(chat_id, {})
    chat_map[chat_id]["last_outbound_message_ids"] = message_ids
    if message_ids:
        chat_map[chat_id]["last_outbound_message_id"] = message_ids[-1]
    save_chat_map(chat_map)

    runtime.update(
        {
            "active_chat_id": chat_id,
            "last_outbound_message_ids": message_ids,
            "last_outbound_message_id": message_ids[-1] if message_ids else None,
            "updated_at": time.time(),
        }
    )
    save_runtime_state(runtime)

    payload = {
        "chat_id": chat_id,
        "message_ids": message_ids,
        "reply_to_message_id": reply_to_message_id,
        "files": list(files),
    }
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "structuredContent": payload,
    }


def handle_edit_message(arguments: dict[str, Any]) -> dict[str, Any]:
    config, access, chat_map, runtime, default_chat_id = current_context()
    token = require_token(config)
    chat_id = resolve_chat(arguments.get("chat_id"), default_chat_id)
    require_authorized_chat(access, chat_id)
    entry = resolve_entry(chat_map, chat_id)

    message_id = arguments.get("message_id")
    if not isinstance(message_id, int):
        candidate = entry.get("last_outbound_message_id") or runtime.get("last_outbound_message_id")
        if isinstance(candidate, int):
            message_id = candidate
    if not isinstance(message_id, int):
        raise RuntimeError("No outbound Telegram message is available to edit. Call reply first or pass message_id.")

    format_value = str(arguments.get("format") or "text").strip().lower()
    parse_mode = "MarkdownV2" if format_value == "markdownv2" else None
    edit_message_text(token, chat_id, message_id, str(arguments.get("text") or ""), parse_mode=parse_mode)

    runtime.update(
        {
            "active_chat_id": chat_id,
            "last_outbound_message_id": message_id,
            "updated_at": time.time(),
        }
    )
    save_runtime_state(runtime)

    payload = {"chat_id": chat_id, "message_id": message_id}
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "structuredContent": payload,
    }


def handle_react(arguments: dict[str, Any]) -> dict[str, Any]:
    config, access, chat_map, runtime, default_chat_id = current_context()
    token = require_token(config)
    chat_id = resolve_chat(arguments.get("chat_id"), default_chat_id)
    require_authorized_chat(access, chat_id)
    entry = resolve_entry(chat_map, chat_id)

    message_id = arguments.get("message_id")
    target = str(arguments.get("target") or "inbound")
    if not isinstance(message_id, int):
        if target == "outbound":
            candidate = entry.get("last_outbound_message_id") or runtime.get("last_outbound_message_id")
        else:
            candidate = entry.get("last_message_id") or runtime.get("last_inbound_message_id")
        if isinstance(candidate, int):
            message_id = candidate
    if not isinstance(message_id, int):
        raise RuntimeError("No Telegram message is available to react to. Pass message_id explicitly.")

    emoji = str(arguments.get("emoji") or "").strip()
    if not emoji:
        raise RuntimeError("emoji is required.")

    set_message_reaction(token, chat_id, message_id, emoji)
    payload = {"chat_id": chat_id, "message_id": message_id, "emoji": emoji, "target": target}
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "structuredContent": payload,
    }


def handle_download_attachment(arguments: dict[str, Any]) -> dict[str, Any]:
    config, _, _, _, _ = current_context()
    token = require_token(config)
    file_id = str(arguments.get("file_id") or "").strip()
    if not file_id:
        raise RuntimeError("file_id is required.")
    filename = arguments.get("filename")
    if filename is not None and not isinstance(filename, str):
        raise RuntimeError("filename must be a string when provided.")
    path = download_attachment_to_dir(token, file_id, INBOX_DIR, preferred_name=filename)
    if not path:
        raise RuntimeError("Could not download the Telegram attachment.")
    payload = {"file_id": file_id, "path": path}
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "structuredContent": payload,
    }


def handle_tool_call(params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        raise RuntimeError("Tool arguments must be an object.")
    if name == "reply":
        return handle_reply(arguments)
    if name == "edit_message":
        return handle_edit_message(arguments)
    if name == "react":
        return handle_react(arguments)
    if name == "download_attachment":
        return handle_download_attachment(arguments)
    raise RuntimeError(f"Unknown tool: {name}")


def main() -> None:
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue

        request_id = message.get("id")
        method = message.get("method")

        try:
            if method == "initialize":
                params = message.get("params") or {}
                protocol = str(params.get("protocolVersion") or PROTOCOL_VERSION)
                respond(
                    request_id,
                    {
                        "protocolVersion": protocol,
                        "capabilities": {
                            "tools": {"listChanged": False},
                        },
                        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    },
                )
            elif method == "notifications/initialized":
                continue
            elif method == "ping":
                respond(request_id, {})
            elif method == "tools/list":
                respond(request_id, {"tools": tool_definitions()})
            elif method == "resources/list":
                respond(request_id, {"resources": []})
            elif method == "prompts/list":
                respond(request_id, {"prompts": []})
            elif method == "tools/call":
                params = message.get("params")
                if not isinstance(params, dict):
                    raise RuntimeError('invalid request: missing required "params"')
                respond(request_id, handle_tool_call(params))
            else:
                if request_id is not None:
                    respond(request_id, error={"code": -32601, "message": f"Method not found: {method}"})
        except Exception as exc:
            if request_id is not None:
                respond(
                    request_id,
                    error={"code": -32000, "message": str(exc)},
                )


if __name__ == "__main__":
    main()
