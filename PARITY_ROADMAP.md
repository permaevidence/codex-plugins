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
  - transcript-driven file attachment and tool-file capture
  - session-start history injection
  - durable user-fact extraction and injection
  - optional calendar injection via private read-only iCal feeds
  - history compaction with temporary, consolidated, and meta archive-backed summaries
  - OpenAI-backed summaries, file captions, and user-fact extraction with deterministic fallback
  - durable single-worker maintenance queue for summary retries, compaction, fact cleanup, and file descriptions
  - backup, reset, and restore workflows
- Remaining gap:
  - no major memory workflow gap remains relative to the Claude plugin

### Telegram plugin

- Reached:
  - DM polling
  - pairing and allowlist access control
  - persistent Codex thread mapping
  - `/help`, `/status`, `/stop`, `/newsession`
  - auto-approve command, file-change, and permission approval requests
  - auto-answering for structured `item/tool/requestUserInput` prompts, with fallback notification when safe inference is not possible
  - voice transcription
  - reminders
  - read-only Gmail IMAP polling with official Gmail-plugin actions
  - groups, mentions, and mention regexes
  - delivery controls: ack reactions, reply threading, chunking
  - on-disk Codex version monitor
  - bundled restart-loop supervisor script
  - inbound attachment metadata and downloaded photo-path forwarding
  - Codex-exposed Telegram action tools for replies, attachments, inbound file download, edits, and reactions
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
  - continue expanding live end-to-end regression coverage across Codex releases
  - document intentional Codex-native differences more explicitly
  - keep the installed-runtime hook/MCP/API smoke tests current as Codex evolves

## Implementation principles

- Prefer Codex-native primitives where they are clearly better:
  - Codex hooks for memory
  - Codex app-server for remote conversation control
  - Codex Memories as a complement, not a replacement, for explicit history injection
- Preserve user data on uninstall unless explicitly asked to delete it
- Keep optional integrations soft-failing
