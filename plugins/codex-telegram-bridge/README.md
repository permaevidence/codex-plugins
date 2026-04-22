# Codex Telegram Bridge

This plugin ports the Telegram control loop from `claude-telegram-plugin` to Codex by talking to `codex app-server` directly.

## What it does

- Polls the Telegram Bot API with long polling
- Maps each Telegram chat to a persistent Codex thread
- Starts, steers, and interrupts turns through `codex app-server`
- Auto-approves command, file-change, and permission approval requests
- Handles unsupported `item/tool/requestUserInput` prompts gracefully
- Supports `/help`, `/status`, `/stop`, and `/newsession`
- Supports DM pairing, allowlists, groups, mention policies, and mention regexes
- Transcribes voice messages with OpenAI `gpt-4o-transcribe`
- Injects due reminders from `~/.codex/telegram-bridge/scheduled_reminders.json`
- Injects unread-email summaries via `gws gmail +triage`
- Monitors on-disk Codex CLI version changes and notifies the owner chat
- Supports delivery controls such as ack reactions, chunking, and reply threading

## Configuration

State lives under `~/.codex/telegram-bridge/`:

- `config.json`
- `access.json`
- `chat-map.json`
- `scheduled_reminders.json`
- `email_state.json`
- `version_state.json`
- `.env` (optional)

Example `config.json`:

```json
{
  "bot_token": "123456789:AA....",
  "default_cwd": "/absolute/path/to/your/project",
  "model": "gpt-5.4",
  "effort": "medium",
  "approval_policy": "never",
  "personality": "friendly",
  "sandbox_mode": "workspaceWrite",
  "network_access": false,
  "writable_roots": [],
  "owner_chat_id": "123456789",
  "openai_api_key": "sk-...",
  "enable_voice_transcription": true,
  "enable_reminders": true,
  "enable_email_notifications": false
}
```

You can also place `TELEGRAM_BOT_TOKEN=...` and `OPENAI_API_KEY=...` in `~/.codex/telegram-bridge/.env`.

## Running

```bash
python3 /absolute/path/to/plugins/codex-telegram-bridge/scripts/telegram_bridge.py
```

For unattended use, run it inside `tmux`, `screen`, `launchd`, or another supervisor.

This repo also includes a simple restart loop:

```bash
chmod +x /absolute/path/to/plugins/codex-telegram-bridge/scripts/start_bridge.sh
/absolute/path/to/plugins/codex-telegram-bridge/scripts/start_bridge.sh
```

It writes logs to `~/.codex/telegram-bridge/bridge.log` and tracks supervisor and child PIDs under the same state directory.

## Access control

Show current state:

```bash
python3 /absolute/path/to/plugins/codex-telegram-bridge/scripts/access.py show
```

Allow yourself directly:

```bash
python3 /absolute/path/to/plugins/codex-telegram-bridge/scripts/access.py allow 123456789
```

Approve a pairing code:

```bash
python3 /absolute/path/to/plugins/codex-telegram-bridge/scripts/access.py pair a1b2c3
```

Enable a group:

```bash
python3 /absolute/path/to/plugins/codex-telegram-bridge/scripts/access.py group-add -1001234567890
```

Configure delivery behavior:

```bash
python3 /absolute/path/to/plugins/codex-telegram-bridge/scripts/access.py set ackReaction 👀
python3 /absolute/path/to/plugins/codex-telegram-bridge/scripts/access.py set replyToMode all
python3 /absolute/path/to/plugins/codex-telegram-bridge/scripts/access.py set textChunkLimit 3500
python3 /absolute/path/to/plugins/codex-telegram-bridge/scripts/access.py set chunkMode newline
```

See [ACCESS.md](./ACCESS.md) for the full access and delivery model.

## Reminders

The bridge polls `scheduled_reminders.json` every 60 seconds. Example:

```json
[
  {
    "id": "check-build",
    "chat_id": "123456789",
    "due": "2026-04-22T18:30:00",
    "prompt": "Check whether the release build finished and message me with the result."
  },
  {
    "id": "daily-review",
    "chat_id": "123456789",
    "due": "2026-04-23T09:00:00",
    "prompt": "Review today's calendar and email priorities.",
    "recurring": "daily"
  }
]
```

## Differences from the Claude plugin

- Codex does not need the Claude wrapper-script tricks for hook-size patching or trust-dialog repair.
- `/status` is based on live app-server state plus recent chat metadata, rather than transcript scraping.
- The bridge is dependency-light Python instead of a channel plugin plus Bun runtime.

## Remaining gap

The Claude fork exposes Telegram-specific assistant tools for message edits and reactions during long tasks. This Codex port currently sends normal Telegram replies and optional automatic ack reactions, but it does not yet expose a tool surface for Codex itself to edit or react to messages mid-turn.
