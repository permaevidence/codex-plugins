# Codex Parity Roadmap

This document tracks parity between the original Claude Code plugins and the Codex-native ports in this repo.

## Goal

Reach practical feature parity for:

1. Long-term memory across Codex sessions
2. Telegram-driven unattended Codex operation

When Codex offers a better primitive, parity uses the Codex-native primitive rather than a literal port.

## Current status

### Memory plugin

- Reached:
  - hook install and uninstall
  - user prompt logging
  - assistant reply logging
  - session-start history injection
  - durable user-fact extraction and injection
  - optional calendar injection via `gws`
  - history compaction with archive-backed summaries
  - backup, reset, and restore workflows
- Remaining gap:
  - Claude's multi-tier LLM summarization is still replaced by deterministic local compaction
  - assistant attachment and tool-file capture is still lighter than the Claude hook implementation

### Telegram plugin

- Reached:
  - DM polling
  - pairing and allowlist access control
  - persistent Codex thread mapping
  - `/help`, `/status`, `/stop`, `/newsession`
  - auto-approve command, file-change, and permission approval requests
  - graceful handling for `item/tool/requestUserInput`
  - voice transcription
  - reminders
  - Gmail polling via `gws`
  - groups, mentions, and mention regexes
  - delivery controls: ack reactions, reply threading, chunking
  - on-disk Codex version monitor
  - bundled restart-loop supervisor script
  - Codex-exposed Telegram action tools for replies, edits, and reactions
- Remaining gap:
  - no major Telegram workflow gap remains relative to the Claude plugin

## Phases

### Phase 1: Memory foundation

- Status: complete

### Phase 2: Telegram unattended robustness

- Status: complete

### Phase 3: Telegram feature parity

- Status: complete

### Phase 4: Polish and verification

- Status: in progress
- Remaining work:
  - run a live end-to-end Telegram test with real bot credentials
  - document intentional Codex-native differences more explicitly
  - decide whether the remaining memory-side differences should be closed literally or kept as Codex-native variants

## Implementation principles

- Prefer Codex-native primitives where they are clearly better:
  - Codex hooks for memory
  - Codex app-server for remote conversation control
  - Codex Memories as a complement, not a replacement, for explicit history injection
- Preserve user data on uninstall unless explicitly asked to delete it
- Keep optional integrations soft-failing
