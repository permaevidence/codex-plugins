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
            ["start", "help", "status", "model", "stop", "newsession"],
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
