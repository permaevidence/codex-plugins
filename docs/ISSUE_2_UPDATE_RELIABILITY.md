# Issue 2: update reliability corrections

Review target: `fix/issue-2-update-reliability`, Telegram bridge 0.7.12.
Issue: https://github.com/permaevidence/codex-plugins/issues/2

## Changes

1. Doctor and live app discovery now share complete cursor traversal, repeated-cursor detection, strict response validation and explicit incomplete-result exceptions. A partial dictionary is diagnostic evidence, never a successful return value.
2. Initial refresh defaults to 60 seconds, later pages to 15 seconds, and the traversal to 120 seconds. Configurable deadlines, 100-page and 20,000-entry limits prevent unbounded requests. Doctor uses the configured Codex command and completes initialization before querying apps.
3. The bridge test module isolates home and XDG paths before imports, redirects cached Telegram helper paths, and rejects operational state I/O with an audit guard. The fake-crash test mocks and asserts the health write; recovery tests also explicitly redirect their health file. Direct test-file execution now includes the classes formerly below `unittest.main()`.
4. Successful initialization clears only `app_server_exit`, while holding the same lock used to publish EOF. The child must still be alive and an EOF must not already have been observed. Other errors and failed handshakes retain their status.
5. On macOS with `dangerFullAccess`, plugin-update preflight refuses missing/unconfigured stable native paths before activation or service changes. The error gives the supported `update_codex.py --no-restart` migration command and macOS permission guidance. Native preparation preserves the npm installation, validates signatures, and restores the original launcher configuration on a later failure. Invalid configuration is preserved rather than replaced with defaults.
6. Doctor can emit a structured report. Updaters accept a validated runtime with degraded external Google discovery/connection findings. Ordinary doctor and revalidation remain strict. Protocol, cursor, configuration, plugin and other runtime failures remain blocking. Update records include failed stages and structured findings; CLI no-op checks avoid restarting when package and command are unchanged. Rollback outcomes no longer receive unconditional success wording. A failed service stop aborts before recovery-queue edits.
7. Both updater scripts support `--revalidate`. Only full doctor success adds a recovery resolution; original failed status, error and timestamps remain. Updates, revalidation and notification receipts use a common maintenance lock. Late notification receipts preserve resolutions and cannot overwrite newer attempts. `/health` separates live component health from each updater's history.
8. Supervisor logging includes PIDs and exact wait status, including signal exits. Operator stop/restart logs include reason and request ID. App-server logs include bridge/child PIDs, EOF status when available, and requested shutdown. Bridge Python runs unbuffered.

## Evidence

- Local setup/runtime suite: 68 tests passed.
- Local Telegram suite: 78 tests passed, with production-state I/O blocked.
- Local memory suite: 58 tests passed using pinned dependencies in a disposable virtual environment.
- Compilation, supervisor shell syntax and `git diff --check` passed.
- Live read-only app discovery completed with **3,559 unique apps in 22.09 seconds**. This confirms traversal beyond the old 1,200-entry limit. No model inference was requested.
- Regression coverage includes late-page connectors, simulated 17.78-second refresh, partial timeout/protocol/cursor failures, deadline and size bounds, stale-health ordering, updater rollback/degradation/no-op decisions, preserved recovery history, overlapping maintenance ownership, and a real disposable supervisor receiving exit 23 and SIGTERM (143).

## Deployment limits

- No running bridge, installed plugin runtime, login, pairing or user configuration was changed during implementation. The live catalog probe started and terminated its own app-server.
- macOS npm migration was tested with isolated state and mocked installation, not by replacing the working installation on this Mac.
- An updater process already running older code cannot gain the new preflight retroactively. Use this checkout's updater/migration command when upgrading such an installation; this caveat is documented in the README.
- A successful handshake establishes transport recovery, not renewed subscription/API credit or successful inference. Those errors are not cleared.
- The 60-second initial deadline is a configurable default, not a guarantee against every network outage. Truncation and timeout remain visible failures.
