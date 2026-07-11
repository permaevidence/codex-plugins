from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SETUP_PATH = REPO_ROOT / "scripts" / "setup.py"

spec = importlib.util.spec_from_file_location("setup_wizard", SETUP_PATH)
assert spec and spec.loader
setup_wizard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup_wizard)


class SetupWizardTests(unittest.TestCase):
    def test_update_env_file_preserves_unknown_lines_and_updates_known_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                "# keep this comment\n"
                "TELEGRAM_BOT_TOKEN=old-token\n"
                "OTHER=value\n",
                encoding="utf-8",
            )

            setup_wizard.update_env_file(
                path,
                {
                    "TELEGRAM_BOT_TOKEN": "new-token",
                    "OPENAI_API_KEY": "sk-test",
                },
            )

            self.assertEqual(
                path.read_text(encoding="utf-8"),
                "# keep this comment\n"
                "TELEGRAM_BOT_TOKEN=new-token\n"
                "OTHER=value\n"
                "\n"
                "OPENAI_API_KEY=sk-test\n",
            )
            self.assertEqual(setup_wizard.read_env_value(path, "OPENAI_API_KEY"), "sk-test")


if __name__ == "__main__":
    unittest.main()
