---
name: telegram-admin
description: Configure, inspect, and operate the local Codex Telegram bridge.
---

Use this skill when the user wants to set up or troubleshoot the Codex Telegram bridge.

## Responsibilities

1. Edit `~/.codex/telegram-bridge/config.json` carefully.
2. Manage access with `scripts/access.py`.
3. Start or inspect the bridge process with `scripts/telegram_bridge.py`.
4. Preserve existing allowlists and pending pairing codes unless the user asks otherwise.

## Files

- Plugin root: `plugins/codex-telegram-bridge`
- Bridge: `scripts/telegram_bridge.py`
- Access CLI: `scripts/access.py`
- State dir: `~/.codex/telegram-bridge`

## Guardrails

- Never leak the Telegram bot token in plain text.
- Keep DM access locked down unless the user explicitly wants open pairing.
- If changing `default_cwd` or sandbox settings, explain the security implications.

