# Codex Long-Term Memory

This plugin ports the core idea of `Claude-Code-Long-Term-Memory` to Codex hooks.

## What it does

- Logs every submitted user prompt
- Logs every final assistant reply
- Captures file attachments and assistant file/tool references when transcript data is available
- Injects recent cross-thread history at `SessionStart` for small/legacy hook-transport setups
- Writes large memory overlays into a marked `AGENTS.md` block for modern Codex releases that spill large hook output
- Refreshes the marked `AGENTS.md` block from a real `PreCompact` hook before Codex compacts
- Extracts durable user facts from earlier conversations
- Optionally injects upcoming calendar items via `gws`
- Compacts older history into archive-backed summaries with temporary, consolidated, and meta tiers
- Uses the OpenAI Responses API for summaries, user-fact extraction, and file descriptions when configured
- Queues failed summary refreshes under `~/.codex/long-term-memory/pending/` and retries them in the background
- Stores everything under `~/.codex/long-term-memory/`
- Merges its hook registrations into `~/.codex/hooks.json`

## Why this differs from the Claude version

Codex already has a native Memories feature, but it is separate from hook-based full-history injection. This plugin focuses on explicit cross-thread recall using hook output.

This implementation now mirrors the Claude plugin much more closely: durable user facts, richer calendar injection, hierarchical summary tiers, raw archives, transcript-driven file capture, model-backed summaries, and model-backed file descriptions are all in place. When model-backed summaries are enabled, failed or too-short model summaries leave the source material in place for retry instead of saving weak fallback memory. Deterministic summaries are used only when model-backed summaries are intentionally disabled.

## Install

For a first-time setup with the companion Telegram bridge, prefer the repo-level wizard:

```bash
python3 /absolute/path/to/repo/scripts/setup.py
```

The wizard installs both plugins, requires an OpenAI API key, configures `agents_md`, writes the memory and Telegram `.env` files, and optionally starts the bridge.

Manual memory-only install:

Run:

```bash
python3 /absolute/path/to/repo/plugins/codex-long-term-memory/scripts/install.py
```

The installer:

- enables `features.hooks = true` in `~/.codex/config.toml`
- registers the plugin hooks in `~/.codex/hooks.json`
- creates `~/.codex/long-term-memory/config.json` if missing

Restart Codex after installing.

After installation, inspect hook trust in Codex or through the app-server `hooks/list` method. Codex tracks hook hashes locally; newly added hooks may need to be trusted before they run in normal sessions.

## Files

- `~/.codex/long-term-memory/history.jsonl`
- `~/.codex/long-term-memory/user_facts.jsonl`
- `~/.codex/long-term-memory/archives/`
- `~/.codex/long-term-memory/files/`
- `~/.codex/long-term-memory/backups/`
- `~/.codex/long-term-memory/pending/`
- `~/.codex/long-term-memory/compaction_scan_state.json`
- `~/.codex/long-term-memory/config.json`
- `~/.codex/long-term-memory/.env`

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
  "openai_model": "gpt-5.6-luna",
  "openai_reasoning_effort": "high",
  "openai_timeout_seconds": 240,
  "minimum_model_summary_words": 100,
  "summary_max_chars": 10000,
  "pending_retry_enabled": true,
  "pending_retry_base_seconds": 30,
  "pending_retry_max_seconds": 480,
  "injection_transport": "hook",
  "agents_md_path": "",
  "agents_project_doc_max_bytes": 524288
}
```

When an OpenAI API key is configured, the plugin uses the Responses API with `gpt-5.6-luna` and `reasoning.effort = "high"` for model-backed summaries, file descriptions, and richer user-fact extraction by default. The repo-level setup wizard treats this key as required because memory quality is poor without model-backed summaries.

Put secrets in `~/.codex/long-term-memory/.env` or the process environment rather than `config.json`:

```dotenv
OPENAI_API_KEY=sk-...
```

If you also use the Telegram bridge, use the same OpenAI key there for voice transcription, but it must be copied separately into `~/.codex/telegram-bridge/.env`. This memory plugin reads `~/.codex/long-term-memory/.env`; it does not automatically read the Telegram bridge `.env`.

`max_injection_chars` is the plugin's cap on rendered chat-history size. Modern Codex releases may still cap large hook output separately, so use `agents_md` transport when you need very large memory overlays to be visible inline.

## AGENTS.md transport

Recent Codex releases spill large hook `additionalContext` output to temp files instead of embedding it inline. To avoid that for large memory overlays, use `agents_md` transport.

For a single-user setup where Codex normally starts from your home directory, set:

```json
{
  "injection_transport": "agents_md",
  "agents_md_path": "~/AGENTS.md",
  "agents_project_doc_max_bytes": 524288
}
```

Copy-paste helper for the single-user setup:

```bash
python3 - <<'PY'
import json
from pathlib import Path

path = Path.home() / ".codex/long-term-memory/config.json"
config = json.loads(path.read_text())
config.update({
    "injection_transport": "agents_md",
    "agents_md_path": "~/AGENTS.md",
    "agents_project_doc_max_bytes": 524288,
})
path.write_text(json.dumps(config, indent=2) + "\n")
PY
```

For a project-specific setup, use that project's `AGENTS.md` and make the Telegram bridge `default_cwd` point to the same project directory:

```json
{
  "injection_transport": "agents_md",
  "agents_md_path": "/absolute/path/to/project/AGENTS.md",
  "agents_project_doc_max_bytes": 524288
}
```

When the Telegram bridge starts, and again when `/newsession` is received, it runs `scripts/update_agents_injection.py`. That helper rewrites only the marked long-term-memory block appended to the target `AGENTS.md`, updates `~/.codex/long-term-memory/injected_context.md`, and raises top-level `project_doc_max_bytes` in `~/.codex/config.toml` high enough for Codex to embed the whole file.

You can also refresh the block manually:

```bash
python3 /absolute/path/to/repo/plugins/codex-long-term-memory/scripts/update_agents_injection.py --cwd /absolute/path/to/project
```

In this mode the `SessionStart` hook no longer emits the full memory overlay, and the old compaction reinjection path on `UserPromptSubmit` is disabled. The `PreCompact` hook refreshes the marked `AGENTS.md` block before Codex compacts so the durable file is current before messages are removed from the live context. Codex reads `AGENTS.md` at fresh session start, so use `/newsession` after changing this mode or when a current thread needs to start with the refreshed embedded history.

The target `AGENTS.md` is edited only between these markers:

```md
<!-- BEGIN CODEX LONG-TERM-MEMORY INJECTION -->
...
<!-- END CODEX LONG-TERM-MEMORY INJECTION -->
```

Existing instructions outside the marked block are preserved.

## Hook transport

Hook transport is still available:

```json
{
  "injection_transport": "hook"
}
```

Use it only when your rendered memory is small enough for your Codex version's hook-output behavior, or when you intentionally prefer temp-file spill/retrieval behavior. On recent Codex releases, large `SessionStart` or `UserPromptSubmit` `additionalContext` values are not fully embedded inline.

## Compaction tiers

- Raw history: newest messages and file entries remain verbatim.
- Temporary summaries: oldest raw chunks are archived into `temp_*` files and replaced with compact summaries.
- Consolidated summaries: older temporary chunks are merged into `cons_*` archive files and re-expressed as one summary.
- Meta summaries: older overflow consolidated summaries are folded into meta summaries with source-archive references, while the most recent consolidated summaries stay visible individually.

When model-backed summarization is enabled, compaction requires a non-empty, substantive model summary. If the API is unavailable or returns an output that is too short, the source material is left in place and compaction retries on a later stop/start cycle. Deterministic fallback summaries are used only when model-backed summaries are intentionally disabled.

## Codex compaction hooks

Current Codex releases expose `PreCompact` and `PostCompact` hooks. The compact-hook output schema supports continuing or stopping compaction; it does not support large `additionalContext` reinjection. This plugin therefore uses `PreCompact` as a side-effect hook: it refreshes the `AGENTS.md` memory block before compaction runs.

For legacy hook-transport setups, the plugin still keeps the full history injection at `SessionStart`, and has a one-shot reinjection path on `UserPromptSubmit`.

### Legacy rollout-log detection

The rollout-log scanner is kept only for older hook-transport behavior. It is not used for the `AGENTS.md` transport.

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

After updating Codex, trigger a real compaction and confirm `PreCompact` still fires with the expected `trigger` values, currently `manual` and `auto`. For legacy hook-transport setups, also confirm the rollout logs still contain an `event_msg` record whose nested payload type is exactly `context_compacted`.

Also check hook trust after updates or local edits. In the app-server `hooks/list` result, the expected memory hooks are:

- `preCompact`
- `sessionStart`
- `userPromptSubmit`
- `stop`

They should be enabled and trusted on the local machine.

## Data management

Create a backup:

```bash
python3 /absolute/path/to/repo/plugins/codex-long-term-memory/scripts/backup.py
```

Reset memory data after making a backup:

```bash
python3 /absolute/path/to/repo/plugins/codex-long-term-memory/scripts/reset.py
```

List backups or restore one:

```bash
python3 /absolute/path/to/repo/plugins/codex-long-term-memory/scripts/restore.py
python3 /absolute/path/to/repo/plugins/codex-long-term-memory/scripts/restore.py <backup_name>
```

## Uninstall

```bash
python3 /absolute/path/to/repo/plugins/codex-long-term-memory/scripts/uninstall.py
```

This removes only this plugin's hook entries. It does not delete your saved history.
