#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.common import (
    build_compaction_policy,
    build_injected_context,
    load_config,
    print_session_start_context,
    read_history,
)


def main() -> None:
    config = load_config()
    history = read_history()
    parts = [build_compaction_policy()]
    context = build_injected_context(history, config)
    if context:
        parts.extend(["", context])
    context = "\n".join(parts)
    print_session_start_context(context)


if __name__ == "__main__":
    main()
