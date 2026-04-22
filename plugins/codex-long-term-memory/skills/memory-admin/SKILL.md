---
name: memory-admin
description: Install, inspect, and manage the Codex long-term memory hook plugin in ~/.codex.
---

Use this skill when the user wants to set up, inspect, tune, or remove the Codex long-term memory plugin.

## Responsibilities

1. Install hooks by running `scripts/install.py`.
2. Inspect `~/.codex/long-term-memory/` files.
3. Tune `config.json` conservatively.
4. Remove hooks with `scripts/uninstall.py` if asked.

## Files

- Plugin root: `plugins/codex-long-term-memory`
- Installer: `scripts/install.py`
- Uninstaller: `scripts/uninstall.py`
- State dir: `~/.codex/long-term-memory`
- Hook registry: `~/.codex/hooks.json`

## Guardrails

- Preserve unrelated hook entries already in `~/.codex/hooks.json`.
- Do not delete saved history unless the user explicitly asks.
- If changing config, keep `max_injection_chars` at a realistic level for the user's model and workflow.

