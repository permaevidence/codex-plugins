#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import shlex
import shutil
import sys
import tempfile
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PYTHON = shutil.which("python3") or sys.executable or "/usr/bin/python3"
CODEX_DIR = Path.home() / ".codex"
HOOKS_FILE = CODEX_DIR / "hooks.json"
CONFIG_TOML = CODEX_DIR / "config.toml"
STATE_DIR = CODEX_DIR / "long-term-memory"
STATE_CONFIG = STATE_DIR / "config.json"

DEFAULT_STATE_CONFIG = {
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
    "model_file_max_bytes": 8388608,
    "openai_api_key": "",
    "openai_api_key_env": "OPENAI_API_KEY",
    "openai_base_url": "https://api.openai.com/v1/responses",
    "openai_model": "gpt-5.6-luna",
    "openai_reasoning_effort": "high",
    "openai_timeout_seconds": 240,
    "minimum_model_summary_words": 100,
    "summary_max_chars": 10000,
    "pending_retry_enabled": True,
    "pending_retry_base_seconds": 30,
    "pending_retry_max_seconds": 480,
    "injection_transport": "hook",
    "agents_md_path": "",
    "agents_project_doc_max_bytes": 524288,
}


def hook_group(
    script_name: str,
    event_name: str,
    matcher: str | None = None,
    status: str | None = None,
    timeout: int = 30,
) -> dict:
    group: dict[str, object] = {
        "hooks": [
            {
                "type": "command",
                # shlex.quote both parts: Codex parses this string with POSIX
                # shell rules, and the runtime now lives under
                # "~/Library/Application Support/…" — an unquoted space there
                # makes every hook fail with exit code 2 (and a failing
                # UserPromptSubmit hook blocks the prompt entirely).
                "command": f"{shlex.quote(PYTHON)} {shlex.quote(str(PLUGIN_ROOT / 'hooks' / script_name))}",
                "timeout": timeout,
            }
        ]
    }
    if matcher is not None:
        group["matcher"] = matcher
    if status is not None:
        group["hooks"][0]["statusMessage"] = status
    return group


OUR_HOOKS = {
    "SessionStart": [
        hook_group("session_start.py", "SessionStart", "startup|resume|clear", "Loading long-term memory"),
    ],
    "UserPromptSubmit": [
        hook_group("user_prompt_submit.py", "UserPromptSubmit"),
    ],
    "PreCompact": [
        hook_group("pre_compact.py", "PreCompact", "manual|auto", "Refreshing long-term memory", timeout=120),
    ],
    "Stop": [
        hook_group("stop.py", "Stop", timeout=120),
    ],
}


def set_hooks_feature_enabled(text: str) -> str:
    features_match = re.search(r"(?m)^\[features\]\s*$", text)
    if not features_match:
        return text.rstrip() + "\n\n[features]\nhooks = true\n"

    section_start = features_match.end()
    next_section = re.search(r"(?m)^\[[^\]]+\]\s*$", text[section_start:])
    section_end = section_start + next_section.start() if next_section else len(text)

    prefix = text[:section_start]
    section = text[section_start:section_end]
    suffix = text[section_end:]

    section = re.sub(r"(?m)^\s*codex_hooks\s*=.*\n?", "", section)
    if re.search(r"(?m)^\s*hooks\s*=", section):
        section = re.sub(r"(?m)^(\s*hooks\s*=\s*).*$", r"\1true", section, count=1)
    else:
        section = "\nhooks = true" + section

    return (prefix + section + suffix).rstrip() + "\n"


def load_hooks() -> dict:
    if not HOOKS_FILE.exists():
        return {"hooks": {}}
    try:
        return json.loads(HOOKS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Could not parse {HOOKS_FILE}: {exc}")


def save_hooks(payload: dict) -> None:
    HOOKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(HOOKS_FILE, json.dumps(payload, indent=2) + "\n")


def is_our_group(group: dict) -> bool:
    for hook in group.get("hooks", []):
        command = str(hook.get("command", ""))
        if str(PLUGIN_ROOT / "hooks") in command or "codex-long-term-memory/hooks/" in command:
            return True
    return False


def merge_hooks() -> None:
    payload = load_hooks()
    hooks = payload.setdefault("hooks", {})

    for event_name, groups in OUR_HOOKS.items():
        existing = hooks.get(event_name, [])
        existing = [group for group in existing if not is_our_group(group)]
        existing.extend(groups)
        hooks[event_name] = existing

    save_hooks(payload)


def ensure_hooks_feature_enabled() -> None:
    CONFIG_TOML.parent.mkdir(parents=True, exist_ok=True)
    if not CONFIG_TOML.exists():
        atomic_write_text(CONFIG_TOML, "[features]\nhooks = true\n")
        return

    text = CONFIG_TOML.read_text(encoding="utf-8")
    atomic_write_text(CONFIG_TOML, set_hooks_feature_enabled(text))


def ensure_state_files() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not STATE_CONFIG.exists():
        atomic_write_text(STATE_CONFIG, json.dumps(DEFAULT_STATE_CONFIG, indent=2) + "\n")


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temp_path = Path(raw_path)
    try:
        with open(descriptor, "w", encoding="utf-8", closefd=True) as handle:
            handle.write(text)
        if path.exists():
            shutil.copymode(path, temp_path)
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)


def main() -> None:
    ensure_hooks_feature_enabled()
    merge_hooks()
    ensure_state_files()

    print("Installed codex-long-term-memory.")
    print(f"- Hook registry: {HOOKS_FILE}")
    print(f"- Config:        {CONFIG_TOML}")
    print(f"- State dir:     {STATE_DIR}")
    print("Restart Codex to load the new hooks.")


if __name__ == "__main__":
    main()
