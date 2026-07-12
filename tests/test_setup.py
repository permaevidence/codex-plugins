from __future__ import annotations

import importlib.util
import contextlib
import io
import tempfile
import unittest
import json
import os
from unittest import mock
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SETUP_PATH = REPO_ROOT / "scripts" / "setup.py"

spec = importlib.util.spec_from_file_location("setup_wizard", SETUP_PATH)
assert spec and spec.loader
setup_wizard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup_wizard)

RUNTIME_PATH = REPO_ROOT / "scripts" / "runtime_install.py"
runtime_spec = importlib.util.spec_from_file_location("runtime_install_test", RUNTIME_PATH)
assert runtime_spec and runtime_spec.loader
runtime_install = importlib.util.module_from_spec(runtime_spec)
runtime_spec.loader.exec_module(runtime_install)


class SetupWizardTests(unittest.TestCase):
    def model_catalog_result(self):
        payload = {
            "models": [
                {
                    "slug": "gpt-current",
                    "display_name": "Current",
                    "visibility": "list",
                    "default_reasoning_level": "medium",
                    "supported_reasoning_levels": [{"effort": "medium"}, {"effort": "high"}],
                },
                {
                    "slug": "gpt-saved",
                    "display_name": "Saved",
                    "visibility": "list",
                    "default_reasoning_level": "low",
                    "supported_reasoning_levels": [{"effort": "low"}, {"effort": "xhigh"}],
                },
            ]
        }
        return mock.Mock(returncode=0, stdout=json.dumps(payload))

    def test_model_selection_preserves_saved_bridge_choice(self) -> None:
        with mock.patch.object(setup_wizard.subprocess, "run", return_value=self.model_catalog_result()), mock.patch.object(
            setup_wizard, "codex_effective_model_and_effort", return_value=("gpt-current", "high")
        ):
            selected = setup_wizard.resolve_model_selection(None, None, "gpt-saved", "xhigh")
        self.assertEqual(selected, ("gpt-saved", "xhigh"))

    def test_first_model_selection_inherits_codex_defaults(self) -> None:
        with mock.patch.object(setup_wizard.subprocess, "run", return_value=self.model_catalog_result()), mock.patch.object(
            setup_wizard, "codex_effective_model_and_effort", return_value=("gpt-current", "high")
        ):
            selected = setup_wizard.resolve_model_selection(None, None)
        self.assertEqual(selected, ("gpt-current", "high"))

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

    def test_recommended_sandbox_mode_is_danger_full_access(self) -> None:
        with mock.patch.object(setup_wizard, "prompt", return_value="1"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(setup_wizard.choose_sandbox_mode(), "dangerFullAccess")

    def test_danger_full_access_uses_whole_mac_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp).resolve()
            config_path = project_dir / "telegram-config.json"
            with mock.patch.object(setup_wizard, "TELEGRAM_CONFIG", config_path), mock.patch.object(
                setup_wizard, "TELEGRAM_ENV", project_dir / ".env"
            ):
                setup_wizard.configure_telegram(
                    telegram_token="123:token",
                    openai_key="sk-test",
                    project_dir=project_dir,
                    model="gpt-5.5",
                    effort="high",
                    sandbox_mode="dangerFullAccess",
                    network_access=True,
                )

            config = setup_wizard.load_json(config_path)
            self.assertEqual(config["default_cwd"], str(project_dir))
            self.assertEqual(config["sandbox_mode"], "dangerFullAccess")
            self.assertTrue(config["network_access"])
            self.assertEqual(config["writable_roots"], [])

    def test_local_capabilities_block_is_written_and_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agents_path = Path(tmp) / "AGENTS.md"
            agents_path.write_text("Existing instructions.\n", encoding="utf-8")

            setup_wizard.write_local_capabilities_block(agents_path)
            first = agents_path.read_text(encoding="utf-8")
            setup_wizard.write_local_capabilities_block(agents_path)
            second = agents_path.read_text(encoding="utf-8")

            self.assertEqual(first, second)
            self.assertIn(setup_wizard.LOCAL_CAPABILITIES_BEGIN, second)
            self.assertIn("Telegram Reminders", second)
            self.assertIn("scheduled_reminders.json", second)
            self.assertIn("Whole-Mac Codex Control", second)
            self.assertIn("Communication Trust", second)

    def test_hook_trust_is_added_and_updated_without_duplicates(self) -> None:
        original = "[features]\nhooks = true\n\n[hooks.state]\n"
        first = setup_wizard.set_hook_trust(original, "/tmp/hooks.json:stop:0:0", "sha256:first")
        second = setup_wizard.set_hook_trust(first, "/tmp/hooks.json:stop:0:0", "sha256:second")
        self.assertEqual(second.count('[hooks.state."/tmp/hooks.json:stop:0:0"]'), 1)
        self.assertNotIn("sha256:first", second)
        self.assertIn('trusted_hash = "sha256:second"', second)

    def test_setup_backup_preserves_existing_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex = root / ".codex"
            agents = root / "AGENTS.md"
            (codex / "long-term-memory").mkdir(parents=True)
            (codex / "config.toml").write_text("model = 'test'\n", encoding="utf-8")
            agents.write_text("instructions\n", encoding="utf-8")
            with mock.patch.object(setup_wizard, "CODEX_DIR", codex), mock.patch.object(
                setup_wizard, "MEMORY_ENV", codex / "long-term-memory/.env"
            ), mock.patch.object(
                setup_wizard, "MEMORY_CONFIG", codex / "long-term-memory/config.json"
            ), mock.patch.object(
                setup_wizard, "TELEGRAM_ENV", codex / "telegram-bridge/.env"
            ), mock.patch.object(
                setup_wizard, "TELEGRAM_CONFIG", codex / "telegram-bridge/config.json"
            ):
                backup = setup_wizard.create_setup_backup(agents)
            self.assertEqual((backup / "config.toml").read_text(encoding="utf-8"), "model = 'test'\n")
            self.assertEqual((backup / "AGENTS.md").read_text(encoding="utf-8"), "instructions\n")


class RuntimeInstallTests(unittest.TestCase):
    def make_source(self, root: Path, version: str = "1.2.3") -> Path:
        source = root / "source"
        (source / ".agents/plugins").mkdir(parents=True)
        (source / ".agents/plugins/marketplace.json").write_text(
            json.dumps({"name": "permaevidence-local", "plugins": []}), encoding="utf-8"
        )
        for name in ("codex-long-term-memory", "codex-telegram-bridge"):
            manifest = source / "plugins" / name / ".codex-plugin" / "plugin.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(json.dumps({"name": name, "version": version}), encoding="utf-8")
        return source

    def test_runtime_install_uses_atomic_current_link_and_cachebuster(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            app = root / "Application Support/PermaEvidenceCodex"
            with mock.patch.object(runtime_install, "APP_SUPPORT_ROOT", app), mock.patch.object(
                runtime_install, "VERSIONS_DIR", app / "versions"
            ), mock.patch.object(runtime_install, "CURRENT_LINK", app / "current"):
                current = runtime_install.install_runtime(source, cachebuster="commit-abc123")
                target = current.resolve()
            self.assertTrue(current.is_symlink())
            self.assertTrue(target.is_dir())
            manifest = json.loads(
                (current / "plugins/codex-telegram-bridge/.codex-plugin/plugin.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["version"], "1.2.3+codex.commit-abc123")


if __name__ == "__main__":
    unittest.main()
