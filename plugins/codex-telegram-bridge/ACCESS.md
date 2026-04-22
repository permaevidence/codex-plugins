# Telegram Access And Delivery

The bridge reads `~/.codex/telegram-bridge/access.json` on every inbound message, so access changes take effect without restarting the bot.

## Defaults

```json
{
  "dmPolicy": "pairing",
  "allowFrom": [],
  "groups": {},
  "mentionPatterns": [],
  "ackReaction": "",
  "replyToMode": "first",
  "textChunkLimit": 3900,
  "chunkMode": "newline",
  "pending": {}
}
```

## DM policies

- `pairing`: unknown DMs get a pairing code and are dropped until approved
- `allowlist`: unknown DMs are dropped silently
- `disabled`: drop all inbound messages, including groups

## Groups

Enable a group explicitly:

```bash
python3 /absolute/path/to/plugins/codex-telegram-bridge/scripts/access.py group-add -1001654782309
```

Flags:

- `--no-mention`: process every message from the group
- `--allow 123,456`: restrict triggers to specific sender IDs

With the default `requireMention: true`, the bridge accepts:

- structured `@botusername` mentions
- replies to one of the bot's messages
- regex matches from `mentionPatterns`

## Delivery controls

- `ackReaction`: emoji reaction applied to inbound messages when processing starts
- `replyToMode`: `first`, `all`, or `off`
- `textChunkLimit`: max outbound chunk length, capped to Telegram-safe limits
- `chunkMode`: `newline` or `length`

Examples:

```bash
python3 /absolute/path/to/plugins/codex-telegram-bridge/scripts/access.py set ackReaction 👀
python3 /absolute/path/to/plugins/codex-telegram-bridge/scripts/access.py set replyToMode off
python3 /absolute/path/to/plugins/codex-telegram-bridge/scripts/access.py set textChunkLimit 3200
python3 /absolute/path/to/plugins/codex-telegram-bridge/scripts/access.py set chunkMode length
python3 /absolute/path/to/plugins/codex-telegram-bridge/scripts/access.py set mentionPatterns '["^hey codex\\\\b","\\\\bassistant\\\\b"]'
```

## CLI reference

- `show`
- `allow <user_id>`
- `remove <user_id>`
- `policy pairing|allowlist|disabled`
- `pair <code>`
- `deny <code>`
- `group-add <group_id> [--no-mention] [--allow 1,2,3]`
- `group-rm <group_id>`
- `set-mentions <regex1> <regex2> ...`
- `set ackReaction|replyToMode|textChunkLimit|chunkMode|mentionPatterns <value>`
