import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
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


class TelegramTimeEnvelopeTests(unittest.TestCase):
    def test_channel_envelope_includes_original_human_readable_sent_time(self):
        bridge = load_bridge_module()
        sent = datetime(2026, 7, 13, 9, 53, tzinfo=timezone.utc)
        rendered = bridge.build_channel_message(
            {
                "chat": {"id": 123},
                "from": {"id": 456},
                "message_id": 789,
                "date": int(sent.timestamp()),
            },
            "Hello",
            {},
            "America/New_York",
        )
        self.assertIn(f'ts="{int(sent.timestamp())}"', rendered)
        self.assertIn('sent_at="2026-07-13 05:53 EDT"', rendered)

    def test_invalid_timezone_falls_back_without_dropping_sent_at(self):
        bridge = load_bridge_module()
        rendered = bridge.build_channel_message(
            {"chat": {"id": 1}, "from": {"id": 2}, "message_id": 3, "date": 1},
            "Hello",
            {},
            "Not/A_Zone",
        )
        self.assertIn('sent_at="', rendered)


class GoogleAppOnboardingTests(unittest.TestCase):
    def test_live_bridge_lists_all_codex_app_pages(self):
        bridge = load_bridge_module()
        client = object.__new__(bridge.CodexAppServerClient)
        client.request = mock.Mock(
            side_effect=[
                {
                    "data": [{"id": "gmail-new", "name": "Gmail"}],
                    "nextCursor": "page-2",
                },
                {
                    "data": [{"id": "calendar-new", "displayName": "Google Calendar"}],
                },
            ]
        )
        apps = client.list_apps()
        self.assertEqual(set(apps), {"gmail-new", "calendar-new"})
        self.assertEqual(client.request.call_count, 2)
        self.assertEqual(client.request.call_args_list[1].args[1]["cursor"], "page-2")

    def test_google_app_requires_enabled_and_accessible(self):
        operator = load_operator_module()
        connected, detail = operator.google_app_connection_status(
            {"name": "Gmail", "isEnabled": True, "isAccessible": False}
        )
        self.assertFalse(connected)
        self.assertIn("enabled=True", detail)
        self.assertIn("accessible=False", detail)

    def test_google_app_passes_when_enabled_and_accessible(self):
        operator = load_operator_module()
        connected, detail = operator.google_app_connection_status(
            {"name": "Google Calendar", "isEnabled": True, "isAccessible": True}
        )
        self.assertTrue(connected)
        self.assertIn("accessible=True", detail)

    def test_missing_google_app_is_not_connected(self):
        operator = load_operator_module()
        connected, detail = operator.google_app_connection_status({})
        self.assertFalse(connected)
        self.assertIn("not returned by Codex app/list", detail)

    def test_google_apps_are_discovered_when_connector_ids_change(self):
        operator = load_operator_module()
        apps = {
            "connector_new_mail": {
                "id": "connector_new_mail",
                "name": "Gmail",
                "isEnabled": True,
                "isAccessible": True,
            },
            "connector_new_calendar": {
                "id": "connector_new_calendar",
                "displayName": "Google Calendar",
                "isEnabled": True,
                "isAccessible": True,
            },
        }

        self.assertEqual(
            operator.find_google_app(apps, "gmail")["id"],
            "connector_new_mail",
        )
        self.assertEqual(
            operator.find_google_app(apps, "calendar")["id"],
            "connector_new_calendar",
        )


class BridgeDoctorTests(unittest.TestCase):
    def test_live_child_accepts_resolved_runtime_path(self):
        operator = load_operator_module()
        command = f"/usr/bin/python3 {operator.SCRIPT_DIR / 'telegram_bridge.py'}"
        self.assertTrue(operator.bridge_child_command_valid(command))

    def test_live_child_accepts_stable_current_symlink_path(self):
        operator = load_operator_module()
        current = (
            operator.runtime_data_root()
            / "current"
            / "plugins"
            / "codex-telegram-bridge"
            / "scripts"
            / "telegram_bridge.py"
        )
        command = f"/usr/bin/python3 {current}"
        self.assertTrue(operator.bridge_child_command_valid(command))

    def test_live_child_rejects_unrelated_bridge_script(self):
        operator = load_operator_module()
        self.assertFalse(
            operator.bridge_child_command_valid(
                "/usr/bin/python3 /tmp/codex-telegram-bridge/scripts/telegram_bridge.py"
            )
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

    def test_interrupt_turn_sends_active_thread_and_turn_ids(self):
        bridge = load_bridge_module()
        client = object.__new__(bridge.CodexAppServerClient)
        client._active_turn_by_chat = {"123": "turn-1"}
        client._turns = {"turn-1": {"thread_id": "thread-1"}}
        client.request = mock.Mock(return_value={})

        with mock.patch.object(bridge, "load_runtime_state", return_value={}), mock.patch.object(
            bridge, "save_runtime_state"
        ) as save_state:
            self.assertTrue(client.interrupt_turn("123"))

        client.request.assert_called_once_with(
            "turn/interrupt",
            {"threadId": "thread-1", "turnId": "turn-1"},
        )
        self.assertEqual(save_state.call_args.args[0]["last_turn_status"], "interruptRequested")


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
            ), mock.patch.object(operator, "start_launch_service", return_value=0), mock.patch.object(
                operator, "PLATFORM_FAMILY", "macos"
            ), mock.patch.object(
                operator,
                "ensure_macos_bridge_host",
                return_value={"bundle": Path("/stable/PermaEvidence Codex Bridge.app"), "executable": Path("/stable/bridge-host")},
            ):
                self.assertEqual(operator.install_service(), 0)
            payload = operator.plistlib.loads(plist.read_bytes())
            self.assertTrue(payload["RunAtLoad"])
            self.assertTrue(payload["KeepAlive"])
            self.assertEqual(payload["Label"], operator.SERVICE_LABEL)
            self.assertEqual(payload["ProgramArguments"], ["/stable/bridge-host"])
            self.assertNotIn("start_bridge.sh", " ".join(payload["ProgramArguments"]))


class SystemdServiceTests(unittest.TestCase):
    def test_install_service_writes_persistent_systemd_user_unit(self):
        operator = load_operator_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            unit = root / "systemd/user/bridge.service"
            state = root / "state"
            with mock.patch.object(operator, "PLATFORM_FAMILY", "linux"), mock.patch.object(
                operator, "SYSTEMD_USER_DIR", unit.parent
            ), mock.patch.object(operator, "SYSTEMD_UNIT_FILE", unit), mock.patch.object(
                operator, "STATE_DIR", state
            ), mock.patch.object(operator, "stop_bridge", return_value=0), mock.patch.object(
                operator, "start_systemd_service", return_value=0
            ), mock.patch.object(operator, "enable_linux_linger", return_value=True), mock.patch.object(
                operator.shutil, "which", return_value="/usr/bin/bash"
            ):
                self.assertEqual(operator.install_service(), 0)
            contents = unit.read_text(encoding="utf-8")
            self.assertIn("WantedBy=default.target", contents)
            self.assertIn("Restart=always", contents)
            self.assertIn("start_bridge.sh", contents)
            self.assertIn("KillMode=control-group", contents)

    def test_linux_service_status_uses_systemctl_user(self):
        operator = load_operator_module()
        completed = mock.Mock(returncode=0)
        with mock.patch.object(operator, "PLATFORM_FAMILY", "linux"), mock.patch.object(
            operator.subprocess, "run", return_value=completed
        ) as run:
            self.assertTrue(operator.service_loaded())
        self.assertEqual(
            run.call_args.args[0],
            ["systemctl", "--user", "is-active", "--quiet", operator.SYSTEMD_SERVICE_NAME],
        )

    def test_systemd_path_preserves_spaces_and_escapes_specifiers(self):
        operator = load_operator_module()
        escaped = operator.systemd_path("/home/alice/My Runtime/100% ready")
        self.assertEqual(escaped, "/home/alice/My Runtime/100%% ready")
        self.assertNotIn("\\x20", escaped)
        self.assertNotIn('"', escaped)


class BotCommandMenuTests(unittest.TestCase):
    def test_bot_commands_are_reflected_in_help_text(self):
        bridge = load_bridge_module()
        help_text = bridge.help_text()

        self.assertEqual(
            [item["command"] for item in bridge.BOT_COMMANDS],
            ["start", "help", "status", "health", "model", "resume", "retrymemory", "stop", "newsession", "update", "updatecodex"],
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
        self.assertIn("updatecodex", {item["command"] for item in bridge.BOT_COMMANDS})

    def test_completed_codex_update_reports_verified_stable_paths(self):
        bridge = load_bridge_module()
        text = bridge.codex_update_outcome_message(
            {
                "status": "completed",
                "previous_version": "0.146.0",
                "version": "0.147.0",
                "announced": False,
            }
        )
        self.assertIsNotNone(text)
        self.assertIn("0.146.0 → 0.147.0", text)
        self.assertIn("designated requirements", text)
        self.assertIn("stable permission paths", text)

    def test_failed_codex_update_reports_rollback(self):
        bridge = load_bridge_module()
        text = bridge.codex_update_outcome_message(
            {"status": "failed", "error": "signature mismatch", "announced": False}
        )
        self.assertIsNotNone(text)
        self.assertIn("FAILED", text)
        self.assertIn("signature mismatch", text)
        self.assertIn("previous stable Codex package was restored", text)


class HealthVisibilityTests(unittest.TestCase):
    def test_ogg_transcription_stops_before_api_when_converter_is_missing(self):
        bridge = load_bridge_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            health_file = Path(tmpdir) / "health.json"
            notices = []
            with mock.patch.object(bridge, "HEALTH_STATE_FILE", health_file), mock.patch.object(
                bridge,
                "resolve_ffmpeg_executable",
                return_value=None,
            ), mock.patch.object(bridge.urllib.request, "urlopen") as urlopen:
                result = bridge.transcribe_audio(
                    b"ogg-opus",
                    "voice.ogg",
                    "audio/ogg",
                    "sk-test",
                    on_notice=notices.append,
                )

            self.assertIsNone(result)
            urlopen.assert_not_called()
            health = json.loads(health_file.read_text(encoding="utf-8"))
            self.assertEqual(
                health["components"]["transcription"]["category"],
                "audio_conversion",
            )
            self.assertIn("setup wizard", notices[0])

    def test_ogg_conversion_uses_resolved_ffmpeg_and_returns_mp3(self):
        bridge = load_bridge_module()

        def convert(command, **_kwargs):
            Path(command[-1]).write_bytes(b"mp3")
            return mock.Mock(returncode=0, stderr="")

        with mock.patch.object(
            bridge,
            "resolve_ffmpeg_executable",
            return_value="/private/converter/ffmpeg",
        ), mock.patch.object(bridge.subprocess, "run", side_effect=convert) as run_process:
            converted = bridge.convert_audio_for_transcription(b"ogg", ".ogg")

        self.assertEqual(converted, (b"mp3", "transcription.mp3", "audio/mpeg"))
        self.assertEqual(run_process.call_args.args[0][0], "/private/converter/ffmpeg")

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
            ), mock.patch.object(
                bridge, "ffmpeg_status", return_value=(True, "test converter")
            ):
                text = bridge.health_text()
            self.assertIn("Memory summaries: parked", text)
            self.assertIn("Raw conversation is still being saved", text)
            self.assertIn("Voice transcription: API key rejected", text)

    def test_health_ignores_stale_google_errors_when_background_features_are_disabled(self):
        bridge = load_bridge_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            telegram_config = root / "telegram-config.json"
            memory_dir = root / "memory"
            memory_dir.mkdir()
            bridge.save_json(telegram_config, {"enable_email_notifications": False})
            bridge.save_json(memory_dir / "config.json", {"enable_calendar": False})
            bridge.save_json(
                root / "telegram-health.json",
                {"components": {"email": {"status": "error", "detail": "old IMAP error"}}},
            )
            bridge.save_json(
                memory_dir / "calendar_health.json",
                {"status": "error", "detail": "old calendar error"},
            )
            with mock.patch.object(bridge, "CONFIG_FILE", telegram_config), mock.patch.object(
                bridge, "MEMORY_STATE_DIR", memory_dir
            ), mock.patch.object(
                bridge, "HEALTH_STATE_FILE", root / "telegram-health.json"
            ), mock.patch.object(
                bridge, "MEMORY_HEALTH_FILE", memory_dir / "health.json"
            ), mock.patch.object(
                bridge, "MEMORY_ALERT_FILE", memory_dir / "alert.json"
            ), mock.patch.object(
                bridge, "MEMORY_TASK_FILE", memory_dir / "task.json"
            ), mock.patch.object(
                bridge, "MEMORY_PID_FILE", memory_dir / "worker.pid"
            ), mock.patch.object(
                bridge, "UPDATE_STATE_FILE", root / "update.json"
            ), mock.patch.object(
                bridge, "ffmpeg_status", return_value=(True, "test converter")
            ):
                text = bridge.health_text()
                compact = bridge.compact_health_summary()
            self.assertIn("Email notifications: disabled", text)
            self.assertIn("Calendar context: disabled", text)
            self.assertNotIn("old IMAP error", text)
            self.assertNotIn("old calendar error", text)
            self.assertEqual(compact, "Health: OK")

    def test_google_background_failures_alert_once_and_recovery_alerts_once(self):
        bridge = load_bridge_module()
        notifications = {}
        bridge_config = {"enable_email_notifications": True}
        memory_config = {"enable_calendar": True}
        email_error = {"status": "error", "detail": "authentication failed"}
        calendar_warning = {"status": "warning", "detail": "using cached feed"}

        first = bridge.google_background_alert_messages(
            notifications,
            bridge_config,
            memory_config,
            email_error,
            calendar_warning,
        )
        second = bridge.google_background_alert_messages(
            notifications,
            bridge_config,
            memory_config,
            email_error,
            calendar_warning,
        )
        recovered = bridge.google_background_alert_messages(
            notifications,
            bridge_config,
            memory_config,
            {"status": "ok", "detail": "poll succeeded"},
            {"status": "ok", "detail": "fresh feed"},
        )
        repeated_recovery = bridge.google_background_alert_messages(
            notifications,
            bridge_config,
            memory_config,
            {"status": "ok"},
            {"status": "ok"},
        )

        self.assertEqual(len(first), 2)
        self.assertIn("email checkpoint has not advanced", first[0])
        self.assertIn("cached data", first[1])
        self.assertIn("setup.py", first[0])
        self.assertEqual(second, [])
        self.assertEqual(len(recovered), 2)
        self.assertIn("working again", recovered[0])
        self.assertIn("working again", recovered[1])
        self.assertEqual(repeated_recovery, [])

    def test_calendar_refresh_loop_retries_failure_then_returns_to_normal_interval(self):
        bridge = load_bridge_module()

        class StopLoop(Exception):
            pass

        class ImmediateThread:
            def __init__(self, *, target, **_kwargs):
                self.target = target

            def start(self):
                try:
                    self.target()
                except StopLoop:
                    pass

        with tempfile.TemporaryDirectory() as tmpdir:
            memory_dir = Path(tmpdir)
            bridge.save_json(memory_dir / "config.json", {"enable_calendar": True})
            delays = []

            def fake_sleep(delay):
                delays.append(delay)
                if len(delays) > 1:
                    raise StopLoop()

            with mock.patch.object(bridge, "MEMORY_STATE_DIR", memory_dir), mock.patch.object(
                bridge.threading, "Thread", ImmediateThread
            ), mock.patch.object(
                bridge.time, "sleep", side_effect=fake_sleep
            ), mock.patch.object(
                bridge,
                "update_long_term_memory_agents_file",
                return_value={"status": "ok", "detail": "fresh feed"},
            ) as update_agents:
                bridge.maybe_start_calendar_refresh_loop(
                    {"default_cwd": str(Path.home())},
                    {"status": "error", "detail": "temporary timeout"},
                )

        self.assertEqual(
            delays,
            [bridge.CALENDAR_FAILURE_RETRY_INTERVAL, bridge.CALENDAR_REFRESH_INTERVAL],
        )
        self.assertEqual(
            bridge.calendar_refresh_delay({"status": "warning"}),
            bridge.CALENDAR_FAILURE_RETRY_INTERVAL,
        )
        update_agents.assert_called_once()

    def test_email_polling_waits_one_minute_after_failures_and_alerts_after_second(self):
        bridge = load_bridge_module()
        statuses = []
        delays = []
        outcomes = [
            bridge.GmailImapError("Could not reach Gmail IMAP: timeout"),
            bridge.GmailImapError("Could not reach Gmail IMAP: timeout"),
            ([], {"initialized": True, "last_uid": 7}),
        ]

        class StopEmailLoop(Exception):
            pass

        class ImmediateThread:
            def __init__(self, *, target, **_kwargs):
                self.target = target

            def start(self):
                try:
                    self.target()
                except StopEmailLoop:
                    pass

        def poll(*_args, **_kwargs):
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        def sleep(delay):
            delays.append(delay)
            if len(delays) == 3:
                raise StopEmailLoop()

        def record_health(component, status, **kwargs):
            statuses.append((component, status, kwargs.get("detail")))
            return True

        with mock.patch.object(bridge, "poll_unread_messages", side_effect=poll), mock.patch.object(
            bridge, "load_email_state", return_value={}
        ), mock.patch.object(bridge, "save_email_state"), mock.patch.object(
            bridge, "set_component_health", side_effect=record_health
        ), mock.patch.object(bridge.time, "sleep", side_effect=sleep), mock.patch.object(
            bridge.threading, "Thread", ImmediateThread
        ):
            bridge.maybe_start_email_loop(
                {
                    "enable_email_notifications": True,
                    "owner_chat_id": "123",
                    "gmail_imap_email": "owner@gmail.com",
                    "gmail_imap_app_password": "app-password",
                },
                mock.Mock(),
                {},
                mock.Mock(),
            )

        self.assertEqual([status for _component, status, _detail in statuses], ["warning", "error", "ok"])
        self.assertEqual(delays, [60, 60, 300])

    def test_timeout_alert_does_not_tell_user_to_replace_credentials(self):
        bridge = load_bridge_module()
        message = bridge.gmail_background_failure_message(
            "Could not reach Gmail IMAP: timeout: The read operation timed out",
            "python3 /runtime/scripts/setup.py",
        )
        self.assertIn("after two consecutive failures", message)
        self.assertIn("retry in one minute", message)
        self.assertNotIn("replace only", message)

    def test_first_transient_email_failure_does_not_alert(self):
        bridge = load_bridge_module()
        notifications = {}
        messages = bridge.google_background_alert_messages(
            notifications,
            {"enable_email_notifications": True},
            {"enable_calendar": False},
            {"status": "warning", "detail": "read timed out"},
            {},
        )
        self.assertEqual(messages, [])
        self.assertNotIn("email_active", notifications)

    def test_active_email_failure_does_not_spam_when_detail_changes(self):
        bridge = load_bridge_module()
        notifications = {}
        first = bridge.google_background_alert_messages(
            notifications,
            {"enable_email_notifications": True},
            {"enable_calendar": False},
            {"status": "error", "detail": "read timed out"},
            {},
        )
        changed = bridge.google_background_alert_messages(
            notifications,
            {"enable_email_notifications": True},
            {"enable_calendar": False},
            {"status": "error", "detail": "network unreachable"},
            {},
        )
        self.assertEqual(len(first), 1)
        self.assertEqual(changed, [])

    def test_disabling_background_feature_clears_active_alert_without_false_recovery(self):
        bridge = load_bridge_module()
        notifications = {
            "email_active": True,
            "email_fingerprint": "error|bad password",
            "calendar_active": True,
            "calendar_fingerprint": "error|bad URL",
        }
        messages = bridge.google_background_alert_messages(
            notifications,
            {"enable_email_notifications": False},
            {"enable_calendar": False},
            {"status": "ok"},
            {"status": "ok"},
        )
        self.assertEqual(messages, [])
        self.assertNotIn("email_active", notifications)
        self.assertNotIn("calendar_active", notifications)

    def test_health_checks_official_google_app_access_and_gives_repair_path(self):
        bridge = load_bridge_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            telegram_config = root / "telegram-config.json"
            memory_dir = root / "memory"
            memory_dir.mkdir()
            bridge.save_json(
                telegram_config,
                {"enable_google_apps": True, "enable_email_notifications": False},
            )
            bridge.save_json(memory_dir / "config.json", {"enable_calendar": False})
            apps = {
                "new-gmail-id": {
                    "id": "new-gmail-id",
                    "name": "Gmail",
                    "isEnabled": True,
                    "isAccessible": False,
                },
                "new-calendar-id": {
                    "id": "new-calendar-id",
                    "displayName": "Google Calendar",
                    "isEnabled": True,
                    "isAccessible": True,
                },
            }
            with mock.patch.object(bridge, "CONFIG_FILE", telegram_config), mock.patch.object(
                bridge, "MEMORY_STATE_DIR", memory_dir
            ), mock.patch.object(bridge, "HEALTH_STATE_FILE", root / "health.json"), mock.patch.object(
                bridge, "MEMORY_HEALTH_FILE", memory_dir / "health.json"
            ), mock.patch.object(bridge, "MEMORY_ALERT_FILE", memory_dir / "alert.json"), mock.patch.object(
                bridge, "MEMORY_TASK_FILE", memory_dir / "task.json"
            ), mock.patch.object(bridge, "MEMORY_PID_FILE", memory_dir / "worker.pid"), mock.patch.object(
                bridge, "UPDATE_STATE_FILE", root / "update.json"
            ):
                text = bridge.health_text(google_apps=apps)
        self.assertIn("Official Gmail app: not connected", text)
        self.assertIn("Official Google Calendar app: connected", text)
        self.assertIn("Saved working settings", text)
        self.assertIn("setup.py", text)

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
