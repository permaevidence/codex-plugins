#!/usr/bin/env python3
import json
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
HOOKS_FILE = Path.home() / ".codex" / "hooks.json"


def is_our_group(group: dict) -> bool:
    for hook in group.get("hooks", []):
        command = str(hook.get("command", ""))
        if str(PLUGIN_ROOT / "hooks") in command:
            return True
    return False


def main() -> None:
    if not HOOKS_FILE.exists():
        print(f"No hooks file found at {HOOKS_FILE}.")
        return

    payload = json.loads(HOOKS_FILE.read_text(encoding="utf-8"))
    hooks = payload.get("hooks", {})

    for event_name in list(hooks.keys()):
        kept = [group for group in hooks.get(event_name, []) if not is_our_group(group)]
        if kept:
            hooks[event_name] = kept
        else:
            hooks.pop(event_name, None)

    payload["hooks"] = hooks
    HOOKS_FILE.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    print("Removed codex-long-term-memory hook entries.")
    print("Saved history in ~/.codex/long-term-memory/ was left untouched.")


if __name__ == "__main__":
    main()

