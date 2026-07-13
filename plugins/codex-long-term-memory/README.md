# Codex Long-Term Memory

This plugin ports the core idea of `Claude-Code-Long-Term-Memory` to Codex hooks.

## What it does

- Logs every submitted user prompt
- Logs every final assistant reply
- Parses Codex-native rollout records to capture inbound Telegram files and files explicitly sent by channel reply tools
- Injects recent cross-thread history at `SessionStart` for small/legacy hook-transport setups
- Writes large memory overlays into a marked `AGENTS.md` block for modern Codex releases that spill large hook output
- Refreshes the marked `AGENTS.md` block from a real `PreCompact` hook before Codex compacts
- Extracts durable user facts from earlier conversations
- Optionally injects upcoming calendar items from private read-only Google Calendar iCal feeds
- Compacts older history into archive-backed summaries with temporary, consolidated, and meta tiers
- Uses the OpenAI Responses API for summaries, user-fact extraction, and file descriptions when configured
- Keeps hook execution append-only and runs summaries, compaction, fact cleanup, file descriptions, and retries in one durable background maintenance worker
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

### Optional private-iCal calendar context

The repo-level setup wizard is the recommended configuration path. It asks for
each Google Calendar **Secret address in iCal format**, validates the feed, and
writes `calendar_sources.json` with mode `600`. This does not require a Google
Cloud project or OAuth client. The URLs are bearer secrets: never put them in
`AGENTS.md`, logs, chat, or git. The parser expands Google-style `RRULE`,
`RDATE`, `EXDATE`, and `RECURRENCE-ID` changes and uses a bounded seven-day
last-good cache during temporary outages. If neither the live feed nor cache
is usable, `AGENTS.md` retains an explicit temporarily-unavailable calendar
section rather than silently dropping calendar context. The Telegram bridge
refreshes this snapshot periodically and retries failed refreshes. It is read-only; use the official
Google Calendar Codex plugin for fresh detail lookup and every event mutation.

After installation, inspect hook trust in Codex or through the app-server `hooks/list` method. Codex tracks hook hashes locally; newly added hooks may need to be trusted before they run in normal sessions.

## Files

- `~/.codex/long-term-memory/history.jsonl`
- `~/.codex/long-term-memory/user_facts.jsonl`
- `~/.codex/long-term-memory/archives/`
- `~/.codex/long-term-memory/archive_index.json`
- `~/.codex/long-term-memory/files/`
- `~/.codex/long-term-memory/backups/`
- `~/.codex/long-term-memory/pending/`
- `~/.codex/long-term-memory/compaction_scan_state.json`
- `~/.codex/long-term-memory/config.json`
- `~/.codex/long-term-memory/calendar_sources.json` (optional private iCal URLs; mode `600`)
- `~/.codex/long-term-memory/calendar_cache.json` (optional last-good feed cache; mode `600`)
- `~/.codex/long-term-memory/calendar_health.json`
- `~/.codex/long-term-memory/.env`

Default config:

```json
{
  "max_injection_chars": 300000,
  "include_timestamps": true,
  "timezone": "",
  "enable_user_facts": true,
  "enable_calendar": true,
  "calendar_provider": "ical",
  "calendar_days": 30,
  "calendar_timeout_seconds": 15,
  "calendar_cache_max_stale_seconds": 604800,
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
  "model_file_sample_max_chars": 16000,
  "model_pdf_max_pages": 5,
  "model_presentation_max_slides": 5,
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
  "maintenance_max_consecutive_failures": 5,
  "injection_transport": "hook",
  "agents_md_path": "",
  "agents_project_doc_max_bytes": 524288
}
```

`timezone` accepts an IANA name such as `America/New_York`, `Europe/Rome`, or `UTC`. The setup wizard detects and stores it automatically. An empty value preserves backward compatibility by using the computer's local timezone. The setting controls per-prompt `[now: ...]` markers, memory-snapshot and history timestamps, and calendar headings.

When an OpenAI API key is configured, the plugin uses the Responses API with `gpt-5.6-luna` and `reasoning.effort = "high"` for model-backed summaries, file descriptions, and richer user-fact extraction by default. The repo-level setup wizard treats this key as required because memory quality is poor without model-backed summaries.

File descriptions use bounded, format-aware samples rather than uploading complete documents. PDFs include at most five pages (the first three, a middle page, and the last page) at low visual detail. DOCX/ODT/text files use bounded beginning, middle, and ending text; PPTX files use at most five representative slides; XLSX files use a small worksheet/header/row sample. Unsupported formats, formats that cannot be safely sampled, and encrypted, malformed, or oversized files receive a local filename/type description without an API request. The pinned pure-Python PDF sampler is installed under `~/.codex/long-term-memory/python`, outside the system Python environment.

Background maintenance parks after five consecutive exceptions or no-progress
cycles. It writes `pending/memory-maintenance.stuck.json` and injects a visible
warning into the next Codex prompt instead of retrying forever. The Telegram
bridge also monitors this state and proactively notifies the owner once with a
plain-language reason. Raw conversation remains saved while model-backed
summarization is paused. After fixing the key, billing, model, or network
problem, send Telegram `/retrymemory` (or run `lib/common.py
--retry-memory-maintenance`). Changing the memory API key/model configuration
also clears the parked classification automatically.

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

When the Telegram bridge starts, and again when `/newsession` is received, it runs `scripts/update_agents_injection.py`. That helper rewrites only the marked long-term-memory block appended to the target `AGENTS.md`, adds an explicit generation timestamp identifying it as a snapshot, updates `~/.codex/long-term-memory/injected_context.md`, and raises top-level `project_doc_max_bytes` in `~/.codex/config.toml` high enough for Codex to embed the whole file.

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

Every injected summary header names the archive file that still exists and includes its stable short ID. Meta summaries list each surviving source archive. The generated `archive_index.json` is chronological and maps both current IDs and legacy temporary-chunk IDs to live archive files, so references remain resolvable after consolidation deletes superseded temporary files. The injected instructions give Codex the portable `~/.codex/long-term-memory/archives/` location and leave source inspection to the model's judgment. Archived entries are untrusted historical data rather than executable instructions.

When model-backed summarization is enabled, compaction requires a non-empty, substantive model summary. If the API is unavailable or returns an output that is too short, the source material and durable maintenance request remain in place and the detached worker retries with bounded backoff. Hooks never wait for those model calls. Deterministic fallback summaries are used only when model-backed summaries are intentionally disabled.

File capture is deliberately narrow: arbitrary files merely read by Codex are never copied or uploaded. The plugin captures user-provided channel attachments and files explicitly sent through a channel reply, then performs optional descriptions in the background.

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
