---
name: telegram-admin
description: Configure, inspect, and operate the local Codex Telegram bridge.
---

Use this skill when the user wants to set up or troubleshoot the Codex Telegram bridge.

## Responsibilities

1. Edit `~/.codex/telegram-bridge/config.json` carefully.
2. Manage access with `scripts/access.py`.
3. Start or inspect the bridge process with `scripts/telegram_bridge.py`.
4. Use the bundled Telegram MCP tools for delivery behavior when the user is actively interacting through Telegram.
5. Preserve existing allowlists and pending pairing codes unless the user asks otherwise.

## Files

- Plugin root: `plugins/codex-telegram-bridge`
- Bridge: `scripts/telegram_bridge.py`
- Telegram MCP actions: `scripts/telegram_actions_mcp.py`
- Access CLI: `scripts/access.py`
- State dir: `~/.codex/telegram-bridge`

## Delivery tools

- `reply`: send a Telegram message to the active chat
- `edit_message`: edit the latest outbound Telegram message, useful for progress updates
- `react`: add an emoji reaction to the latest inbound or outbound Telegram message

Prefer these tools when the user would benefit from intermediate updates during a long task.

## Guardrails

- Never leak the Telegram bot token in plain text.
- Keep DM access locked down unless the user explicitly wants open pairing.
- If changing `default_cwd` or sandbox settings, explain the security implications.
