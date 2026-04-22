# Codex Plugins

Codex-native ports of two Claude Code workflows:

- `codex-long-term-memory`: long-term cross-session memory built with Codex hooks
- `codex-telegram-bridge`: Telegram control of Codex through `codex app-server`

The Telegram bridge now defaults to broad remote-control mode for parity with the setup we validated locally. Read the plugin README before using it that way on another machine.

This repo is structured as a local Codex plugin marketplace so both plugins can be installed from one workspace.

## Install

For another Codex instance, the safest assumption is:

1. Clone this repo locally.
2. Open the cloned folder in Codex.
3. Use the local marketplace file at `.agents/plugins/marketplace.json` so Codex can see both plugins from one repo.
4. Install:
   - `codex-long-term-memory`
   - `codex-telegram-bridge`
5. Run the memory installer after install:

```bash
python3 /absolute/path/to/repo/plugins/codex-long-term-memory/scripts/install.py
```

6. Create and fill:
   - `~/.codex/telegram-bridge/config.json`
   - `~/.codex/telegram-bridge/access.json`
   - optionally `~/.codex/telegram-bridge/.env`
7. Start the Telegram bridge:

```bash
python3 /absolute/path/to/repo/plugins/codex-telegram-bridge/scripts/telegram_bridge.py
```

If you want Codex on the other machine to do this itself, giving it the repo URL should be enough only if it is also told to clone the repo locally and use the checked-out `.agents/plugins/marketplace.json`. The URL alone is not a complete install contract without those steps.

The repo intentionally ignores local runtime state under `.codex/`, so secrets, chat history, and live bridge state do not belong in git.

## Quick setup

Minimum secrets/config needed after cloning:

- Long-term memory:
  - optional `OPENAI_API_KEY` if you want model-backed summaries, captions, and fact extraction
- Telegram bridge:
  - Telegram bot token
  - `owner_chat_id` if you want owner-only notifications like email/version monitor
  - optional `OPENAI_API_KEY` for voice transcription
  - optional `ffmpeg` for Telegram voice-note transcription
  - optional `gws` for Gmail and calendar integrations
  - review the Telegram README before leaving `dangerFullAccess` and `network_access = true` enabled

## Included plugins

### `codex-long-term-memory`

- logs user prompts and assistant replies
- captures file attachments and assistant file references from transcript data
- injects recent history at session start
- extracts durable user facts
- optionally injects calendar context via `gws`
- compacts older history into temporary, consolidated, and meta archive-backed summaries
- uses the OpenAI Responses API for summaries, file captions, and richer user-fact extraction when configured
- retries failed summary refreshes from an on-disk pending queue
- includes backup, reset, and restore utilities

### `codex-telegram-bridge`

- maps Telegram chats to persistent Codex threads
- supports `/help`, `/status`, `/stop`, and `/newsession`
- supports DM pairing, allowlists, groups, and mention rules
- auto-approves unattended Codex approval requests
- transcribes voice messages with OpenAI audio transcription
- injects reminders and unread-email summaries
- forwards inbound attachment metadata and downloaded photo paths
- bundles Telegram MCP tools for replies, attachments, inbound file download, message edits, and reactions
- includes a simple restart-loop supervisor script
- can run in either safer `workspaceWrite` mode or broader `dangerFullAccess` mode depending on your remote-control needs

## Repo layout

- [.agents/plugins/marketplace.json](.agents/plugins/marketplace.json)
- [plugins/codex-long-term-memory/README.md](plugins/codex-long-term-memory/README.md)
- [plugins/codex-telegram-bridge/README.md](plugins/codex-telegram-bridge/README.md)
- [PARITY_ROADMAP.md](PARITY_ROADMAP.md)

## Notes

- The original Claude plugin repos were used as local reference material during the port and are intentionally ignored by git in this repo.
- Telegram parity and memory parity are now functionally closed relative to the original Claude plugins.

## References

- [Codex hooks](https://developers.openai.com/codex/hooks)
- [Codex app-server](https://developers.openai.com/codex/app-server)
- [Build Codex plugins](https://developers.openai.com/codex/plugins/build)
- [Codex memories](https://developers.openai.com/codex/memories)
