#!/usr/bin/env python3
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

STATE_DIR = Path.home() / ".codex" / "long-term-memory"
CONFIG_FILE = STATE_DIR / "config.json"
HISTORY_FILE = STATE_DIR / "history.jsonl"
FACTS_FILE = STATE_DIR / "user_facts.jsonl"
ARCHIVES_DIR = STATE_DIR / "archives"
BACKUPS_DIR = STATE_DIR / "backups"
FILES_DIR = STATE_DIR / "files"
PENDING_DIR = STATE_DIR / "pending"
COMPACTION_STATE_FILE = STATE_DIR / "compaction_scan_state.json"
SESSIONS_DIR = Path.home() / ".codex" / "sessions"

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
MODEL_FILE_MAX_BYTES = 8 * 1024 * 1024
MODEL_CONTEXT_MAX_CHARS = 80000
MODEL_INPUT_MAX_CHARS = 120000
SUPPORTED_VISION_MEDIA_TYPES = {
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/gif",
    "image/webp",
}
COMPACTION_SCAN_OVERLAP_BYTES = 4096

DEFAULT_CONFIG = {
    "max_injection_chars": 300000,
    "include_timestamps": True,
    "enable_user_facts": True,
    "enable_calendar": True,
    "enable_attachment_capture": True,
    "compact_threshold_chars": 80000,
    "archive_chunk_chars": 40000,
    "temp_summaries_per_consolidation": 4,
    "max_visible_consolidated": 5,
    "meta_permanent_threshold": 5,
    "enable_model_summaries": True,
    "enable_model_file_descriptions": True,
    "enable_model_user_facts": True,
    "user_facts_max_chars": 16000,
    "model_file_max_bytes": MODEL_FILE_MAX_BYTES,
    "openai_api_key": "",
    "openai_api_key_env": "OPENAI_API_KEY",
    "openai_base_url": OPENAI_RESPONSES_URL,
    "openai_model": "gpt-5.4",
    "openai_reasoning_effort": "high",
    "openai_timeout_seconds": 45,
    "pending_retry_enabled": True,
    "pending_retry_base_seconds": 30,
    "pending_retry_max_seconds": 480,
}


def ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVES_DIR.mkdir(parents=True, exist_ok=True)
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    PENDING_DIR.mkdir(parents=True, exist_ok=True)


def data_items() -> list[tuple[Path, str]]:
    return [
        (HISTORY_FILE, "file"),
        (FACTS_FILE, "file"),
        (ARCHIVES_DIR, "dir"),
        (FILES_DIR, "dir"),
        (PENDING_DIR, "dir"),
    ]


def load_hook_input() -> dict[str, Any]:
    try:
        return json.load(sys.stdin)
    except Exception:
        return {}


def load_config() -> dict[str, Any]:
    ensure_state_dir()
    if not CONFIG_FILE.exists():
        save_json(CONFIG_FILE, DEFAULT_CONFIG)
        return dict(DEFAULT_CONFIG)
    try:
        with CONFIG_FILE.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        merged = dict(DEFAULT_CONFIG)
        merged.update(data)
        return merged
    except Exception:
        return dict(DEFAULT_CONFIG)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")


def load_compaction_state_locked(handle: Any) -> dict[str, Any]:
    handle.seek(0)
    raw = handle.read().strip()
    if not raw:
        return {"threads": {}}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"threads": {}}
    if not isinstance(data, dict):
        return {"threads": {}}
    threads = data.get("threads")
    if not isinstance(threads, dict):
        data["threads"] = {}
    return data


def save_compaction_state_locked(handle: Any, data: dict[str, Any]) -> None:
    handle.seek(0)
    json.dump(data, handle, indent=2)
    handle.write("\n")
    handle.truncate()


def current_thread_id(payload: dict[str, Any]) -> str:
    value = first_present(payload, "thread_id", "threadId", "session_id", "sessionId")
    if value in (None, ""):
        return ""
    return str(value).strip()


def compaction_state_has_thread(thread_id: str) -> bool:
    if not thread_id or not COMPACTION_STATE_FILE.exists():
        return False
    try:
        with COMPACTION_STATE_FILE.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return False
    threads = data.get("threads")
    return isinstance(threads, dict) and thread_id in threads


def current_compaction_thread_id(payload: dict[str, Any]) -> str:
    value = first_present(payload, "thread_id", "threadId")
    if value not in (None, ""):
        return str(value).strip()

    fallback = first_present(payload, "session_id", "sessionId")
    if fallback in (None, ""):
        return ""

    fallback_thread_id = str(fallback).strip()
    if rollout_paths_for_thread(fallback_thread_id) or compaction_state_has_thread(fallback_thread_id):
        return fallback_thread_id
    return ""


def rollout_paths_for_thread(thread_id: str) -> list[Path]:
    if not thread_id or not SESSIONS_DIR.exists():
        return []
    pattern = f"rollout-*-{thread_id}.jsonl"
    return sorted(SESSIONS_DIR.rglob(pattern), key=lambda path: path.as_posix())


def is_context_compacted_event(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    if obj.get("type") != "event_msg":
        return False
    payload = obj.get("payload")
    return isinstance(payload, dict) and payload.get("type") == "context_compacted"


def scan_rollout_log_for_compaction(
    path: Path,
    start_offset: int = 0,
    last_compaction_offset: int = -1,
) -> tuple[int, bool, int]:
    saw_compaction = False
    latest_compaction_offset = last_compaction_offset
    try:
        with path.open("rb") as handle:
            scan_offset = max(start_offset - COMPACTION_SCAN_OVERLAP_BYTES, 0)
            handle.seek(scan_offset)
            if scan_offset > 0:
                handle.readline()
            while True:
                line_offset = handle.tell()
                line = handle.readline()
                if not line:
                    break
                try:
                    obj = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    if not line.endswith(b"\n"):
                        return line_offset, saw_compaction, latest_compaction_offset
                    continue
                if is_context_compacted_event(obj) and line_offset > latest_compaction_offset:
                    saw_compaction = True
                    latest_compaction_offset = line_offset
            return handle.tell(), saw_compaction, latest_compaction_offset
    except OSError:
        return start_offset, False, last_compaction_offset


def update_compaction_reinjection_state(thread_id: str, consume: bool = False) -> bool:
    if not thread_id:
        return False

    ensure_state_dir()
    COMPACTION_STATE_FILE.touch(exist_ok=True)
    with COMPACTION_STATE_FILE.open("r+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        state = load_compaction_state_locked(handle)
        threads = state.setdefault("threads", {})
        thread_state = threads.get(thread_id)
        if not isinstance(thread_state, dict):
            thread_state = {}
            threads[thread_id] = thread_state

        file_states = thread_state.get("files")
        if not isinstance(file_states, dict):
            file_states = {}
            thread_state["files"] = file_states

        active_paths: set[str] = set()
        for path in rollout_paths_for_thread(thread_id):
            path_key = str(path)
            active_paths.add(path_key)
            try:
                stat_result = path.stat()
            except OSError:
                continue

            file_state = file_states.get(path_key)
            if not isinstance(file_state, dict):
                file_state = {}

            try:
                offset = max(int(file_state.get("offset", 0)), 0)
            except (TypeError, ValueError):
                offset = 0
            try:
                last_compaction_offset = int(file_state.get("last_compaction_offset", -1))
            except (TypeError, ValueError):
                last_compaction_offset = -1

            if file_state.get("inode") != stat_result.st_ino or offset > stat_result.st_size:
                offset = 0
                last_compaction_offset = -1

            new_offset, saw_compaction, latest_compaction_offset = scan_rollout_log_for_compaction(
                path,
                offset,
                last_compaction_offset,
            )
            file_states[path_key] = {
                "inode": stat_result.st_ino,
                "offset": new_offset,
                "last_compaction_offset": latest_compaction_offset,
            }
            if saw_compaction:
                thread_state["pending_compaction_reinjection"] = True
                thread_state["last_compaction_detected_at"] = datetime.now(timezone.utc).isoformat()
                thread_state["last_compaction_log"] = path_key

        for stale_path in [path for path in file_states if path not in active_paths]:
            del file_states[stale_path]

        pending = bool(thread_state.get("pending_compaction_reinjection"))
        if consume and pending:
            thread_state["pending_compaction_reinjection"] = False

        save_compaction_state_locked(handle, state)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return pending


def should_reinject_after_compaction(payload: dict[str, Any]) -> bool:
    return update_compaction_reinjection_state(current_compaction_thread_id(payload), consume=False)


def consume_compaction_reinjection(payload: dict[str, Any]) -> bool:
    return update_compaction_reinjection_state(current_compaction_thread_id(payload), consume=True)


def append_history_entry(role: str, content: str, payload: dict[str, Any]) -> None:
    text = (content or "").strip()
    if not text:
        return

    entry = {
        "role": role,
        "content": text,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "thread_id": first_present(payload, "thread_id", "threadId", "session_id", "sessionId"),
        "turn_id": first_present(payload, "turn_id", "turnId"),
    }
    append_history_entries([entry], payload)


def append_history_entries(entries: list[dict[str, Any]], payload: dict[str, Any] | None = None) -> None:
    if not entries:
        return

    ensure_state_dir()
    config = load_config()
    resume_pending_tasks(config)

    normalized: list[dict[str, Any]] = []
    payload = payload or {}
    thread_id = first_present(payload, "thread_id", "threadId", "session_id", "sessionId")
    turn_id = first_present(payload, "turn_id", "turnId")

    for entry in entries:
        role = str(entry.get("role") or "").strip().lower()
        if not role:
            continue
        item = dict(entry)
        item["role"] = role
        item.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        if thread_id not in (None, ""):
            item.setdefault("thread_id", thread_id)
        if turn_id not in (None, ""):
            item.setdefault("turn_id", turn_id)

        if role == "file":
            if not str(item.get("path") or "").strip():
                continue
        else:
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            item["content"] = content

        normalized.append(item)

    if not normalized:
        return

    with HISTORY_FILE.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        for entry in normalized:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    if config.get("enable_user_facts"):
        fallback_facts: list[str] = []
        for entry in normalized:
            if entry.get("role") == "user":
                fallback_facts.extend(extract_user_facts(str(entry.get("content", ""))))
        append_user_facts(fallback_facts, config)

    maybe_compact_history(config)


def read_history() -> list[dict[str, Any]]:
    if not HISTORY_FILE.exists():
        return []

    entries: list[dict[str, Any]] = []
    with HISTORY_FILE.open("r", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return entries


def rewrite_history(entries: list[dict[str, Any]]) -> None:
    ensure_state_dir()
    temp_path = HISTORY_FILE.with_suffix(".jsonl.tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    temp_path.replace(HISTORY_FILE)


def read_facts() -> list[dict[str, Any]]:
    if not FACTS_FILE.exists():
        return []
    facts: list[dict[str, Any]] = []
    with FACTS_FILE.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                facts.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return facts


def append_user_facts(facts: list[str], config: dict[str, Any] | None = None) -> None:
    if not facts:
        return

    config = config or load_config()
    existing = read_facts()
    existing_ids = {fact.get("id") for fact in existing}
    now = datetime.now(timezone.utc).isoformat()

    with FACTS_FILE.open("a", encoding="utf-8") as handle:
        for fact_text in facts:
            normalized_fact = normalize_fact(fact_text)
            if not normalized_fact:
                continue
            fact_identifier = fact_id(normalized_fact)
            if fact_identifier in existing_ids:
                continue
            payload = {
                "id": fact_identifier,
                "fact": normalized_fact,
                "added": now,
            }
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            existing_ids.add(fact_identifier)

    condense_user_facts_if_needed(config)


def fact_id(fact_text: str) -> str:
    return hashlib.sha256(fact_text.strip().lower().encode("utf-8")).hexdigest()[:10]


def extract_user_facts(text: str) -> list[str]:
    cleaned = " ".join(text.strip().split())
    if not cleaned:
        return []

    facts: list[str] = []
    patterns = [
        r"\bI live in ([^.?!]+)",
        r"\bI am based in ([^.?!]+)",
        r"\bmy timezone is ([^.?!]+)",
        r"\bI work at ([^.?!]+)",
        r"\bmy company is ([^.?!]+)",
        r"\bmy GitHub is ([A-Za-z0-9_.-]+)",
        r"\bmy email is ([^\s,;]+@[^\s,;]+)",
        r"\bI prefer ([^.?!]+)",
        r"\bI like ([^.?!]+)",
        r"\bI love ([^.?!]+)",
        r"\bI hate ([^.?!]+)",
        r"\bmy wife is ([^.?!]+)",
        r"\bmy husband is ([^.?!]+)",
        r"\bmy partner is ([^.?!]+)",
        r"\bmy name is ([^.?!]+)",
    ]

    for pattern in patterns:
        for match in re.finditer(pattern, cleaned, flags=re.IGNORECASE):
            clause = match.group(0).strip().rstrip(".")
            facts.append(normalize_fact(clause))

    deduped: list[str] = []
    seen: set[str] = set()
    for fact in facts:
        key = fact.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(fact)
    return deduped[:12]


def normalize_fact(text: str) -> str:
    normalized = " ".join(text.strip().split())
    if not normalized:
        return ""
    if normalized[:1].islower():
        normalized = normalized[:1].upper() + normalized[1:]
    return normalized.rstrip(".")


def build_injected_context(entries: list[dict[str, Any]], config: dict[str, Any]) -> str:
    parts: list[str] = []

    if config.get("enable_user_facts"):
        facts_text = format_facts(read_facts())
        if facts_text:
            parts.append(facts_text)
            parts.append("")

    if config.get("enable_calendar"):
        calendar_text = fetch_calendar_section()
        if calendar_text:
            parts.append(calendar_text)
            parts.append("")

    history_text = format_entries(entries, config)
    if history_text:
        parts.append(history_text)

    return "\n".join(parts).strip()


def format_facts(facts: list[dict[str, Any]]) -> str:
    if not facts:
        return ""

    lines = [
        "=== USER CONTEXT (permanent personal facts) ===",
        "[Skip this section during compaction — it is re-injected automatically after compaction.]",
        "",
    ]
    for fact in facts:
        fact_text = fact.get("fact", "").strip()
        if fact_text:
            lines.append(f"- {fact_text}")
    return "\n".join(lines).strip()


def format_entries(entries: list[dict[str, Any]], config: dict[str, Any]) -> str:
    if not entries:
        return ""

    max_chars = int(config.get("max_injection_chars", DEFAULT_CONFIG["max_injection_chars"]))
    include_timestamps = bool(config.get("include_timestamps", True))

    selected: list[dict[str, Any]] = []
    total_chars = 0

    for entry in reversed(entries):
        rendered = render_entry(entry, include_timestamps)
        if not rendered:
            continue
        entry_chars = len(rendered) + 2
        if selected and total_chars + entry_chars > max_chars:
            break
        selected.append(entry)
        total_chars += entry_chars

    selected.reverse()

    lines = [
        "=== CHAT HISTORY (all sessions) ===",
        "[Skip this section during compaction — it is re-injected automatically after compaction.]",
        "Older segments of this ongoing conversation are compressed into summaries. When a summary header looks relevant, read the referenced archive file before relying on the summary alone.",
        "",
    ]

    current_date = None
    current_thread = None
    for group in group_entries(selected):
        message = group["message"]
        files = group.get("files", [])
        timestamp = message.get("timestamp", "")
        date_str = format_date(timestamp)
        if date_str and date_str != current_date:
            if current_date is not None:
                lines.append("")
            lines.append(f"--- {date_str} ---")
            lines.append("")
            current_date = date_str

        thread_id = message.get("thread_id") or message.get("session_id")
        if thread_id and thread_id != current_thread and message.get("role") != "summary":
            current_thread = thread_id
            lines.append(f"[thread: {str(thread_id)[:8]}]")

        rendered = render_entry(message, include_timestamps)
        if rendered:
            lines.append(rendered)
            for file_entry in files:
                rendered_file = render_file_entry(file_entry)
                if rendered_file:
                    lines.append(rendered_file)
            lines.append("")

    return "\n".join(lines).strip()


def render_entry(entry: dict[str, Any], include_timestamps: bool) -> str:
    role = (entry.get("role") or "unknown").lower()

    if role == "summary":
        archive_file = entry.get("archive_file", "")
        archive_suffix = _archive_suffix(archive_file, entry.get("source_archives", []))
        summary_type = str(entry.get("summary_type") or "temporary")
        if summary_type == "meta_permanent":
            header = f"[META SUMMARY covering {entry.get('covers_from', '?')} to {entry.get('covers_to', '?')}{archive_suffix}]"
        elif summary_type == "meta_temporary":
            header = f"[META SUMMARY (in progress) covering {entry.get('covers_from', '?')} to {entry.get('covers_to', '?')}{archive_suffix}]"
        elif summary_type == "consolidated":
            header = f"[CONSOLIDATED SUMMARY covering {entry.get('covers_from', '?')} to {entry.get('covers_to', '?')}{archive_suffix}]"
        else:
            header = f"[SUMMARY of {entry.get('covers_from', '?')} to {entry.get('covers_to', '?')}{archive_suffix}]"
        return f"{header}\n{str(entry.get('content', '')).strip()}".strip()

    if role == "file":
        return render_file_entry(entry)

    content = (entry.get("content") or "").strip()
    if not content:
        return ""

    prefix = ""
    if include_timestamps:
        timestamp = entry.get("timestamp")
        if timestamp:
            prefix = f"[{format_timestamp(timestamp)}] "

    return f"{prefix}{role.upper()}: {content}"


def render_file_entry(entry: dict[str, Any]) -> str:
    path = str(entry.get("path") or "").strip()
    if not path:
        return ""
    desc = str(entry.get("description") or "").strip()
    media_type = str(entry.get("media_type") or "").strip()
    size_bytes = entry.get("size_bytes")
    details: list[str] = []
    if media_type:
        details.append(media_type)
    if isinstance(size_bytes, int) and size_bytes > 0:
        details.append(size_str_bytes(size_bytes))
    suffix = f" ({', '.join(details)})" if details else ""
    if desc:
        return f"  [file from {entry.get('from', '?')}] {path}{suffix} — {desc}"
    return f"  [file from {entry.get('from', '?')}] {path}{suffix}"


def group_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    current_group: dict[str, Any] | None = None

    for entry in entries:
        role = str(entry.get("role") or "").lower()
        if role == "file":
            if current_group is None:
                groups.append({"message": entry, "files": []})
            else:
                current_group.setdefault("files", []).append(entry)
            continue

        if current_group is not None:
            groups.append(current_group)
        current_group = {"message": entry, "files": []}

    if current_group is not None:
        groups.append(current_group)
    return groups


def maybe_compact_history(config: dict[str, Any] | None = None) -> None:
    config = config or load_config()
    entries = read_history()
    conversation_entries = [entry for entry in entries if entry.get("role") != "summary"]
    total_chars = sum(entry_content_size(entry) for entry in conversation_entries)
    threshold = int(config.get("compact_threshold_chars", DEFAULT_CONFIG["compact_threshold_chars"]))
    chunk_target = int(config.get("archive_chunk_chars", DEFAULT_CONFIG["archive_chunk_chars"]))
    temp_per_cons = int(
        config.get("temp_summaries_per_consolidation", DEFAULT_CONFIG["temp_summaries_per_consolidation"])
    )
    max_visible_consolidated = int(
        config.get("max_visible_consolidated", DEFAULT_CONFIG["max_visible_consolidated"])
    )
    meta_perm_threshold = int(
        config.get("meta_permanent_threshold", DEFAULT_CONFIG["meta_permanent_threshold"])
    )

    if total_chars <= threshold:
        return

    meta_perm = [entry for entry in entries if entry.get("summary_type") == "meta_permanent"]
    meta_temp = [entry for entry in entries if entry.get("summary_type") == "meta_temporary"]
    consolidated = [entry for entry in entries if entry.get("summary_type") == "consolidated"]
    temporary = [entry for entry in entries if entry.get("summary_type") == "temporary"]
    conversation = [entry for entry in entries if entry.get("role") != "summary"]

    while sum(entry_content_size(entry) for entry in conversation) > threshold:
        chunk_entries, remaining_conversation = select_conversation_chunk(conversation, chunk_target)
        if not chunk_entries:
            break

        facts_output = extract_user_facts_from_chunk(chunk_entries, config)
        append_user_facts(parse_fact_lines(facts_output), config)

        context_entries = meta_perm + consolidated + temporary + remaining_conversation
        summary_text, pending_id = summarize_entries(
            chunk_entries,
            label="temporary",
            config=config,
            context_entries=context_entries,
            pending_meta={
                "summary_type": "temporary",
                "covers_from": chunk_entries[0].get("timestamp", "?"),
                "covers_to": chunk_entries[-1].get("timestamp", "?"),
            },
        )
        archive_name = archive_temp_chunk(chunk_entries, summary_text)
        temp_entry = {
            "role": "summary",
            "summary_type": "temporary",
            "content": summary_text,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "covers_from": chunk_entries[0].get("timestamp", "?"),
            "covers_to": chunk_entries[-1].get("timestamp", "?"),
            "archive_file": archive_name,
            "entry_count": len(chunk_entries),
        }
        if pending_id:
            temp_entry["summary_pending_id"] = pending_id
        temporary.append(temp_entry)
        conversation = remaining_conversation

    while len(temporary) > temp_per_cons + 1:
        to_consolidate = temporary[:temp_per_cons]
        remaining_temp = temporary[temp_per_cons:]
        raw_entries: list[dict[str, Any]] = []
        archive_files_to_delete: list[str] = []
        for summary in to_consolidate:
            archive_name = str(summary.get("archive_file") or "")
            if not archive_name:
                continue
            raw_entries.extend(read_archive_entries(archive_name))
            archive_files_to_delete.append(archive_name)

        if raw_entries:
            context_entries = consolidated + remaining_temp + conversation
            cons_summary, pending_id = summarize_entries(
                raw_entries,
                label="consolidated",
                config=config,
                context_entries=context_entries,
                pending_meta={
                    "summary_type": "consolidated",
                    "covers_from": to_consolidate[0].get("covers_from", ""),
                    "covers_to": to_consolidate[-1].get("covers_to", ""),
                },
            )
            cons_archive = archive_consolidated_chunk(raw_entries, to_consolidate, cons_summary)
            cons_entry = {
                "role": "summary",
                "summary_type": "consolidated",
                "content": cons_summary,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "covers_from": to_consolidate[0].get("covers_from", ""),
                "covers_to": to_consolidate[-1].get("covers_to", ""),
                "archive_file": cons_archive,
                "source_archives": archive_files_to_delete,
            }
            if pending_id:
                cons_entry["summary_pending_id"] = pending_id
            consolidated.append(cons_entry)
            for archive_name in archive_files_to_delete:
                try:
                    (ARCHIVES_DIR / archive_name).unlink(missing_ok=True)
                except Exception:
                    pass
        else:
            cons_summary, pending_id = summarize_summary_entries(
                to_consolidate,
                label="consolidated",
                config=config,
                context_entries=consolidated + remaining_temp + conversation,
                pending_meta={
                    "summary_type": "consolidated",
                    "covers_from": to_consolidate[0].get("covers_from", ""),
                    "covers_to": to_consolidate[-1].get("covers_to", ""),
                },
            )
            cons_entry = {
                "role": "summary",
                "summary_type": "consolidated",
                "content": cons_summary,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "covers_from": to_consolidate[0].get("covers_from", ""),
                "covers_to": to_consolidate[-1].get("covers_to", ""),
                "source_archives": archive_files_to_delete,
            }
            if pending_id:
                cons_entry["summary_pending_id"] = pending_id
            consolidated.append(cons_entry)

        temporary = remaining_temp

    if len(consolidated) > max_visible_consolidated + 1:
        to_keep = consolidated[:max_visible_consolidated]
        overflow = consolidated[max_visible_consolidated:]
        existing_meta = meta_temp[-1] if meta_temp else None
        source_archives: list[str] = []
        covers_from = overflow[0].get("covers_from", "") if overflow else ""
        if existing_meta is not None:
            source_archives.extend(list(existing_meta.get("source_archives", [])))
            covers_from = existing_meta.get("covers_from", covers_from)
        source_archives.extend([entry.get("archive_file", "") for entry in overflow if entry.get("archive_file")])
        covers_to = overflow[-1].get("covers_to", "") if overflow else ""

        combined_entries: list[dict[str, Any]] = []
        if existing_meta is not None:
            combined_entries.append(existing_meta)
        combined_entries.extend(overflow)
        summary_content, pending_id = summarize_summary_entries(
            combined_entries,
            label="meta",
            config=config,
            context_entries=meta_perm + to_keep,
            pending_meta={
                "summary_type": "meta_permanent" if len(source_archives) >= meta_perm_threshold else "meta_temporary",
                "covers_from": covers_from,
                "covers_to": covers_to,
            },
        )

        new_meta = {
            "role": "summary",
            "summary_type": "meta_permanent" if len(source_archives) >= meta_perm_threshold else "meta_temporary",
            "content": summary_content,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "covers_from": covers_from,
            "covers_to": covers_to,
            "source_archives": [archive for archive in source_archives if archive],
        }
        if pending_id:
            new_meta["summary_pending_id"] = pending_id
        meta_temp = []
        if new_meta["summary_type"] == "meta_permanent":
            meta_perm.append(new_meta)
        else:
            meta_temp = [new_meta]
        consolidated = to_keep

    rewrite_history(meta_perm + consolidated + meta_temp + temporary + conversation)


def select_conversation_chunk(
    conversation: list[dict[str, Any]], chunk_target: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    groups = group_entries(conversation)
    if len(groups) < 2:
        return [], conversation

    char_count = 0
    split_group_idx = 0
    last_assistant_group_idx = -1

    for group_index, group in enumerate(groups):
        group_size = entry_content_size(group["message"])
        for file_entry in group.get("files", []):
            group_size += entry_content_size(file_entry)
        char_count += group_size

        if str(group["message"].get("role") or "").lower() == "assistant":
            last_assistant_group_idx = group_index

        if char_count >= chunk_target:
            split_group_idx = group_index + 1
            break

    if split_group_idx == 0 or split_group_idx >= len(groups):
        return [], conversation

    if 0 <= last_assistant_group_idx < split_group_idx:
        split_group_idx = last_assistant_group_idx + 1
    elif last_assistant_group_idx < 0:
        for group_index in range(split_group_idx, len(groups)):
            if str(groups[group_index]["message"].get("role") or "").lower() == "assistant":
                split_group_idx = group_index + 1
                break

    if split_group_idx == 0 or split_group_idx >= len(groups):
        return [], conversation

    chunk_entries: list[dict[str, Any]] = []
    remaining: list[dict[str, Any]] = []

    for group in groups[:split_group_idx]:
        chunk_entries.append(group["message"])
        chunk_entries.extend(group.get("files", []))

    for group in groups[split_group_idx:]:
        remaining.append(group["message"])
        remaining.extend(group.get("files", []))

    return chunk_entries, remaining


def archive_temp_chunk(entries: list[dict[str, Any]], summary: str) -> str:
    ensure_state_dir()
    first_ts = safe_date_stamp(entries[0].get("timestamp", "unknown"))
    last_ts = safe_date_stamp(entries[-1].get("timestamp", "unknown"))
    digest = hashlib.sha256(json.dumps(entries, sort_keys=True).encode("utf-8")).hexdigest()[:8]
    name = f"temp_{first_ts}_to_{last_ts}_{digest}.jsonl"
    path = ARCHIVES_DIR / name
    header = {
        "_chunk": True,
        "from": entries[0].get("timestamp", ""),
        "to": entries[-1].get("timestamp", ""),
        "summary": summary,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(header, ensure_ascii=False) + "\n")
        for entry in entries:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return name


def archive_consolidated_chunk(
    raw_entries: list[dict[str, Any]], temp_summaries: list[dict[str, Any]], summary: str
) -> str:
    ensure_state_dir()
    first_ts = safe_date_stamp(temp_summaries[0].get("covers_from", "unknown"))
    last_ts = safe_date_stamp(temp_summaries[-1].get("covers_to", "unknown"))
    digest = hashlib.sha256(json.dumps(temp_summaries, sort_keys=True).encode("utf-8")).hexdigest()[:8]
    name = f"cons_{first_ts}_to_{last_ts}_{digest}.jsonl"
    path = ARCHIVES_DIR / name
    header = {
        "_consolidated": True,
        "summary_type": "consolidated",
        "from": temp_summaries[0].get("covers_from", ""),
        "to": temp_summaries[-1].get("covers_to", ""),
        "summary": summary,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_archives": [entry.get("archive_file", "") for entry in temp_summaries if entry.get("archive_file")],
    }
    with path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(header, ensure_ascii=False) + "\n")
        for entry in raw_entries:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return name


def read_archive_entries(archive_name: str) -> list[dict[str, Any]]:
    path = ARCHIVES_DIR / archive_name
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("_consolidated") or obj.get("_chunk"):
                continue
            entries.append(obj)
    return entries


def summarize_entries(
    entries: list[dict[str, Any]],
    label: str = "temporary",
    config: dict[str, Any] | None = None,
    context_entries: list[dict[str, Any]] | None = None,
    pending_meta: dict[str, Any] | None = None,
) -> tuple[str, str | None]:
    config = config or load_config()
    context_text = format_entries_for_model(context_entries or [], max_chars=MODEL_CONTEXT_MAX_CHARS)
    if config.get("enable_model_summaries") and openai_settings(config):
        summary_text = generate_model_summary(entries, label, context_text, config)
        if summary_text:
            return summary_text, None
        pending_id = None
        if pending_meta and config.get("pending_retry_enabled", True):
            pending_id = queue_pending_summary(entries, label, context_entries or [], pending_meta, config)
        return summarize_entries_deterministic(entries, label), pending_id
    return summarize_entries_deterministic(entries, label), None


def summarize_summary_entries(
    entries: list[dict[str, Any]],
    label: str = "meta",
    config: dict[str, Any] | None = None,
    context_entries: list[dict[str, Any]] | None = None,
    pending_meta: dict[str, Any] | None = None,
) -> tuple[str, str | None]:
    config = config or load_config()
    context_text = format_entries_for_model(context_entries or [], max_chars=MODEL_CONTEXT_MAX_CHARS)
    if config.get("enable_model_summaries") and openai_settings(config):
        summary_text = generate_model_summary(entries, label, context_text, config)
        if summary_text:
            return summary_text, None
        pending_id = None
        if pending_meta and config.get("pending_retry_enabled", True):
            pending_id = queue_pending_summary(entries, label, context_entries or [], pending_meta, config)
        return summarize_summary_entries_deterministic(entries, label), pending_id
    return summarize_summary_entries_deterministic(entries, label), None


def summarize_entries_deterministic(entries: list[dict[str, Any]], label: str = "temporary") -> str:
    lines = [f"{label.capitalize()} summary over {len(entries)} archived entries."]
    seen_threads: set[str] = set()
    for entry in entries[:20]:
        role = str(entry.get("role", "unknown")).upper()
        if entry.get("role") == "file":
            text = f"file from {entry.get('from', '?')}: {entry.get('path', '?')}"
            if entry.get("description"):
                text += f" — {entry.get('description')}"
        else:
            text = " ".join(str(entry.get("content", "")).split())
        if len(text) > 220:
            text = text[:217] + "..."
        thread_id = str(entry.get("thread_id") or "")[:8]
        if thread_id and thread_id not in seen_threads:
            seen_threads.add(thread_id)
            lines.append(f"- [thread {thread_id}]")
        lines.append(f"- {role}: {text}")
    if len(entries) > 20:
        lines.append(f"- ... plus {len(entries) - 20} more entries in the archive")
    return "\n".join(lines)


def summarize_summary_entries_deterministic(entries: list[dict[str, Any]], label: str = "meta") -> str:
    lines = [f"{label.capitalize()} summary spanning {len(entries)} prior summaries."]
    for entry in entries[:12]:
        summary_type = str(entry.get("summary_type") or entry.get("role") or "summary")
        covers_from = entry.get("covers_from", "?")
        covers_to = entry.get("covers_to", "?")
        content = " ".join(str(entry.get("content", "")).split())
        if len(content) > 260:
            content = content[:257] + "..."
        lines.append(f"- {summary_type}: {covers_from} -> {covers_to} | {content}")
    if len(entries) > 12:
        lines.append(f"- ... plus {len(entries) - 12} more summary entries")
    return "\n".join(lines)


def format_entries_for_model(entries: list[dict[str, Any]], max_chars: int = MODEL_CONTEXT_MAX_CHARS) -> str:
    if not entries:
        return ""

    lines: list[str] = []
    current_size = 0
    for group in group_entries(entries):
        message = group["message"]
        rendered = render_entry(message, include_timestamps=False)
        if rendered:
            rendered_size = len(rendered) + 1
            if current_size + rendered_size > max_chars:
                break
            lines.append(rendered)
            current_size += rendered_size
        for file_entry in group.get("files", []):
            rendered_file = render_file_entry(file_entry)
            if not rendered_file:
                continue
            rendered_size = len(rendered_file) + 1
            if current_size + rendered_size > max_chars:
                return "\n".join(lines)
            lines.append(rendered_file)
            current_size += rendered_size
    return "\n".join(lines)


def openai_settings(config: dict[str, Any]) -> dict[str, Any] | None:
    api_key = str(config.get("openai_api_key") or "").strip()
    if not api_key:
        env_name = str(config.get("openai_api_key_env") or "OPENAI_API_KEY").strip()
        api_key = os.getenv(env_name, "").strip()
    if not api_key:
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None

    base_url = str(config.get("openai_base_url") or "").strip() or os.getenv("OPENAI_BASE_URL", "").strip()
    model = str(config.get("openai_model") or "").strip() or "gpt-5.4"
    reasoning_effort = str(config.get("openai_reasoning_effort") or "").strip() or "high"
    timeout = int(config.get("openai_timeout_seconds", 45))

    return {
        "api_key": api_key,
        "base_url": base_url or OPENAI_RESPONSES_URL,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "timeout": timeout,
    }


def call_openai_responses(
    instructions: str,
    content: list[dict[str, Any]],
    config: dict[str, Any],
    max_retries: int = 3,
) -> str | None:
    settings = openai_settings(config)
    if settings is None:
        return None

    payload = {
        "model": settings["model"],
        "instructions": instructions,
        "input": [
            {
                "role": "user",
                "content": content,
            }
        ],
        "store": False,
    }
    if settings.get("reasoning_effort"):
        payload["reasoning"] = {"effort": settings["reasoning_effort"]}
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings['api_key']}",
    }

    for attempt in range(max_retries):
        try:
            request = Request(settings["base_url"], data=body, headers=headers, method="POST")
            with urlopen(request, timeout=settings["timeout"]) as response:
                result = json.loads(response.read().decode("utf-8"))
            output_text = extract_output_text(result)
            if output_text:
                return output_text.strip()
            return None
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
            if attempt < max_retries - 1:
                time.sleep(2**attempt)
                continue
            return None
        except Exception:
            return None
    return None


def extract_output_text(result: dict[str, Any]) -> str:
    output_text = result.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    output = result.get("output")
    if not isinstance(output, list):
        return ""

    texts: list[str] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for block in item.get("content", []):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "output_text":
                text = str(block.get("text") or "").strip()
                if text:
                    texts.append(text)
    return "\n\n".join(texts).strip()


def generate_model_summary(
    entries: list[dict[str, Any]],
    label: str,
    context_text: str,
    config: dict[str, Any],
) -> str | None:
    conversation_text = format_entries_for_model(entries, max_chars=MODEL_INPUT_MAX_CHARS)
    if not conversation_text:
        return None

    if label == "temporary":
        instructions = (
            "You are summarizing a specific segment of an ongoing user-assistant conversation. "
            "Write a dense, chronological summary that preserves concrete decisions, files, "
            "bugs, requests, outcomes, and any attachments. Output only the summary."
        )
        task = (
            "Summarize ONLY the conversation segment below in substantial detail. "
            "Preserve chronology, important file paths, user intent, assistant actions, "
            "and what changed. Do not summarize the context block."
        )
    elif label == "consolidated":
        instructions = (
            "You are consolidating multiple earlier conversation segments into one memory summary. "
            "Write a dense, chronological summary that preserves major decisions, files, "
            "deliverables, issues, and outcomes. Output only the summary."
        )
        task = (
            "Create one consolidated summary of the archived conversation history below. "
            "Keep the chronology clear and retain the most important file paths, decisions, "
            "deliverables, and unresolved context. Do not summarize the context block."
        )
    else:
        instructions = (
            "You are creating a high-level summary of summaries for long-term memory. "
            "Preserve major milestones, project evolution, durable user preferences, and "
            "important file references. Output only the summary."
        )
        task = (
            "Create a meta-summary from the summary entries below. Keep the chronology of "
            "major milestones clear and retain high-value file references and decisions. "
            "Do not summarize the context block."
        )

    content = []
    if context_text:
        content.append(
            {
                "type": "input_text",
                "text": (
                    "Context from the rest of the memory timeline. Use it only to resolve references; "
                    "do not summarize it.\n\n"
                    f"{context_text}"
                ),
            }
        )
    content.append(
        {
            "type": "input_text",
            "text": f"{task}\n\nCONTENT TO SUMMARIZE:\n\n{conversation_text}",
        }
    )
    return call_openai_responses(instructions, content, config)


def describe_file_entry(
    path: Path,
    media_type: str,
    config: dict[str, Any] | None = None,
    chat_context: str = "",
) -> str:
    config = config or load_config()
    fallback = describe_file_deterministic(path, media_type)
    if not config.get("enable_model_file_descriptions"):
        return fallback

    settings = openai_settings(config)
    if settings is None:
        return fallback

    try:
        size_bytes = path.stat().st_size
    except Exception:
        return fallback

    if size_bytes <= 0 or size_bytes > int(config.get("model_file_max_bytes", MODEL_FILE_MAX_BYTES)):
        return fallback

    try:
        raw = path.read_bytes()
    except Exception:
        return fallback

    content: list[dict[str, Any]] = []
    if chat_context:
        content.append(
            {
                "type": "input_text",
                "text": f"Recent conversation context:\n{chat_context[:MODEL_CONTEXT_MAX_CHARS]}",
            }
        )

    if media_type in SUPPORTED_VISION_MEDIA_TYPES:
        data_uri = f"data:{media_type};base64,{base64.b64encode(raw).decode('ascii')}"
        content.append(
            {
                "type": "input_image",
                "image_url": data_uri,
                "detail": "auto",
            }
        )
    else:
        content.append(
            {
                "type": "input_file",
                "filename": path.name,
                "file_data": base64.b64encode(raw).decode("ascii"),
            }
        )

    content.append(
        {
            "type": "input_text",
            "text": (
                "Describe this file in about 50 tokens. Be specific and factual about what it is and why "
                "it was relevant to the conversation. Output only the description."
            ),
        }
    )

    instructions = (
        "You write short, factual descriptions for files that appeared in a user-assistant conversation. "
        "Be specific, mention the likely purpose, and avoid fluff. Output only the description."
    )
    return call_openai_responses(instructions, content, config) or fallback


def describe_file_deterministic(path: Path, media_type: str) -> str:
    try:
        if media_type.startswith("image/"):
            return f"Image attachment named {path.name}"
        if media_type == "application/pdf":
            return f"PDF document named {path.name}"
        if path.suffix.lower() in {".c", ".cc", ".cpp", ".css", ".go", ".h", ".html", ".java", ".js", ".json", ".jsx", ".md", ".py", ".rb", ".rs", ".sh", ".sql", ".swift", ".toml", ".ts", ".tsx", ".txt", ".xml", ".yaml", ".yml"} and path.stat().st_size <= 64 * 1024:
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                snippet = " ".join(line.strip().split())
                if snippet:
                    if len(snippet) > 120:
                        snippet = snippet[:117] + "..."
                    return f"Text file named {path.name}; first content: {snippet}"
            return f"Text file named {path.name}"
    except Exception:
        pass
    return f"File attachment named {path.name}"


def load_recent_history_context(max_entries: int = 20) -> str:
    entries = read_history()
    if not entries:
        return ""
    return format_entries_for_model(entries[-max_entries:], max_chars=12000)


def extract_user_facts_from_chunk(entries: list[dict[str, Any]], config: dict[str, Any]) -> str:
    if not config.get("enable_user_facts"):
        return "NONE"

    chunk_text = format_entries_for_model(entries, max_chars=MODEL_INPUT_MAX_CHARS)
    if not chunk_text:
        return "NONE"

    if config.get("enable_model_user_facts") and openai_settings(config):
        existing = "\n".join(f"- {fact.get('fact', '')}" for fact in read_facts()) or "Empty — no facts yet"
        instructions = (
            "You extract durable personal facts about the user from conversation history. "
            "Only keep long-term, stable information that would help a human personal assistant "
            "understand who the user is. Exclude tools, models, file paths, scripts, package names, "
            "technical workflows, temporary tasks, and duplicate facts. Output only bullet points "
            "starting with '- ', or NONE."
        )
        content = [
            {
                "type": "input_text",
                "text": (
                    f"Existing user context:\n{existing}\n\n"
                    f"New conversation chunk to analyze:\n{chunk_text}"
                ),
            }
        ]
        result = call_openai_responses(instructions, content, config)
        if result:
            return result

    fallback_facts: list[str] = []
    for entry in entries:
        if entry.get("role") == "user":
            fallback_facts.extend(extract_user_facts(str(entry.get("content") or "")))
    if not fallback_facts:
        return "NONE"
    return "\n".join(f"- {fact}" for fact in fallback_facts)


def parse_fact_lines(text: str) -> list[str]:
    if not text or text.strip().upper() == "NONE":
        return []
    facts: list[str] = []
    for line in text.splitlines():
        clean = line.strip().lstrip("-*• ").strip()
        if not clean or clean.upper() == "NONE":
            continue
        facts.append(normalize_fact(clean))
    deduped: list[str] = []
    seen: set[str] = set()
    for fact in facts:
        key = fact.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(fact)
    return deduped


def condense_user_facts_if_needed(config: dict[str, Any]) -> None:
    facts = read_facts()
    if not facts:
        return

    total_chars = sum(len(str(fact.get("fact") or "")) for fact in facts)
    if total_chars <= int(config.get("user_facts_max_chars", DEFAULT_CONFIG["user_facts_max_chars"])):
        return

    if not config.get("enable_model_user_facts") or not openai_settings(config):
        return

    content = "\n".join(f"- {fact.get('fact', '')}" for fact in facts)
    instructions = (
        "You are condensing a long list of durable user facts. Merge duplicates, remove redundancy, "
        "organize by meaning implicitly, and keep only long-term personal facts that matter to a human "
        "assistant. Exclude tools, models, technical architecture, file paths, and temporary tasks. "
        "Output only bullet points starting with '- '."
    )
    result = call_openai_responses(
        instructions,
        [{"type": "input_text", "text": content}],
        config,
    )
    parsed = parse_fact_lines(result or "")
    if not parsed:
        return

    now = datetime.now(timezone.utc).isoformat()
    temp_path = FACTS_FILE.with_suffix(".jsonl.tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        for fact_text in parsed:
            handle.write(json.dumps({"id": fact_id(fact_text), "fact": fact_text, "added": now}, ensure_ascii=False) + "\n")
    temp_path.replace(FACTS_FILE)


def queue_pending_summary(
    entries: list[dict[str, Any]],
    label: str,
    context_entries: list[dict[str, Any]],
    pending_meta: dict[str, Any],
    config: dict[str, Any],
) -> str | None:
    covers_from = str(pending_meta.get("covers_from") or "")
    covers_to = str(pending_meta.get("covers_to") or "")
    summary_type = str(pending_meta.get("summary_type") or label)
    pending_id = hashlib.sha256(f"{summary_type}:{covers_from}:{covers_to}".encode("utf-8")).hexdigest()[:12]
    task = {
        "kind": "summary_refresh",
        "label": label,
        "summary_type": summary_type,
        "covers_from": covers_from,
        "covers_to": covers_to,
        "entries": entries,
        "context_entries": context_entries,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    task_path = pending_task_path(pending_id)
    if not task_path.exists():
        save_json(task_path, task)
    spawn_background_worker(pending_id, config)
    return pending_id


def pending_task_path(pending_id: str) -> Path:
    return PENDING_DIR / f"{pending_id}.json"


def pending_pid_path(pending_id: str) -> Path:
    return PENDING_DIR / f"{pending_id}.pid"


def is_worker_alive(pending_id: str) -> bool:
    pid_path = pending_pid_path(pending_id)
    if not pid_path.exists():
        return False
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
        return True
    except Exception:
        try:
            pid_path.unlink(missing_ok=True)
        except Exception:
            pass
        return False


def spawn_background_worker(pending_id: str, config: dict[str, Any] | None = None) -> None:
    config = config or load_config()
    if not config.get("pending_retry_enabled", True):
        return
    if is_worker_alive(pending_id):
        return

    try:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--retry-pending", pending_id],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        return


def resume_pending_tasks(config: dict[str, Any] | None = None) -> None:
    config = config or load_config()
    if not config.get("pending_retry_enabled", True):
        return
    ensure_state_dir()
    for path in sorted(PENDING_DIR.glob("*.json")):
        pending_id = path.stem
        if not is_worker_alive(pending_id):
            spawn_background_worker(pending_id, config)


def run_pending_worker(pending_id: str) -> None:
    config = load_config()
    task_path = pending_task_path(pending_id)
    pid_path = pending_pid_path(pending_id)

    if not task_path.exists():
        return

    try:
        pid_path.write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        return

    delay = int(config.get("pending_retry_base_seconds", 30))
    max_delay = int(config.get("pending_retry_max_seconds", 480))

    try:
        while task_path.exists():
            if retry_pending_task(pending_id, config):
                try:
                    task_path.unlink(missing_ok=True)
                except Exception:
                    pass
                return
            time.sleep(delay)
            delay = min(delay * 2, max_delay)
    finally:
        try:
            pid_path.unlink(missing_ok=True)
        except Exception:
            pass


def retry_pending_task(pending_id: str, config: dict[str, Any]) -> bool:
    task_path = pending_task_path(pending_id)
    if not task_path.exists():
        return True
    try:
        task = json.loads(task_path.read_text(encoding="utf-8"))
    except Exception:
        return False

    if task.get("kind") != "summary_refresh":
        return True

    label = str(task.get("label") or "temporary")
    entries = list(task.get("entries") or [])
    context_entries = list(task.get("context_entries") or [])
    context_text = format_entries_for_model(context_entries, max_chars=MODEL_CONTEXT_MAX_CHARS)
    summary_text = generate_model_summary(entries, label, context_text, config)
    if not summary_text:
        return False

    update_summary_entry(
        pending_id=pending_id,
        covers_from=str(task.get("covers_from") or ""),
        covers_to=str(task.get("covers_to") or ""),
        summary_text=summary_text,
    )
    return True


def update_summary_entry(pending_id: str, covers_from: str, covers_to: str, summary_text: str) -> None:
    entries = read_history()
    changed = False
    for entry in entries:
        if entry.get("role") != "summary":
            continue
        if entry.get("summary_pending_id") == pending_id or (
            entry.get("covers_from") == covers_from and entry.get("covers_to") == covers_to
        ):
            entry["content"] = summary_text
            entry.pop("summary_pending_id", None)
            update_archive_header(entry, summary_text)
            changed = True
            break
    if changed:
        rewrite_history(entries)


def update_archive_header(entry: dict[str, Any], summary_text: str) -> None:
    archive_name = str(entry.get("archive_file") or "")
    if not archive_name:
        return
    archive_path = ARCHIVES_DIR / archive_name
    if not archive_path.exists():
        return

    try:
        lines = archive_path.read_text(encoding="utf-8").splitlines()
        if not lines:
            return
        header = json.loads(lines[0])
        if not isinstance(header, dict):
            return
        header["summary"] = summary_text
        lines[0] = json.dumps(header, ensure_ascii=False)
        archive_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        return


def fetch_calendar_section(days: int = 30, timeout: int = 10) -> str:
    if shutil.which("gws") is None:
        return ""
    try:
        result = subprocess.run(
            ["gws", "calendar", "+agenda", "--days", str(days), "--format", "json"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception:
        return ""

    if result.returncode != 0 or not result.stdout.strip():
        return ""

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return ""

    if isinstance(payload, dict):
        events = payload.get("events", [])
    else:
        events = payload
    if not isinstance(events, list):
        return ""

    now = datetime.now(timezone.utc)
    today_start = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + timedelta(days=1)
    week_end = today_start + timedelta(days=7)

    today_events: list[tuple[dict[str, Any], datetime, datetime | None]] = []
    week_events: list[tuple[dict[str, Any], datetime, datetime | None]] = []
    future_events: list[tuple[dict[str, Any], datetime, datetime | None]] = []

    for event in events:
        if not isinstance(event, dict):
            continue
        start_dt = parse_calendar_dt(event.get("start"))
        end_dt = parse_calendar_dt(event.get("end"))
        if start_dt is None:
            continue
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        if end_dt is not None and end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)

        local_start = start_dt.astimezone()
        item = (event, local_start, end_dt.astimezone() if end_dt else None)
        if local_start < today_end:
            today_events.append(item)
        elif local_start < week_end:
            week_events.append(item)
        else:
            future_events.append(item)

    today_events.sort(key=lambda item: item[1])
    week_events.sort(key=lambda item: item[1])
    future_events.sort(key=lambda item: item[1])

    parts = [
        "=== CALENDAR (next 30 days) ===",
        "[Skip this section during compaction — it is re-injected automatically after compaction.]",
        "",
        f"## Today ({today_start.strftime('%A, %B %d')})",
    ]

    if today_events:
        for event, start_dt, end_dt in today_events:
            parts.append(format_calendar_event_full(event, start_dt, end_dt))
    else:
        parts.append("  No events today.")
    parts.append("")

    if week_events:
        parts.append("## This week")
        current_date = ""
        for event, start_dt, end_dt in week_events:
            event_date = start_dt.strftime("%A, %B %d")
            if event_date != current_date:
                current_date = event_date
                parts.append(f"  {event_date}:")
            parts.append(format_calendar_event_moderate(event, start_dt, end_dt))
        parts.append("")

    if future_events:
        parts.append("## Coming up")
        current_date = ""
        for event, start_dt, end_dt in future_events:
            event_date = start_dt.strftime("%A, %B %d")
            if event_date != current_date:
                current_date = event_date
                parts.append(f"  {event_date}:")
            parts.append(format_calendar_event_compact(event, start_dt, end_dt))

    return "\n".join(parts).strip()


def parse_calendar_dt(value: Any) -> datetime | None:
    if isinstance(value, dict):
        text = value.get("dateTime") or value.get("date")
    else:
        text = value
    if not isinstance(text, str) or not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None


def format_calendar_range(start_dt: datetime, end_dt: datetime | None) -> str:
    start_text = start_dt.strftime("%H:%M")
    zone_text = start_dt.strftime("%Z") or "local"
    if end_dt is None:
        return f"{start_text} {zone_text}"
    return f"{start_text}-{end_dt.strftime('%H:%M')} {zone_text}"


def format_calendar_event_full(event: dict[str, Any], start_dt: datetime, end_dt: datetime | None) -> str:
    lines = [f"  {format_calendar_range(start_dt, end_dt)} — {event.get('summary', 'Untitled')}"]
    location = str(event.get("location") or "").strip()
    if location:
        lines.append(f"    Location: {location}")
    description = str(event.get("description") or "").strip()
    if description:
        clipped = description[:200] + ("..." if len(description) > 200 else "")
        lines.append(f"    Details: {clipped}")
    attendees = event.get("attendees")
    if isinstance(attendees, list) and attendees:
        names: list[str] = []
        for attendee in attendees[:5]:
            if isinstance(attendee, dict):
                label = attendee.get("displayName") or attendee.get("email")
                if label:
                    names.append(str(label))
            elif isinstance(attendee, str):
                names.append(attendee)
        if names:
            lines.append(f"    Attendees: {', '.join(names)}")
    return "\n".join(lines)


def format_calendar_event_moderate(event: dict[str, Any], start_dt: datetime, end_dt: datetime | None) -> str:
    line = f"  {format_calendar_range(start_dt, end_dt)} — {event.get('summary', 'Untitled')}"
    location = str(event.get("location") or "").strip()
    if location:
        line += f" ({location})"
    return line


def format_calendar_event_compact(event: dict[str, Any], start_dt: datetime, end_dt: datetime | None) -> str:
    return f"  {format_calendar_range(start_dt, end_dt)} — {event.get('summary', 'Untitled')}"


def format_date(value: str) -> str:
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d")
    except Exception:
        return ""


def format_timestamp(value: str) -> str:
    try:
        dt = datetime.fromisoformat(value)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M %Z")
    except Exception:
        return value


def safe_date_stamp(value: str) -> str:
    date_part = format_date(value)
    return date_part or "unknown"


def _extract_short_id(archive_file: str) -> str:
    if not archive_file:
        return ""
    stem = archive_file.rsplit(".", 1)[0]
    suffix = stem.rsplit("_", 1)[-1]
    return suffix[:6]


def _archive_suffix(archive_file: str, source_archives: list[str]) -> str:
    if source_archives:
        ids = [_extract_short_id(path) for path in source_archives if path]
        ids = [identifier for identifier in ids if identifier]
        if ids:
            return " | ids: " + ", ".join(ids)
    if archive_file:
        short_id = _extract_short_id(archive_file)
        if short_id:
            return f" | id: {short_id}"
    return ""


def first_present(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None


def size_str(path: Path) -> str:
    if path.is_file():
        size = path.stat().st_size
    elif path.is_dir():
        size = sum(file.stat().st_size for file in path.rglob("*") if file.is_file())
    else:
        return "0 B"

    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def size_str_bytes(size: int) -> str:
    value = float(size)
    for unit in ["B", "KB", "MB", "GB"]:
        if value < 1024:
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def entry_content_size(entry: dict[str, Any]) -> int:
    role = str(entry.get("role") or "").lower()
    if role == "file":
        text = str(entry.get("path") or "")
        text += str(entry.get("description") or "")
        return max(len(text), 48)
    return len(str(entry.get("content") or "").strip())


def empty_success() -> None:
    print("{}")


def print_session_start_context(context: str) -> None:
    if not context:
        empty_success()
        return

    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }
    print(json.dumps(output, ensure_ascii=False))


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "--retry-pending":
        run_pending_worker(argv[2])
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
