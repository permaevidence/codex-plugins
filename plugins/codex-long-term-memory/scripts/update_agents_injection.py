#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.common import (
    load_config,
    refresh_agents_memory_injection,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Write long-term memory into AGENTS.md for fresh Codex sessions.")
    parser.add_argument("--cwd", default=str(Path.home()), help="Codex working directory whose AGENTS.md should be updated.")
    args = parser.parse_args()

    config = load_config()
    result = refresh_agents_memory_injection(config, args.cwd)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
