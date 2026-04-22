# Codex Long-Term Memory

This plugin ports the core idea of `Claude-Code-Long-Term-Memory` to Codex hooks.

## What it does

- Logs every submitted user prompt
- Logs every final assistant reply
- Captures file attachments and assistant file/tool references when transcript data is available
- Injects recent cross-thread history at `SessionStart`
- Extracts durable user facts from earlier conversations
- Optionally injects upcoming calendar items via `gws`
- Compacts older history into archive-backed summaries with temporary, consolidated, and meta tiers
- Stores everything under `~/.codex/long-term-memory/`
- Merges its hook registrations into `~/.codex/hooks.json`

## Why this differs from the Claude version

Codex already has a native Memories feature, but it is separate from hook-based full-history injection. This plugin focuses on explicit cross-thread recall using hook output.

This implementation now mirrors the Claude plugin much more closely: durable user facts, calendar injection, hierarchical summary tiers, raw archives, and transcript-driven file capture are all in place. The main remaining difference is that summarization/file descriptions are deterministic and local by default rather than model-generated.

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
- `~/.codex/long-term-memory/files/`
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
  "enable_attachment_capture": true,
  "compact_threshold_chars": 120000,
  "archive_chunk_chars": 40000,
  "temp_summaries_per_consolidation": 4,
  "max_visible_consolidated": 5,
  "meta_permanent_threshold": 5
}
```

## Compaction tiers

- Raw history: newest messages and file entries remain verbatim.
- Temporary summaries: oldest raw chunks are archived into `temp_*` files and replaced with compact summaries.
- Consolidated summaries: older temporary chunks are merged into `cons_*` archive files and re-expressed as one summary.
- Meta summaries: overflow consolidated summaries are folded into meta summaries with source-archive references.

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
