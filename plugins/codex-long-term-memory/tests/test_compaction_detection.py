from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

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


class CompactionDetectionTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
