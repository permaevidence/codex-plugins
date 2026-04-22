# Codex Plugins

Codex-native ports of two Claude Code workflows:

- `codex-long-term-memory`: long-term cross-session memory built with Codex hooks
- `codex-telegram-bridge`: Telegram control of Codex through `codex app-server`

This repo is structured as a local Codex plugin marketplace so both plugins can be installed from one workspace.

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
