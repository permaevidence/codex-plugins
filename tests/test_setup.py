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

UPDATE_PATH = REPO_ROOT / "scripts" / "update.py"
update_spec = importlib.util.spec_from_file_location("runtime_update_test", UPDATE_PATH)
assert update_spec and update_spec.loader
runtime_update = importlib.util.module_from_spec(update_spec)
update_spec.loader.exec_module(runtime_update)


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

    def test_runtime_install_reuses_same_cachebuster_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            app = root / "Application Support/PermaEvidenceCodex"
            with mock.patch.object(runtime_install, "APP_SUPPORT_ROOT", app), mock.patch.object(
                runtime_install, "VERSIONS_DIR", app / "versions"
            ), mock.patch.object(runtime_install, "CURRENT_LINK", app / "current"):
                first = runtime_install.install_runtime(source, cachebuster="commit-abc123").resolve()
                second = runtime_install.install_runtime(source, cachebuster="commit-abc123").resolve()
            self.assertEqual(first, second)
            self.assertEqual([path.name for path in (app / "versions").iterdir()], ["commit-abc123"])

    def test_runtime_install_rewrites_relative_mcp_paths_to_absolute(self) -> None:
        # Codex launches plugin MCP servers with the session cwd, so relative
        # script paths in .mcp.json must become absolute at install time.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            (source / "plugins/codex-telegram-bridge/.mcp.json").write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "telegram-actions": {
                                "command": "python3",
                                "args": ["-u", "./scripts/telegram_actions_mcp.py"],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            app = root / "Application Support/PermaEvidenceCodex"
            with mock.patch.object(runtime_install, "APP_SUPPORT_ROOT", app), mock.patch.object(
                runtime_install, "VERSIONS_DIR", app / "versions"
            ), mock.patch.object(runtime_install, "CURRENT_LINK", app / "current"):
                current = runtime_install.install_runtime(source, cachebuster="commit-abc123")
                installed_dir = current.resolve()
            servers = json.loads(
                (current / "plugins/codex-telegram-bridge/.mcp.json").read_text(encoding="utf-8")
            )["mcpServers"]
            args = servers["telegram-actions"]["args"]
            self.assertEqual(args[0], "-u")
            expected = installed_dir / "plugins/codex-telegram-bridge/scripts/telegram_actions_mcp.py"
            self.assertEqual(Path(args[1]).resolve(), expected.resolve())
            self.assertTrue(Path(args[1]).is_absolute())
            # A bare interpreter name resolves via PATH and must stay untouched.
            self.assertEqual(servers["telegram-actions"]["command"], "python3")

    def test_runtime_install_keeps_previous_version_until_health_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            app = root / "Application Support/PermaEvidenceCodex"
            with mock.patch.object(runtime_install, "APP_SUPPORT_ROOT", app), mock.patch.object(
                runtime_install, "VERSIONS_DIR", app / "versions"
            ), mock.patch.object(runtime_install, "CURRENT_LINK", app / "current"):
                installed = [
                    runtime_install.install_runtime(source, cachebuster=name).resolve()
                    for name in ("first", "second", "third", "fourth")
                ]
                for index, path in enumerate(installed, start=1):
                    os.utime(path, (index, index))
                self.assertTrue(all(path.exists() for path in installed))

                runtime_install.prune_old_versions(active=installed[-1])
                self.assertTrue(installed[-1].exists())
                self.assertEqual(
                    len([path for path in (app / "versions").iterdir() if path.is_dir()]),
                    runtime_install.KEEP_VERSIONS,
                )

    def test_runtime_prune_never_deletes_referenced_versions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            home = root / "home"
            app = home / "Library/Application Support/PermaEvidenceCodex"
            hooks = home / ".codex/hooks.json"
            hooks.parent.mkdir(parents=True)
            launch_agent = home / "Library/LaunchAgents/com.permaevidence.test.plist"
            launch_agent.parent.mkdir(parents=True)
            with mock.patch.object(runtime_install, "APP_SUPPORT_ROOT", app), mock.patch.object(
                runtime_install, "VERSIONS_DIR", app / "versions"
            ), mock.patch.object(runtime_install, "CURRENT_LINK", app / "current"), mock.patch.object(
                runtime_install.Path, "home", return_value=home
            ) as _home:
                installed = [
                    runtime_install.install_runtime(source, cachebuster=name).resolve()
                    for name in ("hook-ref", "launch-ref", "process-ref", "victim", "active")
                ]
                for index, path in enumerate(installed, start=1):
                    os.utime(path, (index, index))
                hooks.write_text(json.dumps({"command": str(installed[0] / "hook.py")}), encoding="utf-8")
                launch_agent.write_text(f"<string>{installed[1]}/bridge.py</string>", encoding="utf-8")
                with mock.patch.object(
                    runtime_install.subprocess,
                    "run",
                    return_value=mock.Mock(returncode=0, stdout=f"python3 {installed[2]}/worker.py\n"),
                ):
                    runtime_install.prune_old_versions(active=installed[-1])

            self.assertTrue(installed[0].exists(), "hook-referenced runtime must be preserved")
            self.assertTrue(installed[1].exists(), "LaunchAgent-referenced runtime must be preserved")
            self.assertTrue(installed[2].exists())
            self.assertFalse(installed[3].exists(), "unreferenced runtime should be pruned")
            self.assertTrue(installed[4].exists())

    def test_deferred_update_is_one_shot_detached_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with mock.patch.object(runtime_update.Path, "home", return_value=home), mock.patch.object(
                runtime_update.subprocess, "Popen"
            ) as popen:
                log = runtime_update.schedule_deferred_update("main", 45)
            command = popen.call_args.args[0]
            self.assertIn("--run-after-delay", command)
            self.assertNotIn("launchctl", command)
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
            self.assertEqual(log, home / ".codex/telegram-bridge/update-handoff.log")



UPDATE_PATH = REPO_ROOT / "scripts" / "update.py"
update_spec = importlib.util.spec_from_file_location("update_script_test", UPDATE_PATH)
assert update_spec and update_spec.loader
update_script = importlib.util.module_from_spec(update_spec)
update_spec.loader.exec_module(update_script)


class UpdateHandoffTests(unittest.TestCase):
    def test_annotate_recovery_marks_only_inflight_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            queue = Path(tmp) / "turn_recovery_queue.json"
            queue.write_text(
                json.dumps(
                    [
                        {"id": "a", "state": "in_progress", "active_retry_turn_id": "t1", "due_at": 1, "attempts": 2},
                        {"id": "b", "state": "starting", "due_at": 1},
                        {"id": "c", "state": "parked", "due_at": None},
                        {"id": "d", "state": "pending", "due_at": 99},
                    ]
                ),
                encoding="utf-8",
            )
            changed = update_script.annotate_recovery_for_restart(queue)
            self.assertEqual(changed, 2)
            records = {item["id"]: item for item in json.loads(queue.read_text(encoding="utf-8"))}
            for rec_id in ("a", "b"):
                self.assertEqual(records[rec_id]["state"], "pending")
                self.assertIsNone(records[rec_id]["active_retry_turn_id"])
                self.assertEqual(records[rec_id]["reason"], "interrupted by runtime update restart")
            self.assertEqual(records["a"]["attempts"], 2)  # attempts untouched
            self.assertEqual(records["c"]["state"], "parked")
            self.assertEqual(records["d"]["due_at"], 99)

    def test_annotate_recovery_handles_missing_or_corrupt_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope.json"
            self.assertEqual(update_script.annotate_recovery_for_restart(missing), 0)
            corrupt = Path(tmp) / "bad.json"
            corrupt.write_text("{not json", encoding="utf-8")
            self.assertEqual(update_script.annotate_recovery_for_restart(corrupt), 0)

    def test_write_update_state_replaces_file_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "update_state.json"
            with mock.patch.object(update_script, "UPDATE_STATE_FILE", state_file):
                update_script.write_update_state(status="running", ref="main", announced=False)
                update_script.write_update_state(status="completed", ref="main", commit="abc", announced=False)
            data = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(data["status"], "completed")
            self.assertEqual(data["commit"], "abc")
            self.assertFalse(data["announced"])


if __name__ == "__main__":
    unittest.main()
