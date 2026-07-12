from __future__ import annotations

import importlib.util
import json
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import common
from lib.common import (
    AGENTS_MEMORY_BEGIN,
    AGENTS_MEMORY_END,
    compaction_state_has_thread,
    current_compaction_thread_id,
    current_thread_id,
    ensure_project_doc_max_bytes,
    is_context_compacted_event,
    replace_marked_agents_block,
    refresh_agents_memory_injection,
    scan_rollout_log_for_compaction,
)

INSTALL_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install.py"
INSTALL_SPEC = importlib.util.spec_from_file_location("memory_install", INSTALL_SCRIPT)
assert INSTALL_SPEC is not None
memory_install = importlib.util.module_from_spec(INSTALL_SPEC)
assert INSTALL_SPEC.loader is not None
INSTALL_SPEC.loader.exec_module(memory_install)
STOP_SCRIPT = Path(__file__).resolve().parents[1] / "hooks" / "stop.py"
STOP_SPEC = importlib.util.spec_from_file_location("memory_stop_hook", STOP_SCRIPT)
assert STOP_SPEC and STOP_SPEC.loader
memory_stop = importlib.util.module_from_spec(STOP_SPEC)
STOP_SPEC.loader.exec_module(memory_stop)


class CompactionDetectionTests(unittest.TestCase):
    def test_history_append_schedules_background_maintenance_without_compacting_inline(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.object(
            common, "HISTORY_FILE", Path(tmpdir) / "history.jsonl"
        ), mock.patch.object(
            common, "HISTORY_LOCK_FILE", Path(tmpdir) / "history.lock"
        ), mock.patch.object(
            common, "ensure_state_dir"
        ), mock.patch.object(
            common, "load_config", return_value={"enable_user_facts": False}
        ), mock.patch.object(
            common, "schedule_memory_maintenance"
        ) as schedule, mock.patch.object(
            common, "maybe_compact_history", side_effect=AssertionError("must not run in hook")
        ):
            common.append_history_entries([{"role": "user", "content": "hello"}])
            schedule.assert_called_once()

    def test_codex_rollout_parser_captures_channel_files_but_not_arbitrary_tool_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            incoming = root / "incoming.pdf"
            outgoing = root / "report.pdf"
            secret = root / ".env"
            incoming.write_bytes(b"incoming")
            outgoing.write_bytes(b"outgoing")
            secret.write_text("SECRET=value", encoding="utf-8")
            rollout = root / "rollout.jsonl"
            records = [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": f'<channel file_path="{incoming}">Review this'}],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {"type": "function_call", "name": "exec_command", "arguments": json.dumps({"path": str(secret)})},
                },
                {
                    "type": "response_item",
                    "payload": {"type": "function_call", "name": "reply", "arguments": json.dumps({"files": [str(outgoing)]})},
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Done"}],
                    },
                },
            ]
            rollout.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")
            with mock.patch.object(memory_stop, "FILES_DIR", root / "stored"):
                (root / "stored").mkdir()
                files = memory_stop.extract_files_from_turn(
                    str(rollout), 0, {}, {"enable_model_file_descriptions": False}
                )
            originals = {Path(str(item.get("original_path"))).name for item in files}
            self.assertEqual(originals, {"incoming.pdf", "report.pdf"})
            self.assertNotIn(".env", originals)
            self.assertEqual(memory_stop.extract_last_assistant_message(str(rollout)), "Done")
    def test_default_memory_model_is_luna_high(self) -> None:
        self.assertEqual(common.DEFAULT_CONFIG["openai_model"], "gpt-5.6-luna")
        self.assertEqual(common.DEFAULT_CONFIG["openai_reasoning_effort"], "high")
        self.assertEqual(memory_install.DEFAULT_STATE_CONFIG["openai_model"], "gpt-5.6-luna")
        self.assertEqual(memory_install.DEFAULT_STATE_CONFIG["openai_reasoning_effort"], "high")

    def test_consolidated_summary_names_surviving_archive_not_deleted_sources(self) -> None:
        rendered = common.render_entry(
            {
                "role": "summary",
                "summary_type": "consolidated",
                "content": "Durable summary",
                "covers_from": "2026-01-01T00:00:00+00:00",
                "covers_to": "2026-01-02T00:00:00+00:00",
                "archive_file": "cons_2026-01-01_to_2026-01-02_deadbeef.jsonl",
                "source_archives": ["temp_2026-01-01_to_2026-01-01_old12345.jsonl"],
            },
            True,
        )

        self.assertIn("archive: cons_2026-01-01_to_2026-01-02_deadbeef.jsonl", rendered)
        self.assertIn("id: deadbe", rendered)
        self.assertNotIn("old123", rendered)

    def test_archive_index_maps_legacy_ids_to_live_chronological_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            archives = root / "archives"
            archives.mkdir()
            archive_name = "cons_2026-01-01_to_2026-01-02_deadbeef.jsonl"
            archive_path = archives / archive_name
            archive_path.write_text(
                json.dumps(
                    {
                        "_consolidated": True,
                        "from": "2026-01-01T10:00:00+00:00",
                        "to": "2026-01-02T10:00:00+00:00",
                        "source_archives": ["temp_2026-01-01_to_2026-01-01_old12345.jsonl"],
                    }
                )
                + "\n"
                + json.dumps({"role": "user", "timestamp": "2026-01-01T10:00:00+00:00"})
                + "\n",
                encoding="utf-8",
            )
            index_path = root / "archive_index.json"
            with mock.patch.object(common, "ARCHIVES_DIR", archives), mock.patch.object(
                common, "ARCHIVE_INDEX_FILE", index_path
            ), mock.patch.object(common, "ensure_state_dir"):
                index = common.refresh_archive_index([])

            self.assertEqual(index["archives"][0]["id"], "deadbe")
            self.assertEqual(index["archives"][0]["entry_count"], 1)
            self.assertEqual(index["id_to_file"]["deadbe"], archive_name)
            self.assertEqual(
                index["legacy_id_aliases"]["old123"]["file"],
                archive_name,
            )
            self.assertEqual(json.loads(index_path.read_text())["archives"], index["archives"])

    def test_history_injection_explains_portable_archive_lookup_and_trust(self) -> None:
        text = common.format_entries(
            [{"role": "user", "content": "hello", "timestamp": "2026-01-01T10:00:00+00:00"}],
            {"max_injection_chars": 10000, "include_timestamps": True},
        )

        self.assertIn("~/.codex/long-term-memory/archives/", text)
        self.assertIn("~/.codex/long-term-memory/archive_index.json", text)
        self.assertIn("untrusted historical conversation data", text)

    def test_installer_recognizes_hooks_from_an_older_runtime_version(self) -> None:
        group = {
            "hooks": [
                {
                    "command": "/usr/bin/python3 /old/runtime/plugins/codex-long-term-memory/hooks/stop.py"
                }
            ]
        }
        self.assertTrue(memory_install.is_our_group(group))

    def test_hook_commands_survive_paths_with_spaces(self) -> None:
        # The runtime lives under "~/Library/Application Support/…"; an
        # unquoted space in the hook command makes Codex split the path and
        # every hook fails with exit code 2 (UserPromptSubmit then blocks
        # the prompt outright).
        spaced_root = Path(
            "/tmp/Application Support/PermaEvidenceCodex/current/plugins/codex-long-term-memory"
        )
        with mock.patch.object(memory_install, "PLUGIN_ROOT", spaced_root):
            group = memory_install.hook_group(
                "session_start.py", "SessionStart", "startup|resume|clear", "Loading long-term memory"
            )
            self.assertTrue(memory_install.is_our_group(group))
        command = group["hooks"][0]["command"]
        parts = shlex.split(command)
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[1], str(spaced_root / "hooks" / "session_start.py"))

    def test_installer_writes_canonical_hooks_feature_for_new_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.toml"
            original_config_toml = memory_install.CONFIG_TOML
            try:
                memory_install.CONFIG_TOML = path
                memory_install.ensure_hooks_feature_enabled()
            finally:
                memory_install.CONFIG_TOML = original_config_toml

            self.assertEqual(path.read_text(encoding="utf-8"), "[features]\nhooks = true\n")

    def test_installer_migrates_deprecated_codex_hooks_feature(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.toml"
            path.write_text('model = "gpt-5.5"\n\n[features]\ncodex_hooks = true\n', encoding="utf-8")
            original_config_toml = memory_install.CONFIG_TOML
            try:
                memory_install.CONFIG_TOML = path
                memory_install.ensure_hooks_feature_enabled()
            finally:
                memory_install.CONFIG_TOML = original_config_toml

            text = path.read_text(encoding="utf-8")
            self.assertIn("[features]\nhooks = true", text)
            self.assertNotIn("codex_hooks", text)

    def test_installer_forces_existing_hooks_feature_on(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.toml"
            path.write_text('model = "gpt-5.5"\n\n[features]\nhooks = false\n', encoding="utf-8")
            original_config_toml = memory_install.CONFIG_TOML
            try:
                memory_install.CONFIG_TOML = path
                memory_install.ensure_hooks_feature_enabled()
            finally:
                memory_install.CONFIG_TOML = original_config_toml

            self.assertIn("[features]\nhooks = true", path.read_text(encoding="utf-8"))

    def test_installer_updates_only_features_hooks_key(self) -> None:
        text = (
            "[mcp_servers.example]\n"
            'command = "node"\n'
            "hooks = false\n\n"
            "[features]\n"
            "codex_hooks = true\n\n"
            "[profiles.default]\n"
            'model = "gpt-5.5"\n'
        )

        updated = memory_install.set_hooks_feature_enabled(text)

        self.assertIn("[mcp_servers.example]\ncommand = \"node\"\nhooks = false", updated)
        self.assertIn("[features]\nhooks = true", updated)
        self.assertNotIn("codex_hooks", updated)

    def test_replace_marked_agents_block_preserves_surrounding_text(self) -> None:
        original = (
            "# Local Capabilities\n\n"
            "Keep this.\n\n"
            f"{AGENTS_MEMORY_BEGIN}\nold memory\n{AGENTS_MEMORY_END}\n\n"
            "Keep this too.\n"
        )

        updated = replace_marked_agents_block(original, "new memory")

        self.assertIn("Keep this.", updated)
        self.assertIn("Keep this too.", updated)
        self.assertIn("new memory", updated)
        self.assertNotIn("old memory", updated)
        self.assertEqual(updated.count(AGENTS_MEMORY_BEGIN), 1)
        self.assertEqual(updated.count(AGENTS_MEMORY_END), 1)

    def test_ensure_project_doc_max_bytes_inserts_top_level_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.toml"
            path.write_text('model = "gpt-5.5"\n\n[features]\nhooks = true\n', encoding="utf-8")

            changed = ensure_project_doc_max_bytes(524288, path)

            self.assertTrue(changed)
            text = path.read_text(encoding="utf-8")
            self.assertLess(text.index("project_doc_max_bytes = 524288"), text.index("[features]"))

            changed_again = ensure_project_doc_max_bytes(100000, path)
            self.assertFalse(changed_again)

    def test_refresh_agents_memory_injection_updates_marked_block_and_doc_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            state_dir = root / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            agents_path = root / "AGENTS.md"
            codex_config_path = root / "config.toml"
            agents_path.write_text("# Existing\n\nKeep this.\n", encoding="utf-8")
            config = {
                "injection_transport": "agents_md",
                "agents_md_path": str(agents_path),
                "agents_project_doc_max_bytes": 524288,
                "max_injection_chars": 300000,
                "include_timestamps": True,
                "enable_user_facts": False,
                "enable_calendar": False,
            }

            original_paths = {
                "STATE_DIR": common.STATE_DIR,
                "CONFIG_FILE": common.CONFIG_FILE,
                "HISTORY_FILE": common.HISTORY_FILE,
                "FACTS_FILE": common.FACTS_FILE,
                "ARCHIVES_DIR": common.ARCHIVES_DIR,
                "BACKUPS_DIR": common.BACKUPS_DIR,
                "FILES_DIR": common.FILES_DIR,
                "PENDING_DIR": common.PENDING_DIR,
                "INJECTED_CONTEXT_FILE": common.INJECTED_CONTEXT_FILE,
                "CODEX_CONFIG_FILE": common.CODEX_CONFIG_FILE,
            }

            try:
                common.STATE_DIR = state_dir
                common.CONFIG_FILE = state_dir / "config.json"
                common.HISTORY_FILE = state_dir / "history.jsonl"
                common.FACTS_FILE = state_dir / "user_facts.jsonl"
                common.ARCHIVES_DIR = state_dir / "archives"
                common.BACKUPS_DIR = state_dir / "backups"
                common.FILES_DIR = state_dir / "files"
                common.PENDING_DIR = state_dir / "pending"
                common.INJECTED_CONTEXT_FILE = state_dir / "injected_context.md"
                common.CODEX_CONFIG_FILE = codex_config_path
                common.CONFIG_FILE.write_text(json.dumps(config), encoding="utf-8")
                common.HISTORY_FILE.write_text(
                    json.dumps({"role": "user", "content": "hello from history"}) + "\n",
                    encoding="utf-8",
                )

                result = refresh_agents_memory_injection(config, str(root))

                self.assertTrue(result["enabled"])
                updated = agents_path.read_text(encoding="utf-8")
                self.assertIn("Keep this.", updated)
                self.assertIn("hello from history", updated)
                self.assertEqual(updated.count(AGENTS_MEMORY_BEGIN), 1)
                self.assertEqual(updated.count(AGENTS_MEMORY_END), 1)
                self.assertIn("project_doc_max_bytes = 524288", codex_config_path.read_text(encoding="utf-8"))
                self.assertIn("hello from history", common.INJECTED_CONTEXT_FILE.read_text(encoding="utf-8"))
            finally:
                for name, value in original_paths.items():
                    setattr(common, name, value)

    def test_accepts_exact_context_compacted_event(self) -> None:
        obj = {
            "type": "event_msg",
            "payload": {
                "type": "context_compacted",
            },
        }
        self.assertTrue(is_context_compacted_event(obj))

    def test_rejects_plain_text_mentions(self) -> None:
        obj = {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": "I saw context_compacted in a log snippet.",
                    }
                ],
            },
        }
        self.assertFalse(is_context_compacted_event(obj))

    def test_rejects_top_level_compacted_record(self) -> None:
        obj = {
            "type": "compacted",
            "payload": {
                "message": "",
                "replacement_history": [],
            },
        }
        self.assertFalse(is_context_compacted_event(obj))

    def test_scan_rollout_log_only_flags_exact_event(self) -> None:
        records = [
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "context_compacted"}],
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "context_compacted",
                },
            },
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sample.jsonl"
            payload = "".join(json.dumps(record) + "\n" for record in records)
            path.write_text(payload, encoding="utf-8")

            offset, saw_compaction, compaction_offset = scan_rollout_log_for_compaction(path, 0)
            self.assertTrue(saw_compaction)
            self.assertEqual(offset, path.stat().st_size)
            self.assertGreaterEqual(compaction_offset, 0)

            next_offset, saw_again, next_compaction_offset = scan_rollout_log_for_compaction(
                path,
                offset,
                compaction_offset,
            )
            self.assertFalse(saw_again)
            self.assertEqual(next_offset, offset)
            self.assertEqual(next_compaction_offset, compaction_offset)

    def test_scan_rollout_log_retries_partial_trailing_record(self) -> None:
        prefix_record = {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
            },
        }
        partial_record = '{"type":"event_msg","payload":{"type":"context_compacted"'
        suffix = '}}\n'

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sample.jsonl"
            prefix_payload = json.dumps(prefix_record) + "\n"
            path.write_text(prefix_payload + partial_record, encoding="utf-8")

            offset, saw_compaction, first_compaction_offset = scan_rollout_log_for_compaction(path, 0)
            self.assertFalse(saw_compaction)
            self.assertEqual(offset, len(prefix_payload.encode("utf-8")))
            self.assertEqual(first_compaction_offset, -1)

            with path.open("a", encoding="utf-8") as handle:
                handle.write(suffix)

            next_offset, saw_after_retry, latest_compaction_offset = scan_rollout_log_for_compaction(path, offset)
            self.assertTrue(saw_after_retry)
            self.assertEqual(next_offset, path.stat().st_size)
            self.assertGreaterEqual(latest_compaction_offset, 0)

    def test_scan_rollout_log_recovers_recent_compaction_from_overlap(self) -> None:
        prefix_record = {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
            },
        }
        compaction_record = {
            "type": "event_msg",
            "payload": {
                "type": "context_compacted",
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sample.jsonl"
            prefix_payload = json.dumps(prefix_record) + "\n"
            partial_compaction = json.dumps(compaction_record)[:-2]
            path.write_text(prefix_payload + partial_compaction, encoding="utf-8")

            stale_offset = path.stat().st_size
            with path.open("a", encoding="utf-8") as handle:
                handle.write("}}\n")

            next_offset, saw_after_recovery, latest_compaction_offset = scan_rollout_log_for_compaction(
                path,
                stale_offset,
            )
            self.assertTrue(saw_after_recovery)
            self.assertEqual(next_offset, path.stat().st_size)

            final_offset, saw_again, final_compaction_offset = scan_rollout_log_for_compaction(
                path,
                next_offset,
                latest_compaction_offset,
            )
            self.assertFalse(saw_again)
            self.assertEqual(final_offset, next_offset)
            self.assertEqual(final_compaction_offset, latest_compaction_offset)

    def test_compaction_detection_requires_thread_id(self) -> None:
        self.assertEqual(current_thread_id({"session_id": "session-123"}), "session-123")
        self.assertEqual(current_compaction_thread_id({"thread_id": "thread-123", "session_id": "session-123"}), "thread-123")

    def test_thread_state_consumes_reinjection_once(self) -> None:
        thread_id = "thread-123"
        records = [
            {
                "type": "event_msg",
                "payload": {
                    "type": "context_compacted",
                },
            }
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            state_dir = root / "state"
            sessions_dir = root / "sessions"
            log_dir = sessions_dir / "2026" / "04" / "22"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / f"rollout-2026-04-22T10-00-00-{thread_id}.jsonl"
            log_path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

            original_paths = {
                "STATE_DIR": common.STATE_DIR,
                "CONFIG_FILE": common.CONFIG_FILE,
                "HISTORY_FILE": common.HISTORY_FILE,
                "FACTS_FILE": common.FACTS_FILE,
                "ARCHIVES_DIR": common.ARCHIVES_DIR,
                "BACKUPS_DIR": common.BACKUPS_DIR,
                "FILES_DIR": common.FILES_DIR,
                "PENDING_DIR": common.PENDING_DIR,
                "COMPACTION_STATE_FILE": common.COMPACTION_STATE_FILE,
                "SESSIONS_DIR": common.SESSIONS_DIR,
            }

            try:
                common.STATE_DIR = state_dir
                common.CONFIG_FILE = state_dir / "config.json"
                common.HISTORY_FILE = state_dir / "history.jsonl"
                common.FACTS_FILE = state_dir / "user_facts.jsonl"
                common.ARCHIVES_DIR = state_dir / "archives"
                common.BACKUPS_DIR = state_dir / "backups"
                common.FILES_DIR = state_dir / "files"
                common.PENDING_DIR = state_dir / "pending"
                common.COMPACTION_STATE_FILE = state_dir / "compaction_scan_state.json"
                common.SESSIONS_DIR = sessions_dir

                self.assertTrue(common.update_compaction_reinjection_state(thread_id, consume=False))
                self.assertTrue(common.update_compaction_reinjection_state(thread_id, consume=True))
                self.assertFalse(common.update_compaction_reinjection_state(thread_id, consume=True))
            finally:
                for name, value in original_paths.items():
                    setattr(common, name, value)

    def test_session_id_only_payload_uses_rollout_backed_fallback(self) -> None:
        thread_id = "thread-123"
        records = [
            {
                "type": "event_msg",
                "payload": {
                    "type": "context_compacted",
                },
            }
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            state_dir = root / "state"
            sessions_dir = root / "sessions"
            log_dir = sessions_dir / "2026" / "04" / "22"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / f"rollout-2026-04-22T10-00-00-{thread_id}.jsonl"
            log_path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

            original_paths = {
                "STATE_DIR": common.STATE_DIR,
                "CONFIG_FILE": common.CONFIG_FILE,
                "HISTORY_FILE": common.HISTORY_FILE,
                "FACTS_FILE": common.FACTS_FILE,
                "ARCHIVES_DIR": common.ARCHIVES_DIR,
                "BACKUPS_DIR": common.BACKUPS_DIR,
                "FILES_DIR": common.FILES_DIR,
                "PENDING_DIR": common.PENDING_DIR,
                "COMPACTION_STATE_FILE": common.COMPACTION_STATE_FILE,
                "SESSIONS_DIR": common.SESSIONS_DIR,
            }

            try:
                common.STATE_DIR = state_dir
                common.CONFIG_FILE = state_dir / "config.json"
                common.HISTORY_FILE = state_dir / "history.jsonl"
                common.FACTS_FILE = state_dir / "user_facts.jsonl"
                common.ARCHIVES_DIR = state_dir / "archives"
                common.BACKUPS_DIR = state_dir / "backups"
                common.FILES_DIR = state_dir / "files"
                common.PENDING_DIR = state_dir / "pending"
                common.COMPACTION_STATE_FILE = state_dir / "compaction_scan_state.json"
                common.SESSIONS_DIR = sessions_dir

                self.assertTrue(common.should_reinject_after_compaction({"session_id": thread_id}))
                self.assertTrue(common.consume_compaction_reinjection({"session_id": thread_id}))
                self.assertFalse(common.consume_compaction_reinjection({"session_id": thread_id}))
            finally:
                for name, value in original_paths.items():
                    setattr(common, name, value)

    def test_session_id_only_payload_resolves_when_rollout_exists(self) -> None:
        thread_id = "thread-123"

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sessions_dir = root / "sessions"
            log_dir = sessions_dir / "2026" / "04" / "22"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / f"rollout-2026-04-22T10-00-00-{thread_id}.jsonl"
            log_path.write_text("", encoding="utf-8")

            original_sessions_dir = common.SESSIONS_DIR
            try:
                common.SESSIONS_DIR = sessions_dir
                self.assertEqual(current_compaction_thread_id({"session_id": thread_id}), thread_id)
                self.assertFalse(compaction_state_has_thread(thread_id))
            finally:
                common.SESSIONS_DIR = original_sessions_dir

    def test_session_id_only_payload_resolves_when_state_exists(self) -> None:
        thread_id = "thread-123"

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            state_dir = root / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            state_file = state_dir / "compaction_scan_state.json"
            state_file.write_text(
                json.dumps({"threads": {thread_id: {"pending_compaction_reinjection": True}}}),
                encoding="utf-8",
            )

            original_state_file = common.COMPACTION_STATE_FILE
            original_sessions_dir = common.SESSIONS_DIR
            try:
                common.COMPACTION_STATE_FILE = state_file
                common.SESSIONS_DIR = root / "missing-sessions"
                self.assertTrue(compaction_state_has_thread(thread_id))
                self.assertEqual(current_compaction_thread_id({"session_id": thread_id}), thread_id)
            finally:
                common.COMPACTION_STATE_FILE = original_state_file
                common.SESSIONS_DIR = original_sessions_dir

    def test_model_summary_prompt_preserves_generic_retrieval_anchors(self) -> None:
        captured: dict[str, object] = {}

        def fake_call(instructions: str, content: list[dict[str, object]], config: dict[str, object]) -> str:
            captured["instructions"] = instructions
            captured["content"] = content
            return "summary"

        original_call = common.call_openai_responses
        try:
            common.call_openai_responses = fake_call  # type: ignore[assignment]
            result = common.generate_model_summary(
                [{"role": "user", "content": "Book dinner at Via Roma on June 9 and keep /tmp/menu.pdf."}],
                "temporary",
                "Earlier context",
                {},
            )
        finally:
            common.call_openai_responses = original_call

        self.assertEqual(result, "summary")
        instructions = str(captured["instructions"])
        content_text = "\n\n".join(str(block.get("text", "")) for block in captured["content"])  # type: ignore[index,union-attr]
        self.assertIn("people, places, organizations", instructions)
        self.assertIn("prices, amounts, confirmation numbers", instructions)
        self.assertIn("commit hashes, issue or PR numbers", instructions)
        self.assertIn('absolute file path beginning with "/"', instructions)
        self.assertIn("Summarize ONLY the raw conversation segment", content_text)
        self.assertIn("Do not summarize this context", content_text)

    def test_consolidated_summary_uses_shared_dense_summary_task(self) -> None:
        captured: dict[str, object] = {}

        def fake_call(instructions: str, content: list[dict[str, object]], config: dict[str, object]) -> str:
            captured["instructions"] = instructions
            captured["content"] = content
            return "summary"

        original_call = common.call_openai_responses
        try:
            common.call_openai_responses = fake_call  # type: ignore[assignment]
            result = common.generate_model_summary(
                [{"role": "assistant", "content": "The plan changed from A to B."}],
                "consolidated",
                "",
                {},
            )
        finally:
            common.call_openai_responses = original_call

        self.assertEqual(result, "summary")
        content_text = "\n\n".join(str(block.get("text", "")) for block in captured["content"])  # type: ignore[index,union-attr]
        self.assertIn("larger historical conversation span", content_text)
        self.assertIn("Maximize useful memory per token", content_text)
        self.assertIn("do not generalize away concrete details", content_text)

    def test_meta_summary_prompt_preserves_evolution_and_daily_life_details(self) -> None:
        captured: dict[str, object] = {}

        def fake_call(instructions: str, content: list[dict[str, object]], config: dict[str, object]) -> str:
            captured["instructions"] = instructions
            captured["content"] = content
            return "summary"

        original_call = common.call_openai_responses
        try:
            common.call_openai_responses = fake_call  # type: ignore[assignment]
            result = common.generate_model_summary(
                [{"role": "summary", "content": "The user preferred morning appointments, then changed to afternoons."}],
                "meta",
                "",
                {},
            )
        finally:
            common.call_openai_responses = original_call

        self.assertEqual(result, "summary")
        content_text = "\n\n".join(str(block.get("text", "")) for block in captured["content"])  # type: ignore[index,union-attr]
        self.assertIn("preserve changes over time", content_text)
        self.assertIn("recurring preferences", content_text)
        self.assertIn("people, places, organizations", content_text)

    def test_meta_rollover_keeps_recent_consolidated_summaries_visible(self) -> None:
        consolidated = [
            {"summary_type": "consolidated", "content": f"summary {index}", "covers_from": str(index)}
            for index in range(7)
        ]

        to_keep, overflow = common.split_consolidated_for_meta(consolidated, 5)

        self.assertEqual([entry["covers_from"] for entry in overflow], ["0", "1"])
        self.assertEqual([entry["covers_from"] for entry in to_keep], ["2", "3", "4", "5", "6"])

    def test_model_summary_failure_does_not_create_deterministic_fallback(self) -> None:
        def fake_call(instructions: str, content: list[dict[str, object]], config: dict[str, object]) -> str | None:
            return None

        original_call = common.call_openai_responses
        try:
            common.call_openai_responses = fake_call  # type: ignore[assignment]
            summary, pending_id = common.summarize_entries(
                [{"role": "user", "content": "Important project detail. " * 200}],
                "temporary",
                {
                    "enable_model_summaries": True,
                    "openai_api_key": "test-key",
                    "minimum_model_summary_words": 100,
                },
            )
        finally:
            common.call_openai_responses = original_call

        self.assertIsNone(summary)
        self.assertIsNone(pending_id)

    def test_model_summary_too_short_is_rejected(self) -> None:
        def fake_call(instructions: str, content: list[dict[str, object]], config: dict[str, object]) -> str:
            return "Too short."

        original_call = common.call_openai_responses
        try:
            common.call_openai_responses = fake_call  # type: ignore[assignment]
            summary, _ = common.summarize_entries(
                [{"role": "user", "content": "Important project detail. " * 200}],
                "temporary",
                {
                    "enable_model_summaries": True,
                    "openai_api_key": "test-key",
                    "minimum_model_summary_words": 100,
                },
            )
        finally:
            common.call_openai_responses = original_call

        self.assertIsNone(summary)

    def test_deterministic_summary_still_used_when_model_summaries_disabled(self) -> None:
        summary, pending_id = common.summarize_entries(
            [{"role": "user", "content": "Use deterministic summaries when models are disabled."}],
            "temporary",
            {"enable_model_summaries": False},
        )

        self.assertIsNotNone(summary)
        self.assertIn("Temporary summary over 1 archived entries", summary or "")
        self.assertIsNone(pending_id)

    def test_user_context_extraction_prompt_includes_budget_and_durable_work_context(self) -> None:
        captured: dict[str, object] = {}

        def fake_call(instructions: str, content: list[dict[str, object]], config: dict[str, object]) -> str:
            captured["instructions"] = instructions
            captured["content"] = content
            return "- The user maintains a long-running Codex Telegram bridge project."

        with tempfile.TemporaryDirectory() as tmpdir:
            facts_path = Path(tmpdir) / "user_facts.jsonl"
            facts_path.write_text(
                json.dumps({"id": "a", "fact": "The user prefers concise technical explanations."}) + "\n",
                encoding="utf-8",
            )
            original_facts_file = common.FACTS_FILE
            original_call = common.call_openai_responses
            try:
                common.FACTS_FILE = facts_path
                common.call_openai_responses = fake_call  # type: ignore[assignment]
                result = common.extract_user_facts_from_chunk(
                    [{"role": "user", "content": "Remember that my Codex plugins repo is a long-running project."}],
                    {
                        "enable_user_facts": True,
                        "enable_model_user_facts": True,
                        "openai_api_key": "test-key",
                        "user_facts_max_chars": 16000,
                    },
                )
            finally:
                common.FACTS_FILE = original_facts_file
                common.call_openai_responses = original_call

        self.assertIn("Codex Telegram bridge", result)
        instructions = str(captured["instructions"])
        content_text = "\n\n".join(str(block.get("text", "")) for block in captured["content"])  # type: ignore[index,union-attr]
        self.assertIn("durable user context", instructions)
        self.assertIn("long-running projects", instructions)
        self.assertIn("Do not discard something merely because it mentions tools", instructions)
        self.assertIn("PERSISTENT USER-CONTEXT BUDGET", content_text)
        self.assertIn("Total budget: about 16000 characters", content_text)
        self.assertIn("Remaining before cleanup", content_text)
        self.assertIn("EXISTING USER CONTEXT FOR DEDUPLICATION", content_text)

    def test_user_context_condense_prompt_uses_budget_without_dropping_technical_context(self) -> None:
        captured: dict[str, object] = {}

        def fake_call(instructions: str, content: list[dict[str, object]], config: dict[str, object]) -> str:
            captured["instructions"] = instructions
            captured["content"] = content
            return "- The user maintains a long-running Codex plugins repo and prefers AGENTS.md memory transport."

        with tempfile.TemporaryDirectory() as tmpdir:
            facts_path = Path(tmpdir) / "user_facts.jsonl"
            facts = [
                "The user maintains a long-running Codex plugins repo.",
                "The user prefers AGENTS.md memory transport for Codex memory.",
                "The user prefers AGENTS.md memory transport for Codex memory.",
            ]
            facts_path.write_text(
                "".join(json.dumps({"id": str(index), "fact": fact}) + "\n" for index, fact in enumerate(facts)),
                encoding="utf-8",
            )
            original_facts_file = common.FACTS_FILE
            original_call = common.call_openai_responses
            try:
                common.FACTS_FILE = facts_path
                common.call_openai_responses = fake_call  # type: ignore[assignment]
                common.condense_user_facts_if_needed(
                    {
                        "enable_model_user_facts": True,
                        "openai_api_key": "test-key",
                        "user_facts_max_chars": 80,
                    }
                )
                updated = facts_path.read_text(encoding="utf-8")
            finally:
                common.FACTS_FILE = original_facts_file
                common.call_openai_responses = original_call

        instructions = str(captured["instructions"])
        content_text = "\n\n".join(str(block.get("text", "")) for block in captured["content"])  # type: ignore[index,union-attr]
        self.assertIn("limited persistent user-context memory", instructions)
        self.assertIn("Do not delete durable technical/work/project context", instructions)
        self.assertIn("PERSISTENT USER-CONTEXT BUDGET", content_text)
        self.assertIn("Target: compress the fact list back under about 80 characters", content_text)
        self.assertIn("FACT LIST TO CLEANUP", content_text)
        self.assertIn("AGENTS.md memory transport", updated)

    def test_lock_loser_does_not_delete_active_worker_pid(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_file = Path(tmpdir) / "memory-maintenance.pid"
            pid_file.write_text("424242", encoding="utf-8")
            with mock.patch.object(common, "MAINTENANCE_PID_FILE", pid_file), mock.patch.object(
                common, "ensure_state_dir"
            ), mock.patch.object(common, "state_lock", side_effect=BlockingIOError):
                common.run_memory_maintenance_worker()
            self.assertEqual(pid_file.read_text(encoding="utf-8"), "424242")

    def test_maintenance_parks_and_alerts_after_bounded_no_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            pending = root / "pending"
            pending.mkdir()
            task_file = pending / "memory-maintenance.json"
            task_file.write_text(json.dumps({"generation": 1}), encoding="utf-8")
            alert_file = pending / "memory-maintenance.stuck.json"
            pid_file = pending / "memory-maintenance.pid"
            lock_file = pending / "memory-maintenance.lock"
            config = {
                **common.DEFAULT_CONFIG,
                "maintenance_max_consecutive_failures": 2,
                "pending_retry_base_seconds": 1,
                "pending_retry_max_seconds": 1,
            }
            with mock.patch.object(common, "MAINTENANCE_TASK_FILE", task_file), mock.patch.object(
                common, "MAINTENANCE_ALERT_FILE", alert_file
            ), mock.patch.object(common, "MAINTENANCE_PID_FILE", pid_file), mock.patch.object(
                common, "MAINTENANCE_LOCK_FILE", lock_file
            ), mock.patch.object(common, "ensure_state_dir"), mock.patch.object(
                common, "load_config", return_value=config
            ), mock.patch.object(common, "run_memory_maintenance_once"), mock.patch.object(
                common, "memory_maintenance_needed", return_value=True
            ), mock.patch.object(common, "maintenance_progress_signature", return_value="unchanged"), mock.patch.object(
                common.time, "sleep"
            ):
                common.run_memory_maintenance_worker()

            alert = json.loads(alert_file.read_text(encoding="utf-8"))
            self.assertEqual(alert["status"], "stuck")
            self.assertEqual(alert["consecutive_failures"], 2)
            self.assertIn("no progress", alert["last_error"])
            self.assertFalse(pid_file.exists())
            with mock.patch.object(common, "MAINTENANCE_ALERT_FILE", alert_file):
                self.assertIn("Inform the user", common.memory_maintenance_alert_context())

    def test_memory_api_failure_records_actionable_health(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            health_file = Path(tmpdir) / "health.json"
            with mock.patch.object(common, "MEMORY_HEALTH_FILE", health_file), mock.patch.object(
                common, "urlopen", side_effect=common.URLError("network offline")
            ), mock.patch.object(common.time, "sleep"):
                result = common.call_openai_responses(
                    "instructions",
                    [{"type": "input_text", "text": "source"}],
                    {**common.DEFAULT_CONFIG, "openai_api_key": "sk-test", "openai_timeout_seconds": 1},
                    max_retries=1,
                )
            self.assertIsNone(result)
            health = json.loads(health_file.read_text(encoding="utf-8"))
            self.assertEqual(health["status"], "error")
            self.assertEqual(health["category"], "network")
            self.assertIn("Could not reach OpenAI", health["detail"])

    def test_retry_memory_cli_clears_alert_and_reschedules(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            alert_file = Path(tmpdir) / "stuck.json"
            alert_file.write_text("{}", encoding="utf-8")
            with mock.patch.object(common, "MAINTENANCE_ALERT_FILE", alert_file), mock.patch.object(
                common, "load_config", return_value=common.DEFAULT_CONFIG
            ), mock.patch.object(common, "schedule_memory_maintenance") as schedule:
                self.assertEqual(common.main(["common.py", "--retry-memory-maintenance"]), 0)
            self.assertFalse(alert_file.exists())
            schedule.assert_called_once()


if __name__ == "__main__":
    unittest.main()
