#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.common import (
    first_present,
    load_config,
    load_hook_input,
    refresh_agents_memory_injection,
    uses_agents_md_injection,
)


def main() -> None:
    payload = load_hook_input()
    config = load_config()

    if uses_agents_md_injection(config):
        cwd = first_present(payload, "cwd")
        refresh_agents_memory_injection(config, cwd if isinstance(cwd, str) else None)

    print(json.dumps({"continue": True, "suppressOutput": True}))


if __name__ == "__main__":
    main()
