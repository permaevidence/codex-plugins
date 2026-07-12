#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.common import (
    FILES_DIR,
    append_history_entries,
    describe_file_entry,
    empty_success,
    first_present,
    load_config,
    load_hook_input,
    load_recent_history_context,
)

TEXT_EXTENSIONS = {
    ".c",
    ".cc",
    ".cpp",
    ".css",
    ".go",
    ".h",
    ".html",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".sql",
    ".swift",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}

CHANNEL_REPLY_RE = re.compile(r"telegram.*reply|whatsapp.*reply|slack.*reply|discord.*reply|^reply$", re.IGNORECASE)
CHANNEL_PATH_RE = re.compile(r'<channel\b[^>]*\b(?:image_path|file_path)=["\']([^"\']+)["\']')


def main() -> None:
    payload = load_hook_input()
    config = load_config()

    assistant_text = extract_assistant_text(payload)
    transcript_path = first_present(payload, "transcript_path", "transcriptPath")

    file_entries: list[dict[str, Any]] = []
    if config.get("enable_attachment_capture", True) and isinstance(transcript_path, str) and os.path.exists(transcript_path):
        turn_start = find_last_turn_start(transcript_path)
        channel_reply = extract_channel_reply_text(transcript_path, turn_start)
        if channel_reply:
            assistant_text = channel_reply
        if not assistant_text:
            assistant_text = extract_last_assistant_message(transcript_path, turn_start)
        file_entries = extract_files_from_turn(transcript_path, turn_start, payload, config)

    entries: list[dict[str, Any]] = []
    for entry in file_entries:
        if entry.get("from") == "user":
            entries.append(entry)
    if assistant_text:
        entries.append(
            {
                "role": "assistant",
                "content": assistant_text,
            }
        )
    for entry in file_entries:
        if entry.get("from") == "assistant":
            entries.append(entry)

    if entries:
        append_history_entries(entries, payload)
    empty_success()


def extract_assistant_text(payload: dict[str, Any]) -> str:
    message = first_present(
        payload,
        "last_assistant_message",
        "lastAssistantMessage",
        "assistant_message",
        "assistantMessage",
    )
    if isinstance(message, str):
        return message.strip()
    return ""


def find_last_turn_start(transcript_path: str) -> int:
    last_user_idx = 0
    try:
        with open(transcript_path, "r", encoding="utf-8") as handle:
            for index, line in enumerate(handle):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if is_user_message(obj):
                    last_user_idx = index
    except Exception:
        return 0
    return last_user_idx


def is_user_message(obj: dict[str, Any]) -> bool:
    item = rollout_item(obj)
    if item.get("type") == "user" or item.get("role") == "user":
        content = ((item.get("message") or {}).get("content")) if isinstance(item.get("message"), dict) else item.get("content")
        if isinstance(content, list) and any(isinstance(block, dict) and block.get("type") == "tool_result" for block in content):
            return False
        return True
    return False


def extract_last_assistant_message(transcript_path: str, turn_start: int = 0) -> str:
    parts: list[str] = []
    try:
        with open(transcript_path, "r", encoding="utf-8") as handle:
            for index, line in enumerate(handle):
                if index < turn_start:
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not is_assistant_message(obj):
                    continue
                for block in content_blocks(obj):
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") in {"text", "output_text"}:
                        text = str(block.get("text") or "").strip()
                        if text:
                            parts.append(text)
                item = rollout_item(obj)
                if isinstance(item.get("content"), str):
                    text = str(item.get("content") or "").strip()
                    if text:
                        parts.append(text)
    except Exception:
        return ""
    return "\n\n".join(parts).strip()


def extract_channel_reply_text(transcript_path: str, turn_start: int = 0) -> str:
    texts: list[str] = []
    try:
        with open(transcript_path, "r", encoding="utf-8") as handle:
            for index, line in enumerate(handle):
                if index < turn_start:
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                item = rollout_item(obj)
                if item.get("type") not in {"tool_use", "tool_call", "function_call", "custom_tool_call"}:
                    continue
                tool_name = str(item.get("name") or item.get("tool_name") or "")
                if not CHANNEL_REPLY_RE.search(tool_name):
                    continue
                tool_input = parse_tool_input(item.get("input") or item.get("arguments") or {})
                text = tool_input.get("text")
                if isinstance(text, str) and text.strip():
                    texts.append(text.strip())
    except Exception:
        return ""
    return "\n\n".join(texts).strip()


def extract_files_from_turn(
    transcript_path: str,
    turn_start: int,
    payload: dict[str, Any],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    seen: set[str] = set()
    timestamp = datetime.now(timezone.utc).isoformat()
    chat_context = load_recent_history_context()

    with open(transcript_path, "r", encoding="utf-8") as handle:
        for index, line in enumerate(handle):
            if index < turn_start:
                continue
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            item = rollout_item(obj)
            source_role = "user" if is_user_message(obj) else "assistant" if is_assistant_message(obj) else ""

            for block in content_blocks(obj):
                if not isinstance(block, dict):
                    continue
                block_type = str(block.get("type") or "")

                if block_type in {"image", "document", "file"}:
                    entry = extract_inline_attachment(block, source_role, timestamp, config, chat_context)
                    if entry and dedupe_key(entry) not in seen:
                        seen.add(dedupe_key(entry))
                        files.append(entry)

                if block_type in {"text", "input_text"} and source_role == "user":
                    for path in extract_channel_file_paths(str(block.get("text") or "")):
                        entry = create_file_entry_from_path(
                            path,
                            source_role,
                            timestamp,
                            config=config,
                            chat_context=chat_context,
                            preserve_original=False,
                        )
                        if entry and dedupe_key(entry) not in seen:
                            seen.add(dedupe_key(entry))
                            files.append(entry)

                if block_type in {"tool_use", "tool_call"} and source_role == "assistant":
                    tool_name = str(block.get("name") or block.get("tool_name") or "")
                    tool_input = block.get("input") or block.get("arguments") or {}
                    for path in extract_paths_from_tool(tool_name, tool_input):
                        entry = create_file_entry_from_path(
                            path,
                            "assistant",
                            timestamp,
                            config=config,
                            chat_context=chat_context,
                        )
                        if entry and dedupe_key(entry) not in seen:
                            seen.add(dedupe_key(entry))
                            files.append(entry)

            if item.get("type") in {"function_call", "custom_tool_call"}:
                tool_name = str(item.get("name") or "")
                tool_input = parse_tool_input(item.get("input") or item.get("arguments") or {})
                for path in extract_paths_from_tool(tool_name, tool_input):
                    entry = create_file_entry_from_path(
                        path,
                        "assistant",
                        timestamp,
                        config=config,
                        chat_context=chat_context,
                    )
                    if entry and dedupe_key(entry) not in seen:
                        seen.add(dedupe_key(entry))
                        files.append(entry)
    return files


def content_blocks(obj: dict[str, Any]) -> list[Any]:
    item = rollout_item(obj)
    message = item.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, list):
            return content
    content = item.get("content")
    if isinstance(content, list):
        return content
    return []


def is_assistant_message(obj: dict[str, Any]) -> bool:
    item = rollout_item(obj)
    return item.get("type") == "assistant" or item.get("role") == "assistant"


def rollout_item(obj: dict[str, Any]) -> dict[str, Any]:
    if obj.get("type") == "response_item" and isinstance(obj.get("payload"), dict):
        return obj["payload"]
    return obj


def parse_tool_input(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def extract_inline_attachment(
    block: dict[str, Any],
    source_role: str,
    timestamp: str,
    config: dict[str, Any],
    chat_context: str,
) -> dict[str, Any] | None:
    source = block.get("source") or {}
    if not isinstance(source, dict):
        return None

    media_type = str(source.get("media_type") or source.get("mime_type") or "application/octet-stream")
    if source.get("type") == "base64" and source.get("data"):
        data_b64 = str(source.get("data") or "")
        path = save_base64_attachment(data_b64, media_type)
        if not path:
            return None
        return build_file_entry(
            path,
            source_role,
            timestamp,
            config=config,
            chat_context=chat_context,
            media_type=media_type,
            include_description=False,
        )

    for key in ("path", "file_path", "pathOrUrl"):
        value = source.get(key)
        if isinstance(value, str) and value:
            return create_file_entry_from_path(
                value,
                source_role,
                timestamp,
                config=config,
                chat_context=chat_context,
            )
    return None


def extract_paths_from_tool(tool_name: str, tool_input: Any) -> list[str]:
    if not isinstance(tool_input, dict):
        return []
    # Only capture files the assistant explicitly sent through a channel reply.
    # Never copy arbitrary paths from read/exec/view tools: those may be secrets
    # and must not be persisted or uploaded merely because Codex inspected them.
    if not CHANNEL_REPLY_RE.search(tool_name):
        return []
    paths: list[str] = []
    file_values = tool_input.get("files")
    if isinstance(file_values, list):
        for item in file_values:
            if isinstance(item, str) and looks_like_local_path(item):
                paths.append(item)
    return dedupe_paths(paths)


def dedupe_paths(paths: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for path in paths:
        norm = str(Path(path).expanduser())
        if norm in seen:
            continue
        seen.add(norm)
        output.append(norm)
    return output


def create_file_entry_from_path(
    file_path: str,
    source_role: str,
    timestamp: str,
    config: dict[str, Any],
    chat_context: str,
    preserve_original: bool = False,
) -> dict[str, Any] | None:
    path = Path(file_path).expanduser()
    if not path.exists() or not path.is_file():
        return None

    entry = build_file_entry(
        str(path),
        source_role,
        timestamp,
        config=config,
        chat_context=chat_context,
        original_path=str(path),
        include_description=False,
    )
    if not preserve_original:
        entry["archive_copy_pending"] = True
    return entry


def build_file_entry(
    file_path: str,
    source_role: str,
    timestamp: str,
    config: dict[str, Any],
    chat_context: str,
    media_type: str | None = None,
    original_path: str | None = None,
    include_description: bool = False,
) -> dict[str, Any]:
    path = Path(file_path)
    media_type = media_type or guess_media_type(str(path))
    size_bytes = path.stat().st_size if path.exists() else 0
    entry = {
        "role": "file",
        "from": source_role,
        "path": str(path),
        "media_type": media_type,
        "size_bytes": size_bytes,
        "timestamp": timestamp,
    }
    if original_path:
        entry["original_path"] = original_path
    if include_description:
        entry["description"] = describe_file_entry(path, media_type, config=config, chat_context=chat_context)
    else:
        entry["description_pending"] = True
    return entry


def dedupe_key(entry: dict[str, Any]) -> str:
    path = str(entry.get("path") or "")
    origin = str(entry.get("original_path") or "")
    return f"{entry.get('from')}::{origin or path}"


def save_base64_attachment(data_b64: str, media_type: str) -> str | None:
    try:
        raw = base64.b64decode(data_b64)
    except Exception:
        return None
    ext = media_extension(media_type)
    digest = hashlib.sha256(raw).hexdigest()[:12]
    filename = f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{digest}{ext}"
    destination = FILES_DIR / filename
    if not destination.exists():
        destination.write_bytes(raw)
    return str(destination)


def copy_file_to_store(path: Path) -> str:
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()[:12]
    ext = path.suffix or media_extension(guess_media_type(str(path)))
    filename = f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{digest}{ext}"
    destination = FILES_DIR / filename
    if not destination.exists():
        shutil.copy2(path, destination)
    return str(destination)


def extract_channel_file_paths(text: str) -> list[str]:
    return [match.group(1) for match in CHANNEL_PATH_RE.finditer(text)]


def extract_channel_image_paths(text: str) -> list[str]:
    """Backward-compatible alias for older tests/callers."""
    return extract_channel_file_paths(text)


def looks_like_local_path(value: str) -> bool:
    return value.startswith("/") or value.startswith("~")


def media_extension(media_type: str) -> str:
    mapping = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "application/pdf": ".pdf",
        "text/plain": ".txt",
        "text/markdown": ".md",
        "application/json": ".json",
    }
    return mapping.get(media_type, ".bin")


def guess_media_type(file_path: str) -> str:
    suffix = Path(file_path).suffix.lower()
    mapping = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".pdf": "application/pdf",
        ".md": "text/markdown",
        ".txt": "text/plain",
        ".json": "application/json",
        ".py": "text/x-python",
        ".js": "text/javascript",
        ".ts": "text/typescript",
        ".tsx": "text/typescript",
        ".jsx": "text/javascript",
        ".html": "text/html",
        ".css": "text/css",
        ".yaml": "text/yaml",
        ".yml": "text/yaml",
        ".sh": "text/x-shellscript",
    }
    return mapping.get(suffix, "application/octet-stream")

if __name__ == "__main__":
    main()
