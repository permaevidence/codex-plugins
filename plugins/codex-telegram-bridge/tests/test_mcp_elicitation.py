import importlib.util
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


if __name__ == "__main__":
    unittest.main()
