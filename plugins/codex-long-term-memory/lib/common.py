#!/usr/bin/env python3
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_DIR = Path.home() / ".codex" / "long-term-memory"
CONFIG_FILE = STATE_DIR / "config.json"
HISTORY_FILE = STATE_DIR / "history.jsonl"
FACTS_FILE = STATE_DIR / "user_facts.jsonl"
ARCHIVES_DIR = STATE_DIR / "archives"
BACKUPS_DIR = STATE_DIR / "backups"

DEFAULT_CONFIG = {
    "max_injection_chars": 200000,
    "max_entries": 400,
    "include_timestamps": True,
    "enable_user_facts": True,
    "enable_calendar": True,
    "compact_threshold_chars": 120000,
    "archive_chunk_chars": 40000,
}


def ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVES_DIR.mkdir(parents=True, exist_ok=True)
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)


def data_items() -> list[tuple[Path, str]]:
    return [
        (HISTORY_FILE, "file"),
        (FACTS_FILE, "file"),
        (ARCHIVES_DIR, "dir"),
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


def append_history_entry(role: str, content: str, payload: dict[str, Any]) -> None:
    text = (content or "").strip()
    if not text:
        return

    ensure_state_dir()
    config = load_config()
    entry = {
        "role": role,
        "content": text,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "thread_id": first_present(payload, "thread_id", "threadId", "session_id", "sessionId"),
        "turn_id": first_present(payload, "turn_id", "turnId"),
    }

    with HISTORY_FILE.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    if config.get("enable_user_facts") and role == "user":
        append_user_facts(extract_user_facts(text))

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


def append_user_facts(facts: list[str]) -> None:
    if not facts:
        return

    existing = read_facts()
    existing_ids = {fact.get("id") for fact in existing}
    now = datetime.now(timezone.utc).isoformat()

    with FACTS_FILE.open("a", encoding="utf-8") as handle:
        for fact_text in facts:
            fact_identifier = fact_id(fact_text)
            if fact_identifier in existing_ids:
                continue
            payload = {
                "id": fact_identifier,
                "fact": fact_text,
                "added": now,
            }
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            existing_ids.add(fact_identifier)


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
    ]

    for pattern in patterns:
        for match in re.finditer(pattern, cleaned, flags=re.IGNORECASE):
            clause = match.group(0).strip().rstrip(".")
            facts.append(normalize_fact(clause))

    if len(cleaned) < 180 and re.search(r"\bmy name is\b", cleaned, flags=re.IGNORECASE):
        facts.append(normalize_fact(cleaned.rstrip(".")))

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
    normalized = text.strip()
    if normalized[:1].islower():
        normalized = normalized[:1].upper() + normalized[1:]
    return normalized


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
        "=== USER CONTEXT (durable facts) ===",
        "These are stable facts learned from earlier conversations with the user.",
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
    max_entries = int(config.get("max_entries", DEFAULT_CONFIG["max_entries"]))
    include_timestamps = bool(config.get("include_timestamps", True))

    selected: list[dict[str, Any]] = []
    total_chars = 0

    for entry in reversed(entries):
        rendered = render_entry(entry, include_timestamps)
        if not rendered:
            continue
        entry_chars = len(rendered) + 2
        if selected and (len(selected) >= max_entries or total_chars + entry_chars > max_chars):
            break
        selected.append(entry)
        total_chars += entry_chars

    selected.reverse()

    lines = [
        "=== LONG-TERM CONVERSATION MEMORY ===",
        "This is cross-thread context from earlier Codex sessions with the same user.",
        "Prefer the current thread if anything here conflicts with newer instructions.",
        "When a summary looks relevant, read the referenced archive file before relying on the summary alone.",
        "",
    ]

    current_date = None
    for entry in selected:
        timestamp = entry.get("timestamp", "")
        date_str = format_date(timestamp)
        if date_str and date_str != current_date:
            if current_date is not None:
                lines.append("")
            lines.append(f"--- {date_str} ---")
            lines.append("")
            current_date = date_str

        rendered = render_entry(entry, include_timestamps)
        if rendered:
            lines.append(rendered)
            lines.append("")

    return "\n".join(lines).strip()


def render_entry(entry: dict[str, Any], include_timestamps: bool) -> str:
    role = (entry.get("role") or "unknown").lower()

    if role == "summary":
        archive_file = entry.get("archive_file", "")
        archive_suffix = f" | id: {_extract_short_id(archive_file)}" if archive_file else ""
        return (
            f"[SUMMARY covering {entry.get('covers_from', '?')} to {entry.get('covers_to', '?')}{archive_suffix}]\n"
            f"{entry.get('content', '').strip()}"
        ).strip()

    content = (entry.get("content") or "").strip()
    if not content:
        return ""

    prefix = ""
    if include_timestamps:
        timestamp = entry.get("timestamp")
        if timestamp:
            prefix = f"[{format_timestamp(timestamp)}] "

    return f"{prefix}{role.upper()}: {content}"


def maybe_compact_history(config: dict[str, Any] | None = None) -> None:
    config = config or load_config()
    entries = read_history()
    conversation_entries = [entry for entry in entries if entry.get("role") != "summary"]
    total_chars = sum(len((entry.get("content") or "").strip()) for entry in conversation_entries)
    threshold = int(config.get("compact_threshold_chars", DEFAULT_CONFIG["compact_threshold_chars"]))
    chunk_target = int(config.get("archive_chunk_chars", DEFAULT_CONFIG["archive_chunk_chars"]))

    if total_chars <= threshold:
        return

    while total_chars > threshold:
        start_index = next((index for index, entry in enumerate(entries) if entry.get("role") != "summary"), None)
        if start_index is None:
            return

        chunk_entries: list[dict[str, Any]] = []
        chunk_size = 0
        end_index = start_index

        while end_index < len(entries):
            entry = entries[end_index]
            if entry.get("role") == "summary":
                break
            chunk_entries.append(entry)
            chunk_size += len((entry.get("content") or "").strip())
            end_index += 1
            if chunk_size >= chunk_target and len(chunk_entries) >= 4:
                break

        if len(chunk_entries) < 2:
            return

        archive_name = archive_chunk(chunk_entries)
        summary_entry = {
            "role": "summary",
            "summary_type": "temporary",
            "content": summarize_entries(chunk_entries),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "covers_from": chunk_entries[0].get("timestamp", "?"),
            "covers_to": chunk_entries[-1].get("timestamp", "?"),
            "archive_file": archive_name,
            "entry_count": len(chunk_entries),
        }

        entries = entries[:start_index] + [summary_entry] + entries[end_index:]
        rewrite_history(entries)

        conversation_entries = [entry for entry in entries if entry.get("role") != "summary"]
        total_chars = sum(len((entry.get("content") or "").strip()) for entry in conversation_entries)


def archive_chunk(entries: list[dict[str, Any]]) -> str:
    ensure_state_dir()
    first_ts = safe_date_stamp(entries[0].get("timestamp", "unknown"))
    last_ts = safe_date_stamp(entries[-1].get("timestamp", "unknown"))
    digest = hashlib.sha256(json.dumps(entries, sort_keys=True).encode("utf-8")).hexdigest()[:8]
    name = f"temp_{first_ts}_to_{last_ts}_{digest}.jsonl"
    path = ARCHIVES_DIR / name
    with path.open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return name


def summarize_entries(entries: list[dict[str, Any]]) -> str:
    lines = [
        f"Compacted {len(entries)} conversation entries.",
    ]
    for entry in entries[:16]:
        role = str(entry.get("role", "unknown")).upper()
        content = " ".join(str(entry.get("content", "")).split())
        if len(content) > 220:
            content = content[:217] + "..."
        lines.append(f"- {role}: {content}")
    if len(entries) > 16:
        lines.append(f"- ... plus {len(entries) - 16} more entries in the archive")
    return "\n".join(lines)


def fetch_calendar_section(days: int = 14, timeout: int = 8) -> str:
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
        events = json.loads(result.stdout)
    except json.JSONDecodeError:
        return ""

    if not isinstance(events, list) or not events:
        return ""

    lines = [
        "=== UPCOMING CALENDAR ===",
        "",
    ]
    for event in events[:10]:
        start = event.get("start", {}).get("dateTime") or event.get("start", {}).get("date") or "?"
        summary = event.get("summary", "Untitled")
        lines.append(f"- {start}: {summary}")
    return "\n".join(lines)


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
