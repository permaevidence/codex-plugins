from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import common
from lib.common import (
    current_compaction_thread_id,
    current_thread_id,
    is_context_compacted_event,
    scan_rollout_log_for_compaction,
)


class CompactionDetectionTests(unittest.TestCase):
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

            offset, saw_compaction = scan_rollout_log_for_compaction(path, 0)
            self.assertTrue(saw_compaction)
            self.assertEqual(offset, path.stat().st_size)

            next_offset, saw_again = scan_rollout_log_for_compaction(path, offset)
            self.assertFalse(saw_again)
            self.assertEqual(next_offset, offset)

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

            offset, saw_compaction = scan_rollout_log_for_compaction(path, 0)
            self.assertFalse(saw_compaction)
            self.assertEqual(offset, len(prefix_payload.encode("utf-8")))

            with path.open("a", encoding="utf-8") as handle:
                handle.write(suffix)

            next_offset, saw_after_retry = scan_rollout_log_for_compaction(path, offset)
            self.assertTrue(saw_after_retry)
            self.assertEqual(next_offset, path.stat().st_size)

    def test_compaction_detection_requires_thread_id(self) -> None:
        self.assertEqual(current_thread_id({"session_id": "session-123"}), "session-123")
        self.assertEqual(current_compaction_thread_id({"session_id": "session-123"}), "")
        self.assertEqual(
            current_compaction_thread_id({"thread_id": "thread-123", "session_id": "session-123"}),
            "thread-123",
        )

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

    def test_session_id_only_payload_does_not_trigger_compaction_scan(self) -> None:
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

                self.assertFalse(common.should_reinject_after_compaction({"session_id": thread_id}))
                self.assertFalse(common.consume_compaction_reinjection({"session_id": thread_id}))
                self.assertTrue(common.should_reinject_after_compaction({"thread_id": thread_id}))
            finally:
                for name, value in original_paths.items():
                    setattr(common, name, value)


if __name__ == "__main__":
    unittest.main()
