# Codex Long-Term Memory

This plugin ports the core idea of `Claude-Code-Long-Term-Memory` to Codex hooks.

## What it does

- Logs every submitted user prompt
- Logs every final assistant reply
- Captures file attachments and assistant file/tool references when transcript data is available
- Injects recent cross-thread history at `SessionStart`
- Reinjects full memory once on the next user turn after detected Codex auto-compaction
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
- `~/.codex/long-term-memory/compaction_scan_state.json`
- `~/.codex/long-term-memory/config.json`

Default config:

```json
{
  "max_injection_chars": 300000,
  "include_timestamps": true,
  "enable_user_facts": true,
  "enable_calendar": true,
  "enable_attachment_capture": true,
  "compact_threshold_chars": 80000,
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

## Reinjection after Codex compaction

Codex does not currently expose a dedicated post-compaction hook. Claude Code does, which is why this extra detection path is needed only on the Codex side right now.

This plugin therefore keeps the full history injection at `SessionStart`, and adds a one-shot reinjection path on `UserPromptSubmit`. The rollout-log scan is a workaround for the missing Codex hook, not the ideal long-term design. If Codex later adds a real post-compaction hook, this plugin should stop doing log-based detection and switch to that direct hook instead.

On each user prompt, the plugin scans the current thread's rollout log under `~/.codex/sessions/` and looks only for this exact JSON event shape:

```json
{
  "type": "event_msg",
  "payload": {
    "type": "context_compacted"
  }
}
```

The detector is structural. It parses JSONL records and requires both the top-level `type == "event_msg"` and nested `payload.type == "context_compacted"`. It does not trigger on plain text that merely mentions `context_compacted`, and it does not treat the separate top-level `type == "compacted"` rollout record as the reinjection signal.

The scanner is also defensive about partially written JSONL output. If the newest trailing line is incomplete when the hook reads the file, the scanner leaves the offset at the start of that line and retries it on the next scan instead of advancing past it and missing a real compaction event forever.

Compaction-log lookup is intentionally keyed only by `thread_id`. The broader history pipeline may still record `session_id` as a fallback identifier in some places, but rollout-log detection does not assume `session_id` and `thread_id` are interchangeable.

When that exact event is seen, the plugin records a pending one-shot reinjection in `~/.codex/long-term-memory/compaction_scan_state.json`. On the next user turn for that same thread, it appends the normal long-term-memory context once and then clears the pending flag.

One limitation remains: because hooks run on user submits, reinjection happens on the next turn after compaction, not in the middle of the already-compacted turn.

### Upgrade check after Codex updates

After updating Codex, trigger a real compaction and confirm the rollout logs still contain an `event_msg` record whose nested payload type is exactly `context_compacted`. If Codex changes the rollout filename pattern, top-level record type, or nested payload shape, update the detector in `lib/common.py` before relying on post-compaction reinjection.

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
