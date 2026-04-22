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
- Uses the OpenAI Responses API for summaries, user-fact extraction, and file descriptions when configured
- Queues failed summary refreshes under `~/.codex/long-term-memory/pending/` and retries them in the background
- Stores everything under `~/.codex/long-term-memory/`
- Merges its hook registrations into `~/.codex/hooks.json`

## Why this differs from the Claude version

Codex already has a native Memories feature, but it is separate from hook-based full-history injection. This plugin focuses on explicit cross-thread recall using hook output.

This implementation now mirrors the Claude plugin much more closely: durable user facts, richer calendar injection, hierarchical summary tiers, raw archives, transcript-driven file capture, model-backed summaries, and model-backed file descriptions are all in place. If the OpenAI API is unavailable, the plugin falls back to deterministic local summaries and descriptions so memory capture still works.

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
- `~/.codex/long-term-memory/pending/`
- `~/.codex/long-term-memory/config.json`

Default config:

```json
{
  "max_injection_chars": 200000,
  "include_timestamps": true,
  "enable_user_facts": true,
  "enable_calendar": true,
  "enable_attachment_capture": true,
  "compact_threshold_chars": 120000,
  "archive_chunk_chars": 40000,
  "temp_summaries_per_consolidation": 4,
  "max_visible_consolidated": 5,
  "meta_permanent_threshold": 5,
  "enable_model_summaries": true,
  "enable_model_file_descriptions": true,
  "enable_model_user_facts": true,
  "user_facts_max_chars": 16000,
  "model_file_max_bytes": 8388608,
  "openai_api_key": "",
  "openai_api_key_env": "OPENAI_API_KEY",
  "openai_base_url": "https://api.openai.com/v1/responses",
  "openai_model": "gpt-5.4",
  "openai_reasoning_effort": "high",
  "openai_timeout_seconds": 45,
  "pending_retry_enabled": true,
  "pending_retry_base_seconds": 30,
  "pending_retry_max_seconds": 480
}
```

When an OpenAI API key is configured, the plugin uses the Responses API with `gpt-5.4` and `reasoning.effort = "high"` for model-backed summaries, file descriptions, and richer user-fact extraction by default.

`max_injection_chars` is the only cap on injected chat-history size. The plugin does not apply a separate entry-count limit.

## Compaction tiers

- Raw history: newest messages and file entries remain verbatim.
- Temporary summaries: oldest raw chunks are archived into `temp_*` files and replaced with compact summaries.
- Consolidated summaries: older temporary chunks are merged into `cons_*` archive files and re-expressed as one summary.
- Meta summaries: overflow consolidated summaries are folded into meta summaries with source-archive references.

When model-backed summarization is enabled, compaction tries the API first and falls back to a deterministic placeholder summary if the API fails. That placeholder is then refreshed by a background retry worker when the API becomes available again.

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
