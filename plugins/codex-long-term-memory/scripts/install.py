#!/usr/bin/env python3
import json
import re
import shutil
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PYTHON = shutil.which("python3") or sys.executable or "/usr/bin/python3"
CODEX_DIR = Path.home() / ".codex"
HOOKS_FILE = CODEX_DIR / "hooks.json"
CONFIG_TOML = CODEX_DIR / "config.toml"
STATE_DIR = CODEX_DIR / "long-term-memory"
STATE_CONFIG = STATE_DIR / "config.json"

DEFAULT_STATE_CONFIG = {
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
    "enable_model_summaries": True,
    "enable_model_file_descriptions": True,
    "enable_model_user_facts": True,
    "user_facts_max_chars": 16000,
    "model_file_max_bytes": 8388608,
    "openai_api_key": "",
    "openai_api_key_env": "OPENAI_API_KEY",
    "openai_base_url": "https://api.openai.com/v1/responses",
    "openai_model": "gpt-5-mini",
    "openai_timeout_seconds": 45,
    "pending_retry_enabled": True,
    "pending_retry_base_seconds": 30,
    "pending_retry_max_seconds": 480,
}


def hook_group(script_name: str, event_name: str, matcher: str | None = None, status: str | None = None) -> dict:
    group: dict[str, object] = {
        "hooks": [
            {
                "type": "command",
                "command": f"{PYTHON} {PLUGIN_ROOT / 'hooks' / script_name}",
                "timeout": 30,
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
        hook_group("session_start.py", "SessionStart", "startup|resume", "Loading long-term memory"),
    ],
    "UserPromptSubmit": [
        hook_group("user_prompt_submit.py", "UserPromptSubmit"),
    ],
    "Stop": [
        hook_group("stop.py", "Stop"),
    ],
}


def load_hooks() -> dict:
    if not HOOKS_FILE.exists():
        return {"hooks": {}}
    try:
        return json.loads(HOOKS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Could not parse {HOOKS_FILE}: {exc}")


def save_hooks(payload: dict) -> None:
    HOOKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    HOOKS_FILE.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def is_our_group(group: dict) -> bool:
    for hook in group.get("hooks", []):
        command = str(hook.get("command", ""))
        if str(PLUGIN_ROOT / "hooks") in command:
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
        CONFIG_TOML.write_text("[features]\ncodex_hooks = true\n", encoding="utf-8")
        return

    text = CONFIG_TOML.read_text(encoding="utf-8")
    if re.search(r"(?m)^\s*codex_hooks\s*=", text):
        updated = re.sub(r"(?m)^(\s*codex_hooks\s*=\s*).*$", r"\1true", text, count=1)
        CONFIG_TOML.write_text(updated, encoding="utf-8")
        return

    features_match = re.search(r"(?m)^\[features\]\s*$", text)
    if features_match:
        insert_at = features_match.end()
        updated = text[:insert_at] + "\ncodex_hooks = true" + text[insert_at:]
        CONFIG_TOML.write_text(updated.rstrip() + "\n", encoding="utf-8")
        return

    updated = text.rstrip() + "\n\n[features]\ncodex_hooks = true\n"
    CONFIG_TOML.write_text(updated, encoding="utf-8")


def ensure_state_files() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not STATE_CONFIG.exists():
        STATE_CONFIG.write_text(json.dumps(DEFAULT_STATE_CONFIG, indent=2) + "\n", encoding="utf-8")


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
