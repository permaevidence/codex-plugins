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
FILES_DIR = STATE_DIR / "files"

DEFAULT_CONFIG = {
    "max_injection_chars": 200000,
    "max_entries": 400,
    "include_timestamps": True,
    "enable_user_facts": True,
    "enable_calendar": True,
    "enable_attachment_capture": True,
    "compact_threshold_chars": 120000,
    "archive_chunk_chars": 40000,
    "temp_summaries_per_consolidation": 4,
    "max_visible_consolidated": 5,
    "meta_permanent_threshold": 5,
}


def ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVES_DIR.mkdir(parents=True, exist_ok=True)
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    FILES_DIR.mkdir(parents=True, exist_ok=True)


def data_items() -> list[tuple[Path, str]]:
    return [
        (HISTORY_FILE, "file"),
        (FACTS_FILE, "file"),
        (ARCHIVES_DIR, "dir"),
        (FILES_DIR, "dir"),
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
        for entry in normalized:
            if entry.get("role") == "user":
                append_user_facts(extract_user_facts(str(entry.get("content", ""))))

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
        chunk_entries: list[dict[str, Any]] = []
        chunk_size = 0
        while conversation:
            entry = conversation.pop(0)
            chunk_entries.append(entry)
            chunk_size += entry_content_size(entry)
            if chunk_size >= chunk_target and len(chunk_entries) >= 4:
                break

        if len(chunk_entries) < 2:
            conversation = chunk_entries + conversation
            break

        archive_name = archive_temp_chunk(chunk_entries)
        temporary.append(
            {
                "role": "summary",
                "summary_type": "temporary",
                "content": summarize_entries(chunk_entries, label="temporary"),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "covers_from": chunk_entries[0].get("timestamp", "?"),
                "covers_to": chunk_entries[-1].get("timestamp", "?"),
                "archive_file": archive_name,
                "entry_count": len(chunk_entries),
            }
        )

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
            cons_archive = archive_consolidated_chunk(raw_entries, to_consolidate)
            consolidated.append(
                {
                    "role": "summary",
                    "summary_type": "consolidated",
                    "content": summarize_entries(raw_entries, label="consolidated"),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "covers_from": to_consolidate[0].get("covers_from", ""),
                    "covers_to": to_consolidate[-1].get("covers_to", ""),
                    "archive_file": cons_archive,
                    "source_archives": archive_files_to_delete,
                }
            )
            for archive_name in archive_files_to_delete:
                try:
                    (ARCHIVES_DIR / archive_name).unlink(missing_ok=True)
                except Exception:
                    pass
        else:
            consolidated.append(
                {
                    "role": "summary",
                    "summary_type": "consolidated",
                    "content": summarize_summary_entries(to_consolidate, label="consolidated"),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "covers_from": to_consolidate[0].get("covers_from", ""),
                    "covers_to": to_consolidate[-1].get("covers_to", ""),
                    "source_archives": archive_files_to_delete,
                }
            )
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
        summary_content = summarize_summary_entries(combined_entries, label="meta")

        new_meta = {
            "role": "summary",
            "summary_type": "meta_permanent" if len(source_archives) >= meta_perm_threshold else "meta_temporary",
            "content": summary_content,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "covers_from": covers_from,
            "covers_to": covers_to,
            "source_archives": [archive for archive in source_archives if archive],
        }
        meta_temp = []
        if new_meta["summary_type"] == "meta_permanent":
            meta_perm.append(new_meta)
        else:
            meta_temp = [new_meta]
        consolidated = to_keep

    rewrite_history(meta_perm + consolidated + meta_temp + temporary + conversation)


def archive_temp_chunk(entries: list[dict[str, Any]]) -> str:
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


def archive_consolidated_chunk(raw_entries: list[dict[str, Any]], temp_summaries: list[dict[str, Any]]) -> str:
    ensure_state_dir()
    first_ts = safe_date_stamp(temp_summaries[0].get("covers_from", "unknown"))
    last_ts = safe_date_stamp(temp_summaries[-1].get("covers_to", "unknown"))
    digest = hashlib.sha256(json.dumps(temp_summaries, sort_keys=True).encode("utf-8")).hexdigest()[:8]
    name = f"cons_{first_ts}_to_{last_ts}_{digest}.jsonl"
    path = ARCHIVES_DIR / name
    header = {
        "_consolidated": True,
        "summary_type": "consolidated",
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
            if obj.get("_consolidated"):
                continue
            entries.append(obj)
    return entries


def summarize_entries(entries: list[dict[str, Any]], label: str = "temporary") -> str:
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


def summarize_summary_entries(entries: list[dict[str, Any]], label: str = "meta") -> str:
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
