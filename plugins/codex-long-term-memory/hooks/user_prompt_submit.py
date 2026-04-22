#!/usr/bin/env python3
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.common import append_history_entry, first_present, load_hook_input


def main() -> None:
    payload = load_hook_input()
    prompt = first_present(payload, "user_prompt", "userPrompt", "prompt")
    if isinstance(prompt, str):
        append_history_entry("user", prompt, payload)

    now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": f"[Current time: {now}]",
        }
    }
    print(json.dumps(output))


if __name__ == "__main__":
    main()
