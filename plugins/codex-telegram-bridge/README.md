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
- Auto-forwards newly generated images from `~/.codex/generated_images/<thread_id>/` when a turn completes
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

## Generated Images

When a Codex turn generates new image files under `~/.codex/generated_images/<thread_id>/`, the bridge now compares the directory state at turn start versus turn completion and automatically sends any newly created images back to the Telegram chat.

Behavior:

- text-only turn: sends the text reply as before
- image-only turn: sends the generated image without the old `Turn completed with no final assistant text.` fallback
- text plus images: sends the text reply and the generated images in the same Telegram response flow

The bridge only forwards images that were newly created during that specific turn, so older images from the same thread are not resent automatically.

## Tell Codex About Local Capabilities

The bridge can surface reminders, unread Gmail summaries, and Telegram runtime context, but Codex still works better if those local capabilities are described explicitly in `AGENTS.md`.

This matters because:

- reminder creation is file-based and expects exact JSON in `~/.codex/telegram-bridge/scheduled_reminders.json`
- the active Telegram chat id lives in bridge runtime state, not in a generic Codex API
- `gws` availability is machine-specific, so Codex should be told which Google account and services are actually configured

Recommended `AGENTS.md` additions:

~~~md
## Local Capabilities

### Telegram Reminders

- The Telegram bridge supports reminders through `~/.codex/telegram-bridge/scheduled_reminders.json`.
- This is a JSON array of reminder objects. Do not assume the bridge has a natural-language reminder parser.
- When creating or editing reminders, preserve valid JSON and keep existing reminder entries unless the user asked to replace or delete them.
- Each reminder should use this shape:

```json
{
  "id": "short-stable-id",
  "chat_id": "123456789",
  "due": "2026-04-22T18:30:00",
  "prompt": "Check whether the release build finished and message me with the result.",
  "recurring": "daily"
}
```

- Required fields: `id`, `chat_id`, `due`, `prompt`.
- `recurring` is optional. Supported values are only `daily`, `weekly`, and `monthly`.
- `due` must be `YYYY-MM-DDTHH:MM[:SS]` in local time.
- The reminder loop polls every 60 seconds, so reminders can fire up to about one minute late.
- For reminders meant for the current Telegram conversation, use the active chat id from `~/.codex/telegram-bridge/runtime_state.json` or `chat-map.json` if needed.

### Google Workspace CLI

- `gws` is installed on this machine and authenticated for `<account@example.com>`.
- Verified live access currently includes Gmail and Calendar.
- Treat `gws` as live account access. Use it only when relevant to the user's request, and summarize clearly what was read or changed.
- If a task depends on a specific Google Workspace capability beyond Gmail or Calendar, verify the exact `gws` command or schema before making assumptions.
~~~

In the current plugin split, this Telegram bridge injects reminders and unread Gmail summaries, while the companion long-term-memory plugin can inject calendar context through `gws`.

After editing `AGENTS.md`, start a new Codex session so those instructions are loaded.

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
bash /absolute/path/to/plugins/codex-telegram-bridge/scripts/start_bridge.sh
```

Use `start_bridge.sh` as the normal launch and restart path. It is a singleton supervisor:

- it writes `.bridge-supervisor.pid` and `.bridge-child.pid` under `~/.codex/telegram-bridge/`
- it cleans stale pid files on startup
- it refuses to start a second live supervisor
- it kills stale orphaned `telegram_bridge.py` children it finds before relaunching

Avoid launching `telegram_bridge.py` directly for normal use. Running the raw Python script alongside the supervisor can create duplicate Telegram long-pollers and produce `HTTP Error 409: Conflict` in `bridge.log`.

For unattended use, run the supervisor inside `tmux`, `screen`, `launchd`, or another process manager.

This repo also includes a simple restart loop:

```bash
chmod +x /absolute/path/to/plugins/codex-telegram-bridge/scripts/start_bridge.sh
bash /absolute/path/to/plugins/codex-telegram-bridge/scripts/start_bridge.sh
```

It writes logs to `~/.codex/telegram-bridge/bridge.log` and tracks supervisor and child PIDs under the same state directory.

If you are watching logs during startup, prefer:

```bash
tail -n 0 -f ~/.codex/telegram-bridge/bridge.log
```

That shows only new log lines from the current launch instead of mixing in older historical `409` entries.

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
