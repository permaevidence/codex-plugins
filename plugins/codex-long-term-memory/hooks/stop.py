#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.common import append_history_entry, empty_success, first_present, load_hook_input


def main() -> None:
    payload = load_hook_input()
    message = first_present(
        payload,
        "last_assistant_message",
        "lastAssistantMessage",
        "assistant_message",
        "assistantMessage",
    )
    if isinstance(message, str):
        append_history_entry("assistant", message, payload)
    empty_success()


if __name__ == "__main__":
    main()
