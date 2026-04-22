# Codex Telegram Bridge

This plugin ports the Telegram control loop from `claude-telegram-plugin` to Codex by talking to `codex app-server` directly.

## What it does

- Polls the Telegram Bot API with long polling
- Maps each Telegram chat to a persistent Codex thread
- Starts, steers, and interrupts turns through `codex app-server`
- Auto-approves command, file-change, and permission approval requests
- Auto-answers structured `item/tool/requestUserInput` prompts with default options when possible
- Supports `/help`, `/status`, `/stop`, and `/newsession`
- Supports DM pairing, allowlists, groups, mention policies, and mention regexes
- Transcribes voice messages with OpenAI `gpt-4o-transcribe`
- Forwards inbound photos as downloaded local paths and other Telegram attachments as downloadable file IDs
- Injects due reminders from `~/.codex/telegram-bridge/scheduled_reminders.json`
- Injects unread-email summaries via `gws gmail +triage`
- Monitors on-disk Codex CLI version changes and notifies the owner chat
- Supports delivery controls such as ack reactions, chunking, and reply threading
- Bundles Telegram MCP action tools so Codex can send replies and attachments, download inbound files, edit progress messages, and react mid-turn

## Configuration

State lives under `~/.codex/telegram-bridge/`:

- `config.json`
- `access.json`
- `chat-map.json`
- `scheduled_reminders.json`
- `email_state.json`
- `version_state.json`
- `runtime_state.json`
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
  "sandbox_mode": "dangerFullAccess",
  "network_access": true,
  "writable_roots": [],
  "owner_chat_id": "123456789",
  "openai_api_key": "sk-...",
  "enable_voice_transcription": true,
  "send_queue_confirmation": false,
  "enable_reminders": true,
  "enable_email_notifications": false
}
```

By default the bridge does not post the per-message `Sent to Codex...` acknowledgement before the actual reply arrives. Set `"send_queue_confirmation": true` if you want that extra queue-status message back.

You can also place `TELEGRAM_BOT_TOKEN=...` and `OPENAI_API_KEY=...` in `~/.codex/telegram-bridge/.env`.

If you change `config.json`, restart the bridge so the new settings take effect.

## Sandbox and Network Modes

The bridge passes its sandbox settings straight through to `codex app-server`.

Current repo default:

```json
{
  "sandbox_mode": "dangerFullAccess",
  "network_access": true
}
```

That matches the broad remote-control setup we used on this machine.

Safer alternative:

```json
{
  "sandbox_mode": "workspaceWrite",
  "network_access": false,
  "writable_roots": [
    "/absolute/path/to/your/project"
  ]
}
```

## Risks

Be careful with the default combination of `dangerFullAccess`, `network_access: true`, and `approval_policy: "never"`.

That setup means a Telegram-triggered Codex turn can:

- modify any file your local user can modify
- read any file your local user can read
- run local commands without an extra approval step
- make outbound network requests
- use any configured credentials reachable from the local environment

Only use that mode if you trust the Telegram account(s) that can reach the bot and you are comfortable treating Telegram as a fully privileged remote control for your Codex machine.

If you want narrower blast radius, switch back to `workspaceWrite`, restrict `writable_roots`, and disable `network_access` unless it is actually needed.

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

## Telegram action tools

The plugin bundles a local MCP server from [`.mcp.json`](./.mcp.json). After install, Codex can use:

- `reply`
- `edit_message`
- `react`
- `download_attachment`

These tools default to the currently active Telegram chat tracked by the bridge, so Codex can post progress updates during a long-running task instead of waiting only for the final turn completion. `reply` also accepts file paths for attachments, and inbound `<channel ...>` messages may include `image_path` or `attachment_file_id` metadata.

## Voice Transcription

Voice transcription uses OpenAI `gpt-4o-transcribe`. Telegram voice notes often arrive as `.oga` / Ogg Opus files, so the bridge converts them to `.mp3` with `ffmpeg` before sending them to the transcription API.

Install `ffmpeg` on the machine running the bridge if you want Telegram voice-note transcription to work reliably.

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

Telegram-side parity is now functionally closed for the Claude plugin's remote-control workflow.
