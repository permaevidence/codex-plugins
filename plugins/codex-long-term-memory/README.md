# Codex Long-Term Memory

This plugin ports the core idea of `Claude-Code-Long-Term-Memory` to Codex hooks.

## What it does

- Logs every submitted user prompt
- Logs every final assistant reply
- Injects recent cross-thread history at `SessionStart`
- Extracts durable user facts from earlier conversations
- Optionally injects upcoming calendar items via `gws`
- Compacts older history into archive-backed summaries
- Stores everything under `~/.codex/long-term-memory/`
- Merges its hook registrations into `~/.codex/hooks.json`

## Why this differs from the Claude version

Codex already has a native Memories feature, but it is separate from hook-based full-history injection. This plugin focuses on explicit cross-thread recall using hook output.

This Codex-native implementation now keeps recent history inline, extracts durable user facts, and compacts older raw conversation into archive-backed summaries. It still differs from the Claude version in that compaction is deterministic/local today rather than multi-tier LLM summarization.

## Install

Run:

```bash
python3 /absolute/path/to/plugins/codex-long-term-memory/scripts/install.py
```

The installer:

- enables `features.codex_hooks = true` in `~/.codex/config.toml`
- registers the plugin hooks in `~/.codex/hooks.json`
- creates `~/.codex/long-term-memory/config.json` if missing

Restart Codex after installing.

## Files

- `~/.codex/long-term-memory/history.jsonl`
- `~/.codex/long-term-memory/user_facts.jsonl`
- `~/.codex/long-term-memory/archives/`
- `~/.codex/long-term-memory/backups/`
- `~/.codex/long-term-memory/config.json`

Default config:

```json
{
  "max_injection_chars": 200000,
  "max_entries": 400,
  "include_timestamps": true,
  "enable_user_facts": true,
  "enable_calendar": true,
  "compact_threshold_chars": 120000,
  "archive_chunk_chars": 40000
}
```

## Data management

Create a backup:

```bash
python3 /absolute/path/to/plugins/codex-long-term-memory/scripts/backup.py
```

Reset memory data after making a backup:

```bash
python3 /absolute/path/to/plugins/codex-long-term-memory/scripts/reset.py
```

List backups or restore one:

```bash
python3 /absolute/path/to/plugins/codex-long-term-memory/scripts/restore.py
python3 /absolute/path/to/plugins/codex-long-term-memory/scripts/restore.py <backup_name>
```

## Uninstall

```bash
python3 /absolute/path/to/plugins/codex-long-term-memory/scripts/uninstall.py
```

This removes only this plugin's hook entries. It does not delete your saved history.
