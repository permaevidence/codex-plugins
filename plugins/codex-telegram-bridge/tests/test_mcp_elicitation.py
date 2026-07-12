import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path


BRIDGE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "telegram_bridge.py"
OPERATOR_PATH = Path(__file__).resolve().parents[1] / "scripts" / "bridge.py"
COMMON_PATH = Path(__file__).resolve().parents[1] / "lib" / "telegram_common.py"
MCP_PATH = Path(__file__).resolve().parents[1] / "scripts" / "telegram_actions_mcp.py"


def load_bridge_module():
    spec = importlib.util.spec_from_file_location("telegram_bridge", BRIDGE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def load_operator_module():
    spec = importlib.util.spec_from_file_location("bridge_operator", OPERATOR_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def load_common_module():
    spec = importlib.util.spec_from_file_location("telegram_common_test", COMMON_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def load_mcp_module():
    spec = importlib.util.spec_from_file_location("telegram_actions_mcp_test", MCP_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class McpElicitationTests(unittest.TestCase):
    def test_mcp_elicitation_is_accepted(self):
        bridge = load_bridge_module()

        class FakeClient(bridge.CodexAppServerClient):
            def __init__(self):
                self.sent = []

            def _send_json(self, payload):
                self.sent.append(payload)

        client = FakeClient()
        client._handle_server_request(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "mcpServer/elicitation/request",
                "params": {},
            }
        )

        self.assertEqual(
            client.sent,
            [{"jsonrpc": "2.0", "id": 7, "result": {"action": "accept"}}],
        )


class AppServerResilienceTests(unittest.TestCase):
    def test_real_app_server_exit_terminates_bridge_for_supervisor_restart(self):
        bridge = load_bridge_module()

        class EmptyStdout:
            def __iter__(self):
                return iter(())

        class FakeProcess:
            stdout = EmptyStdout()

        client = object.__new__(bridge.CodexAppServerClient)
        client.process = FakeProcess()
        client._pending = {}
        client._restart_on_exit = True
        with mock.patch.object(bridge.os, "_exit") as exit_process:
            client._read_loop()
        exit_process.assert_called_once_with(75)

    def test_turn_omits_model_overrides_when_codex_defaults_are_unresolved(self):
        bridge = load_bridge_module()
        client = object.__new__(bridge.CodexAppServerClient)
        client.config = {
            "approval_policy": "never",
            "default_cwd": "/tmp",
            "personality": "friendly",
            "sandbox_mode": "dangerFullAccess",
            "model": None,
            "effort": None,
        }
        params = client._turn_params("thread", "hello")
        self.assertNotIn("model", params)
        self.assertNotIn("effort", params)


class TurnRecoveryTests(unittest.TestCase):
    def test_rate_limit_retry_uses_exhausted_window_reset(self):
        bridge = load_bridge_module()
        retry_at = bridge.rate_limit_retry_at(
            {
                "rateLimits": {
                    "primary": {"usedPercent": 100, "resetsAt": 2000},
                    "secondary": {"usedPercent": 36, "resetsAt": 9000},
                }
            },
            now=1000,
            buffer_seconds=60,
        )
        self.assertEqual(retry_at, 2060)

    def test_rate_limit_retry_returns_none_when_capacity_is_available(self):
        bridge = load_bridge_module()
        self.assertIsNone(
            bridge.rate_limit_retry_at(
                {"rateLimits": {"primary": {"usedPercent": 99, "resetsAt": 2000}}},
                now=1000,
            )
        )

    def test_recovery_prompt_preserves_original_request_and_resume_guardrails(self):
        bridge = load_bridge_module()
        prompt = bridge.recovery_prompt({"original_input": "Fix all nine plugin issues."})
        self.assertIn("Fix all nine plugin issues.", prompt)
        self.assertIn("do not repeat actions that already completed", prompt)
        self.assertIn("Finish all remaining work", prompt)

    def test_empty_completed_turn_is_durably_queued(self):
        bridge = load_bridge_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            recovery_file = Path(tmpdir) / "turn_recovery_queue.json"
            client = object.__new__(bridge.CodexAppServerClient)
            client.config = {"enable_turn_recovery": True}
            client._recovery_lock = bridge.threading.Lock()
            client._active_turn_by_chat = {"123": "turn-1"}
            client._turns = {
                "turn-1": {
                    "chat_id": "123",
                    "thread_id": "thread-1",
                    "text": "",
                    "input_text": "Do the durable task",
                    "recovery_id": "recovery-1",
                    "generated_images_seen": set(),
                }
            }
            sent = []
            client.send_callback = lambda chat_id, text, files=None: sent.append((chat_id, text, files))
            with mock.patch.object(bridge, "TURN_RECOVERY_FILE", recovery_file), mock.patch.object(
                bridge, "list_generated_images", return_value=[]
            ), mock.patch.object(bridge, "load_runtime_state", return_value={}), mock.patch.object(
                bridge, "save_runtime_state"
            ):
                client._finish_turn("turn-1", {"id": "turn-1", "status": "completed"})

            records = json.loads(recovery_file.read_text(encoding="utf-8"))
            self.assertEqual(records[0]["id"], "recovery-1")
            self.assertEqual(records[0]["original_input"], "Do the durable task")
            self.assertEqual(records[0]["state"], "pending")
            self.assertIn("resume automatically", sent[0][1])

    def test_successful_recovery_removes_durable_record(self):
        bridge = load_bridge_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            recovery_file = Path(tmpdir) / "turn_recovery_queue.json"
            recovery_file.write_text(json.dumps([{"id": "recovery-1", "chat_id": "123"}]), encoding="utf-8")
            client = object.__new__(bridge.CodexAppServerClient)
            client.config = {"enable_turn_recovery": True}
            client._recovery_lock = bridge.threading.Lock()
            client._active_turn_by_chat = {"123": "turn-2"}
            client._turns = {
                "turn-2": {
                    "chat_id": "123",
                    "thread_id": "thread-1",
                    "text": "Finished safely.",
                    "input_text": "Do the durable task",
                    "recovery_id": "recovery-1",
                    "generated_images_seen": set(),
                }
            }
            sent = []
            client.send_callback = lambda chat_id, text, files=None: sent.append((chat_id, text, files))
            with mock.patch.object(bridge, "TURN_RECOVERY_FILE", recovery_file), mock.patch.object(
                bridge, "list_generated_images", return_value=[]
            ), mock.patch.object(bridge, "load_runtime_state", return_value={}), mock.patch.object(
                bridge, "save_runtime_state"
            ):
                client._finish_turn("turn-2", {"id": "turn-2", "status": "completed"})

            self.assertEqual(json.loads(recovery_file.read_text(encoding="utf-8")), [])
            self.assertEqual(sent[0][1], "Finished safely.")

    def test_recovery_parks_after_maximum_attempts(self):
        bridge = load_bridge_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            recovery_file = Path(tmpdir) / "turn_recovery_queue.json"
            recovery_file.write_text(
                json.dumps(
                    [
                        {
                            "id": "recovery-1",
                            "chat_id": "123",
                            "thread_id": "thread-1",
                            "attempts": 5,
                            "original_input": "Do the task",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            client = object.__new__(bridge.CodexAppServerClient)
            client.config = {"enable_turn_recovery": True, "turn_recovery_max_attempts": 5}
            client._recovery_lock = bridge.threading.Lock()
            state = {
                "chat_id": "123",
                "thread_id": "thread-1",
                "turn_id": "turn-5",
                "input_text": "Do the task",
                "recovery_id": "recovery-1",
            }
            with mock.patch.object(bridge, "TURN_RECOVERY_FILE", recovery_file):
                self.assertEqual(client._queue_recovery(state, "still empty"), "parked")
            record = json.loads(recovery_file.read_text(encoding="utf-8"))[0]
            self.assertEqual(record["state"], "parked")
            self.assertIsNone(record["due_at"])

    def test_explicit_resume_requeues_parked_recovery(self):
        bridge = load_bridge_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            recovery_file = Path(tmpdir) / "turn_recovery_queue.json"
            recovery_file.write_text(
                json.dumps([{"id": "recovery-1", "chat_id": "123", "state": "parked", "attempts": 5}]),
                encoding="utf-8",
            )
            client = object.__new__(bridge.CodexAppServerClient)
            client._recovery_lock = bridge.threading.Lock()
            with mock.patch.object(bridge, "TURN_RECOVERY_FILE", recovery_file):
                self.assertTrue(client.retry_parked_recovery("123"))
            record = json.loads(recovery_file.read_text(encoding="utf-8"))[0]
            self.assertEqual(record["state"], "pending")
            self.assertEqual(record["attempts"], 0)


class ModelDefaultTests(unittest.TestCase):
    def test_first_start_inherits_and_persists_codex_defaults(self):
        common = load_common_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_file = root / "config.json"
            with mock.patch.object(common, "STATE_DIR", root), mock.patch.object(
                common, "INBOX_DIR", root / "inbox"
            ), mock.patch.object(common, "CONFIG_FILE", config_file), mock.patch.object(
                common, "ENV_FILE", root / ".env"
            ), mock.patch.object(
                common, "codex_model_defaults", return_value=("gpt-codex", "xhigh")
            ):
                config = common.load_config()
            self.assertEqual(config["model"], "gpt-codex")
            self.assertEqual(config["effort"], "xhigh")
            written = json.loads(config_file.read_text(encoding="utf-8"))
            self.assertEqual(written, {"model": "gpt-codex", "effort": "xhigh"})

    def test_saved_bridge_selection_wins_on_restart(self):
        common = load_common_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_file = root / "config.json"
            config_file.write_text(json.dumps({"model": "gpt-saved", "effort": "high"}), encoding="utf-8")
            with mock.patch.object(common, "STATE_DIR", root), mock.patch.object(
                common, "INBOX_DIR", root / "inbox"
            ), mock.patch.object(common, "CONFIG_FILE", config_file), mock.patch.object(
                common, "ENV_FILE", root / ".env"
            ), mock.patch.object(common, "codex_model_defaults") as defaults:
                config = common.load_config()
            defaults.assert_not_called()
            self.assertEqual((config["model"], config["effort"]), ("gpt-saved", "high"))

    def test_pending_requests_are_failed_when_app_server_exits(self):
        bridge = load_bridge_module()

        class EmptyStdout:
            def __iter__(self):
                return iter(())

        class FakeProcess:
            stdout = EmptyStdout()

        client = object.__new__(bridge.CodexAppServerClient)
        pending = bridge.queue.Queue(maxsize=1)
        client.process = FakeProcess()
        client._pending = {9: pending}
        client._read_loop()
        result = pending.get_nowait()
        self.assertIn("error", result)
        self.assertEqual(client._pending, {})


class LaunchAgentTests(unittest.TestCase):
    def test_install_service_writes_persistent_launch_agent(self):
        operator = load_operator_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            plist = root / "LaunchAgents/service.plist"
            state = root / "state"
            with mock.patch.object(operator, "LAUNCH_AGENTS_DIR", plist.parent), mock.patch.object(
                operator, "LAUNCH_AGENT_FILE", plist
            ), mock.patch.object(operator, "STATE_DIR", state), mock.patch.object(
                operator, "stop_bridge", return_value=0
            ), mock.patch.object(operator, "start_launch_service", return_value=0):
                self.assertEqual(operator.install_service(), 0)
            payload = operator.plistlib.loads(plist.read_bytes())
            self.assertTrue(payload["RunAtLoad"])
            self.assertTrue(payload["KeepAlive"])
            self.assertEqual(payload["Label"], operator.SERVICE_LABEL)


class BotCommandMenuTests(unittest.TestCase):
    def test_bot_commands_are_reflected_in_help_text(self):
        bridge = load_bridge_module()
        help_text = bridge.help_text()

        self.assertEqual(
            [item["command"] for item in bridge.BOT_COMMANDS],
            ["start", "help", "status", "health", "model", "resume", "retrymemory", "stop", "newsession", "update"],
        )
        for item in bridge.BOT_COMMANDS:
            self.assertIn(f"/{item['command']} - {item['description']}", help_text)

    def test_configure_bot_command_menu_sets_commands(self):
        bridge = load_bridge_module()
        calls = []

        def fake_set_bot_commands(token, commands):
            calls.append((token, commands))
            return {"ok": True}

        original = bridge.set_bot_commands
        bridge.set_bot_commands = fake_set_bot_commands
        try:
            self.assertTrue(bridge.configure_bot_command_menu("token"))
        finally:
            bridge.set_bot_commands = original

        self.assertEqual(calls, [("token", bridge.BOT_COMMANDS)])

    def test_configure_bot_command_menu_failure_is_nonfatal(self):
        bridge = load_bridge_module()

        def fake_set_bot_commands(_token, _commands):
            raise RuntimeError("network down")

        original = bridge.set_bot_commands
        bridge.set_bot_commands = fake_set_bot_commands
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertFalse(bridge.configure_bot_command_menu("token"))
        finally:
            bridge.set_bot_commands = original


class ModelSelectionTests(unittest.TestCase):
    def test_save_model_selection_updates_only_model_fields(self):
        bridge = load_bridge_module()

        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "config.json"
            config_file.write_text(
                json.dumps(
                    {
                        "default_cwd": "/tmp/project",
                        "bot_token": "should-stay-existing-only",
                        "model": "gpt-5.5",
                        "effort": "high",
                    }
                ),
                encoding="utf-8",
            )
            config = {
                "model": "gpt-5.5",
                "effort": "high",
                "openai_api_key": "env-secret-that-must-not-be-written",
            }
            original = bridge.CONFIG_FILE
            bridge.CONFIG_FILE = config_file
            try:
                bridge.save_model_selection(config, "gpt-5.6-sol", "xhigh")
            finally:
                bridge.CONFIG_FILE = original

            written = json.loads(config_file.read_text(encoding="utf-8"))
            self.assertEqual(written["model"], "gpt-5.6-sol")
            self.assertEqual(written["effort"], "xhigh")
            self.assertEqual(written["default_cwd"], "/tmp/project")
            self.assertEqual(written["bot_token"], "should-stay-existing-only")
            self.assertNotIn("openai_api_key", written)
            self.assertEqual(config["model"], "gpt-5.6-sol")
            self.assertEqual(config["effort"], "xhigh")

    def test_model_keyboards_use_codex_catalog_fields(self):
        bridge = load_bridge_module()
        models = [
            {
                "slug": "gpt-5.6-sol",
                "display_name": "GPT-5.6-Sol",
                "efforts": ["low", "high", "ultra"],
            }
        ]

        self.assertEqual(
            bridge.model_keyboard(models),
            {
                "inline_keyboard": [
                    [{"text": "GPT-5.6-Sol", "callback_data": "model:choose:gpt-5.6-sol"}]
                ]
            },
        )
        self.assertEqual(
            bridge.effort_keyboard(models[0]),
            {
                "inline_keyboard": [
                    [
                        {"text": "low", "callback_data": "model:effort:gpt-5.6-sol:low"},
                        {"text": "high", "callback_data": "model:effort:gpt-5.6-sol:high"},
                        {"text": "ultra", "callback_data": "model:effort:gpt-5.6-sol:ultra"},
                    ]
                ]
            },
        )

    def test_model_callback_selects_effort_and_persists(self):
        bridge = load_bridge_module()

        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "config.json"
            config_file.write_text(json.dumps({"model": "gpt-5.5", "effort": "high"}), encoding="utf-8")
            config = {"codex_cmd": "codex", "model": "gpt-5.5", "effort": "high"}
            access = {"allowFrom": ["123"]}
            calls = []

            def fake_models(_codex_cmd):
                return [
                    {
                        "slug": "gpt-5.6-sol",
                        "display_name": "GPT-5.6-Sol",
                        "efforts": ["low", "high"],
                    }
                ]

            def fake_answer(*args):
                calls.append(("answer", args))

            def fake_edit(*args, **kwargs):
                calls.append(("edit", args, kwargs))

            original_config = bridge.CONFIG_FILE
            original_models = bridge.list_codex_models
            original_answer = bridge.answer_callback_query
            original_edit = bridge.edit_message_text
            bridge.CONFIG_FILE = config_file
            bridge.list_codex_models = fake_models
            bridge.answer_callback_query = fake_answer
            bridge.edit_message_text = fake_edit
            try:
                bridge.handle_model_callback(
                    "token",
                    {
                        "id": "cb1",
                        "data": "model:effort:gpt-5.6-sol:low",
                        "from": {"id": 123},
                        "message": {"message_id": 77, "chat": {"id": "1641309608"}},
                    },
                    config,
                    access,
                )
            finally:
                bridge.CONFIG_FILE = original_config
                bridge.list_codex_models = original_models
                bridge.answer_callback_query = original_answer
                bridge.edit_message_text = original_edit

            written = json.loads(config_file.read_text(encoding="utf-8"))
            self.assertEqual(written["model"], "gpt-5.6-sol")
            self.assertEqual(written["effort"], "low")
            self.assertEqual(config["model"], "gpt-5.6-sol")
            self.assertEqual(config["effort"], "low")
            self.assertEqual(calls[0], ("answer", ("token", "cb1", "Model updated.")))
            self.assertEqual(calls[1][0], "edit")
            self.assertIn("gpt-5.6-sol / low", calls[1][1][3])


class SafetyAndReminderTests(unittest.TestCase):
    def test_user_input_auto_answer_requires_recommended_or_explicit_default(self):
        bridge = load_bridge_module()
        unsafe = {"questions": [{"id": "choice", "options": [{"label": "Delete"}, {"label": "Keep"}]}]}
        safe = {"questions": [{"id": "choice", "options": [{"label": "Keep (Recommended)"}, {"label": "Delete"}]}]}
        self.assertEqual(bridge.build_auto_user_input_answers(unsafe), {})
        self.assertEqual(bridge.build_auto_user_input_answers(safe), {"choice": "Keep (Recommended)"})

    def test_monthly_reminders_use_calendar_months_and_skip_missed_intervals(self):
        bridge = load_bridge_module()
        after = bridge.time.mktime(bridge.time.strptime("2026-01-31T09:00:01", "%Y-%m-%dT%H:%M:%S"))
        self.assertEqual(
            bridge.advance_recurring("2026-01-31T09:00:00", "monthly", after=after),
            "2026-02-28T09:00:00",
        )
        far_after = bridge.time.mktime(bridge.time.strptime("2026-07-10T09:00:00", "%Y-%m-%dT%H:%M:%S"))
        self.assertEqual(
            bridge.advance_recurring("2026-07-01T09:00:00", "daily", after=far_after),
            "2026-07-11T09:00:00",
        )

    def test_mcp_rejects_unauthorized_and_ambiguous_chat_destinations(self):
        mcp = load_mcp_module()
        access = {"allowFrom": ["1", "2"], "groups": {"-100": {}}}
        self.assertEqual(mcp.authorized_chat_ids(access), {"1", "2", "-100"})
        with self.assertRaises(RuntimeError):
            mcp.require_authorized_chat(access, "999")
        with self.assertRaises(RuntimeError):
            mcp.resolve_chat(None, "")


if __name__ == "__main__":
    unittest.main()


class UpdateAnnouncementTests(unittest.TestCase):
    def test_completed_update_produces_owner_notification_once(self):
        bridge = load_bridge_module()
        state = {"status": "completed", "commit": "44eeb81f07ec86e72ad464e7dd6f0ac660eaa0fd", "announced": False}
        text = bridge.update_outcome_message(state)
        self.assertIsNotNone(text)
        self.assertIn("44eeb81f07ec", text)
        self.assertIn("/newsession", text)
        state["announced"] = True
        self.assertIsNone(bridge.update_outcome_message(state))

    def test_failed_update_reports_error_and_rollback(self):
        bridge = load_bridge_module()
        text = bridge.update_outcome_message(
            {"status": "failed", "ref": "main", "error": "doctor found failing checks", "announced": False}
        )
        self.assertIsNotNone(text)
        self.assertIn("FAILED", text)
        self.assertIn("doctor found failing checks", text)
        self.assertIn("previous runtime was restored", text)

    def test_running_or_empty_state_stays_silent(self):
        bridge = load_bridge_module()
        self.assertIsNone(bridge.update_outcome_message({}))
        self.assertIsNone(bridge.update_outcome_message({"status": "running", "announced": False}))

    def test_update_is_a_registered_bot_command(self):
        bridge = load_bridge_module()
        self.assertIn("update", {item["command"] for item in bridge.BOT_COMMANDS})


class HealthVisibilityTests(unittest.TestCase):
    def test_transcription_retries_transient_failure_then_succeeds(self):
        bridge = load_bridge_module()

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"text":"Recovered transcription"}'

        with tempfile.TemporaryDirectory() as tmpdir:
            health_file = Path(tmpdir) / "health.json"
            notices = []
            with mock.patch.object(bridge, "HEALTH_STATE_FILE", health_file), mock.patch.object(
                bridge.urllib.request,
                "urlopen",
                side_effect=[bridge.URLError("temporary network failure"), Response()],
            ) as urlopen, mock.patch.object(bridge.time, "sleep") as sleep:
                result = bridge.transcribe_audio(
                    b"audio",
                    "voice.mp3",
                    "audio/mpeg",
                    "sk-test",
                    on_notice=notices.append,
                )

            self.assertEqual(result, "Recovered transcription")
            self.assertEqual(urlopen.call_count, 2)
            sleep.assert_called_once_with(1)
            self.assertEqual(notices, [])
            health = json.loads(health_file.read_text(encoding="utf-8"))
            self.assertEqual(health["components"]["transcription"]["status"], "ok")

    def test_transcription_stops_after_three_transient_failures(self):
        bridge = load_bridge_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            health_file = Path(tmpdir) / "health.json"
            notices = []
            with mock.patch.object(bridge, "HEALTH_STATE_FILE", health_file), mock.patch.object(
                bridge.urllib.request,
                "urlopen",
                side_effect=bridge.URLError("temporary network failure"),
            ) as urlopen, mock.patch.object(bridge.time, "sleep") as sleep:
                result = bridge.transcribe_audio(
                    b"audio",
                    "voice.mp3",
                    "audio/mpeg",
                    "sk-test",
                    on_notice=notices.append,
                )

            self.assertIsNone(result)
            self.assertEqual(urlopen.call_count, 3)
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])
            self.assertEqual(len(notices), 1)
            self.assertIn("Could not reach OpenAI", notices[0])

    def test_transcription_failure_is_classified_and_visible(self):
        bridge = load_bridge_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            health_file = Path(tmpdir) / "health.json"
            notices = []
            error = bridge.HTTPError(
                "https://api.openai.com/v1/audio/transcriptions",
                429,
                "Too Many Requests",
                {},
                io.BytesIO(b'{"error":{"message":"insufficient_quota: add billing credit"}}'),
            )
            with mock.patch.object(bridge, "HEALTH_STATE_FILE", health_file), mock.patch.object(
                bridge.urllib.request, "urlopen", side_effect=error
            ):
                result = bridge.transcribe_audio(
                    b"audio",
                    "voice.mp3",
                    "audio/mpeg",
                    "sk-test",
                    on_notice=notices.append,
                )
            self.assertIsNone(result)
            health = json.loads(health_file.read_text(encoding="utf-8"))
            self.assertEqual(health["components"]["transcription"]["category"], "insufficient_credit")
            self.assertIn("insufficient credit", notices[0])

    def test_health_text_reports_parked_memory_and_transcription(self):
        bridge = load_bridge_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            health_file = root / "telegram-health.json"
            memory_health = root / "memory-health.json"
            memory_alert = root / "memory-alert.json"
            memory_task = root / "memory-task.json"
            memory_pid = root / "memory.pid"
            bridge.save_json(
                health_file,
                {"components": {"transcription": {"status": "error", "detail": "API key rejected"}}},
            )
            bridge.save_json(memory_alert, {"last_error": "insufficient_credit: add billing"})
            with mock.patch.object(bridge, "HEALTH_STATE_FILE", health_file), mock.patch.object(
                bridge, "MEMORY_HEALTH_FILE", memory_health
            ), mock.patch.object(bridge, "MEMORY_ALERT_FILE", memory_alert), mock.patch.object(
                bridge, "MEMORY_TASK_FILE", memory_task
            ), mock.patch.object(bridge, "MEMORY_PID_FILE", memory_pid), mock.patch.object(
                bridge, "UPDATE_STATE_FILE", root / "update.json"
            ):
                text = bridge.health_text()
            self.assertIn("Memory summaries: parked", text)
            self.assertIn("Raw conversation is still being saved", text)
            self.assertIn("Voice transcription: API key rejected", text)

    def test_non_usage_codex_failure_is_saved_and_parked(self):
        bridge = load_bridge_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            recovery_file = Path(tmpdir) / "recovery.json"
            recovery_file.write_text("[]", encoding="utf-8")
            client = object.__new__(bridge.CodexAppServerClient)
            client.config = {"enable_turn_recovery": True, "turn_recovery_max_attempts": 5}
            client._recovery_lock = bridge.threading.Lock()
            state = {
                "chat_id": "123",
                "thread_id": "thread-1",
                "turn_id": "turn-1",
                "input_text": "Do not lose this task",
            }
            with mock.patch.object(bridge, "TURN_RECOVERY_FILE", recovery_file):
                result = client._queue_recovery(state, "authentication failed", force_park=True)
            record = json.loads(recovery_file.read_text(encoding="utf-8"))[0]
            self.assertEqual(result, "parked")
            self.assertEqual(record["state"], "parked")
            self.assertEqual(record["original_input"], "Do not lose this task")
