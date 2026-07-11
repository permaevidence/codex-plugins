import contextlib
import importlib.util
import io
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
            ["start", "help", "status", "stop", "newsession"],
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


if __name__ == "__main__":
    unittest.main()
