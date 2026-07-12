import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path


BRIDGE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "telegram_bridge.py"


def load_bridge_module():
    spec = importlib.util.spec_from_file_location("telegram_bridge", BRIDGE_PATH)
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


if __name__ == "__main__":
    unittest.main()
