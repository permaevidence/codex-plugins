#!/usr/bin/env python3
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.common import (
    append_history_entry,
    build_compaction_policy,
    build_injected_context,
    consume_compaction_reinjection,
    first_present,
    load_config,
    load_hook_input,
    read_history,
    should_reinject_after_compaction,
    uses_agents_md_injection,
)


def main() -> None:
    payload = load_hook_input()
    config = load_config()
    prompt = first_present(payload, "user_prompt", "userPrompt", "prompt")
    injected_context = ""
    agents_md_mode = uses_agents_md_injection(config)
    needs_reinjection = False if agents_md_mode else should_reinject_after_compaction(payload)

    if needs_reinjection:
        injected_context = build_injected_context(read_history(), config)

    if isinstance(prompt, str):
        append_history_entry("user", prompt, payload)

    now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    context_parts = [f"[now: {now}]"]

    if needs_reinjection and consume_compaction_reinjection(payload):
        context_parts.extend(["", build_compaction_policy()])
        if injected_context:
            context_parts.extend(["", injected_context])

    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "\n".join(context_parts),
        }
    }
    print(json.dumps(output))


if __name__ == "__main__":
    main()
