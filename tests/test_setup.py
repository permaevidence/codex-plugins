from __future__ import annotations

import importlib.util
import contextlib
import io
import tempfile
import unittest
import json
import os
import sqlite3
from unittest import mock
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SETUP_PATH = REPO_ROOT / "scripts" / "setup.py"

spec = importlib.util.spec_from_file_location("setup_wizard", SETUP_PATH)
assert spec and spec.loader
setup_wizard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup_wizard)

PLATFORM_PATH = REPO_ROOT / "scripts" / "platform_support.py"
platform_spec = importlib.util.spec_from_file_location("platform_support_test", PLATFORM_PATH)
assert platform_spec and platform_spec.loader
platform_support = importlib.util.module_from_spec(platform_spec)
platform_spec.loader.exec_module(platform_support)

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

JSONRPC_PATH = REPO_ROOT / "scripts" / "jsonrpc_io.py"
jsonrpc_spec = importlib.util.spec_from_file_location("jsonrpc_io_test", JSONRPC_PATH)
assert jsonrpc_spec and jsonrpc_spec.loader
jsonrpc_io = importlib.util.module_from_spec(jsonrpc_spec)
jsonrpc_spec.loader.exec_module(jsonrpc_io)


class SetupWizardTests(unittest.TestCase):
    class TtyBuffer(io.StringIO):
        def isatty(self) -> bool:
            return True

    def test_wizard_headings_use_terminal_styling_only_when_supported(self) -> None:
        terminal = self.TtyBuffer()
        with mock.patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True), contextlib.redirect_stdout(
            terminal
        ):
            setup_wizard.print_header("Plugin setup")
            setup_wizard.step_header(2, "Telegram bot")
        styled_output = terminal.getvalue()
        self.assertIn("\033[", styled_output)
        self.assertIn("PLUGIN SETUP", styled_output)
        self.assertIn("STEP 2 OF 7", styled_output)

        plain = io.StringIO()
        with contextlib.redirect_stdout(plain):
            setup_wizard.print_header("Plugin setup")
        self.assertNotIn("\033[", plain.getvalue())

        no_color = self.TtyBuffer()
        with mock.patch.dict(
            os.environ, {"TERM": "xterm-256color", "NO_COLOR": "1"}, clear=True
        ), contextlib.redirect_stdout(no_color):
            setup_wizard.print_header("Plugin setup")
        self.assertNotIn("\033[", no_color.getvalue())

    def test_questions_have_a_separate_answer_line(self) -> None:
        output = io.StringIO()
        with mock.patch("builtins.input", return_value=""), contextlib.redirect_stdout(output):
            answer = setup_wizard.prompt("Your choice", default="1")
        self.assertEqual(answer, "1")
        self.assertIn("\n› Your choice  [1]\n", output.getvalue())

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

    def test_timezone_selection_accepts_iana_name_and_rejects_unknown_name(self) -> None:
        self.assertEqual(
            setup_wizard.resolve_timezone_name("America/New_York"),
            "America/New_York",
        )
        with self.assertRaises(SystemExit):
            setup_wizard.resolve_timezone_name("Not/A_Zone")

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

    def test_existing_secret_can_be_kept_without_revealing_it(self) -> None:
        output = io.StringIO()
        with mock.patch.object(setup_wizard, "prompt_yes_no", return_value=True), mock.patch.object(
            setup_wizard.getpass, "getpass"
        ) as secret_prompt, contextlib.redirect_stdout(output):
            value = setup_wizard.resolve_secret(
                supplied=None,
                existing="super-secret-value",
                label="OpenAI API key",
                required_message="required",
            )
        self.assertEqual(value, "super-secret-value")
        secret_prompt.assert_not_called()
        self.assertNotIn("super-secret-value", output.getvalue())
        self.assertIn("configured", output.getvalue())

    def test_existing_secret_can_be_replaced_without_overwriting_early(self) -> None:
        with mock.patch.object(setup_wizard, "prompt_yes_no", return_value=False), mock.patch.object(
            setup_wizard.getpass, "getpass", return_value="replacement"
        ):
            value = setup_wizard.resolve_secret(
                supplied=None,
                existing="old-secret",
                label="Telegram bot token",
                required_message="required",
            )
        self.assertEqual(value, "replacement")

    def test_rerun_defaults_preserve_existing_non_secret_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            setup_wizard, "prompt", side_effect=lambda _label, default="": default
        ), mock.patch.object(setup_wizard, "prompt_yes_no", side_effect=lambda _label, default=False: default):
            self.assertEqual(
                setup_wizard.resolve_project_dir(None, tmp),
                Path(tmp).resolve(),
            )
            self.assertEqual(
                setup_wizard.choose_sandbox_mode("workspaceWrite"),
                "workspaceWrite",
            )
            self.assertFalse(
                setup_wizard.resolve_network_access(None, "dangerFullAccess", False)
            )

    def test_fresh_whole_computer_setup_uses_home_without_folder_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            with mock.patch.object(setup_wizard.Path, "home", return_value=home):
                current = setup_wizard.default_starting_location()
            with mock.patch.object(setup_wizard, "resolve_project_dir") as folder_prompt, mock.patch.object(
                setup_wizard, "prompt_yes_no"
            ) as advanced_prompt:
                selected = setup_wizard.resolve_starting_location_for_access(
                    supplied=None,
                    existing="",
                    current=current,
                    sandbox_mode="dangerFullAccess",
                    offer_advanced_change=False,
                )
        self.assertEqual(selected, home)
        folder_prompt.assert_not_called()
        advanced_prompt.assert_not_called()

    def test_existing_starting_location_is_preserved_in_whole_computer_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            saved = Path(tmp).resolve()
            current = setup_wizard.default_starting_location(str(saved))
            with mock.patch.object(setup_wizard, "resolve_project_dir") as folder_prompt:
                selected = setup_wizard.resolve_starting_location_for_access(
                    supplied=None,
                    existing=str(saved),
                    current=current,
                    sandbox_mode="dangerFullAccess",
                    offer_advanced_change=False,
                )
        self.assertEqual(selected, saved)
        folder_prompt.assert_not_called()

    def test_restricted_mode_requests_its_folder_boundary(self) -> None:
        current = Path.home().resolve()
        restricted = current / "restricted"
        with mock.patch.object(
            setup_wizard, "resolve_project_dir", return_value=restricted
        ) as folder_prompt, contextlib.redirect_stdout(io.StringIO()):
            selected = setup_wizard.resolve_starting_location_for_access(
                supplied=None,
                existing="",
                current=current,
                sandbox_mode="workspaceWrite",
                offer_advanced_change=False,
            )
        self.assertEqual(selected, restricted)
        folder_prompt.assert_called_once_with(None, str(current))

    def test_advanced_rerun_can_change_whole_computer_starting_location(self) -> None:
        current = Path.home().resolve()
        replacement = current / "advanced"
        with mock.patch.object(setup_wizard, "prompt_yes_no", return_value=True), mock.patch.object(
            setup_wizard, "resolve_project_dir", return_value=replacement
        ) as folder_prompt, contextlib.redirect_stdout(io.StringIO()):
            selected = setup_wizard.resolve_starting_location_for_access(
                supplied=None,
                existing=str(current),
                current=current,
                sandbox_mode="dangerFullAccess",
                offer_advanced_change=True,
            )
        self.assertEqual(selected, replacement)
        folder_prompt.assert_called_once_with(None, str(current))

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
                    timezone_name="America/New_York",
                )

            config = setup_wizard.load_json(config_path)
            self.assertEqual(config["default_cwd"], str(project_dir))
            self.assertEqual(config["sandbox_mode"], "dangerFullAccess")
            self.assertEqual(config["timezone"], "America/New_York")
            self.assertTrue(config["network_access"])
            self.assertEqual(config["writable_roots"], [])

    def test_native_codex_permission_targets_follow_current_release_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            release_bin = root / ".codex/packages/standalone/releases/1.2.3/bin"
            release_bin.mkdir(parents=True)
            codex = release_bin / "codex"
            helper = release_bin / "codex-code-mode-host"
            codex.write_text("binary", encoding="utf-8")
            helper.write_text("binary", encoding="utf-8")
            launcher = root / ".local/bin/codex"
            launcher.parent.mkdir(parents=True)
            launcher.symlink_to(codex)

            def signed(command, **_kwargs):
                identifier = Path(command[-1]).name
                return mock.Mock(
                    returncode=0,
                    stdout="",
                    stderr=(
                        f"Identifier={identifier}\n"
                        "TeamIdentifier=2DC432GLL2\n"
                    ),
                )

            with mock.patch.object(setup_wizard.subprocess, "run", side_effect=signed):
                installation = setup_wizard.codex_permission_installation(str(launcher))

        self.assertEqual(installation["kind"], "native")
        self.assertEqual(installation["codex"], codex.resolve())
        self.assertEqual(installation["helper"], helper.resolve())
        self.assertEqual(installation["targets"], [codex.resolve(), helper.resolve()])

    def test_unverified_native_binaries_are_never_permission_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            codex = bin_dir / "codex"
            helper = bin_dir / "codex-code-mode-host"
            codex.write_text("binary", encoding="utf-8")
            helper.write_text("binary", encoding="utf-8")
            signature = mock.Mock(
                returncode=0,
                stdout="",
                stderr="Identifier=codex\nTeamIdentifier=NOT-OPENAI\n",
            )
            with mock.patch.object(setup_wizard.subprocess, "run", return_value=signature):
                installation = setup_wizard.codex_permission_installation(str(codex))

        self.assertEqual(installation["kind"], "untrusted")
        self.assertEqual(installation["targets"], [])
        self.assertIn("could not be verified", installation["detail"])

    def test_npm_codex_is_not_offered_as_full_disk_access_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_js = Path(tmp) / "node_modules/@openai/codex/bin/codex.js"
            codex_js.parent.mkdir(parents=True)
            codex_js.write_text("#!/usr/bin/env node\n", encoding="utf-8")

            installation = setup_wizard.codex_permission_installation(str(codex_js))

        self.assertEqual(installation["kind"], "npm")
        self.assertEqual(installation["targets"], [])
        self.assertIn("unrelated Node programs", installation["detail"])

    def test_full_disk_access_status_matches_exact_current_binary_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex = root / "release-2/bin/codex"
            helper = codex.parent / "codex-code-mode-host"
            codex.parent.mkdir(parents=True)
            codex.write_text("binary", encoding="utf-8")
            helper.write_text("binary", encoding="utf-8")
            database = root / "TCC.db"
            connection = sqlite3.connect(database)
            connection.execute(
                "CREATE TABLE access (service TEXT, client TEXT, auth_value INTEGER)"
            )
            connection.executemany(
                "INSERT INTO access VALUES (?, ?, ?)",
                [
                    ("kTCCServiceSystemPolicyAllFiles", str(codex), 2),
                    ("kTCCServiceSystemPolicyAllFiles", str(helper), 2),
                ],
            )
            connection.commit()
            connection.close()

            granted = setup_wizard.codex_full_disk_access_status(
                [codex, helper],
                database=database,
            )
            next_release = root / "release-3/bin/codex"
            next_release.parent.mkdir(parents=True)
            next_release.write_text("binary", encoding="utf-8")
            changed = setup_wizard.codex_full_disk_access_status(
                [next_release, helper],
                database=database,
            )

        self.assertEqual(granted["state"], "granted")
        self.assertEqual(changed["state"], "missing")
        self.assertEqual(changed["missing"], [next_release.resolve()])

    def test_full_disk_access_plan_is_guided_only_for_native_whole_mac_mode(self) -> None:
        codex = Path("/private/current/bin/codex")
        helper = codex.parent / "codex-code-mode-host"
        installation = {
            "kind": "native",
            "targets": [codex, helper],
            "detail": "found",
        }
        status = {
            "state": "missing",
            "authorized": [],
            "missing": [codex, helper],
            "detail": "missing",
        }
        with mock.patch.object(setup_wizard, "platform_family", return_value="macos"), mock.patch.object(
            setup_wizard, "codex_permission_installation", return_value=installation
        ), mock.patch.object(
            setup_wizard, "codex_full_disk_access_status", return_value=status
        ), mock.patch.object(
            setup_wizard, "prompt_yes_no", return_value=True
        ) as permission_prompt, contextlib.redirect_stdout(io.StringIO()):
            plan = setup_wizard.resolve_macos_full_disk_access_plan("dangerFullAccess")

        self.assertTrue(plan["applicable"])
        self.assertTrue(plan["requested"])
        self.assertIn("open System Settings", plan["review"])
        permission_prompt.assert_called_once()

        with mock.patch.object(setup_wizard, "platform_family", return_value="macos"), mock.patch.object(
            setup_wizard, "codex_permission_installation"
        ) as discovery:
            restricted = setup_wizard.resolve_macos_full_disk_access_plan("workspaceWrite")
        self.assertFalse(restricted["requested"])
        self.assertIn("restricted-folder", restricted["review"])
        discovery.assert_not_called()

    def test_full_disk_access_guide_opens_settings_and_accepts_verified_grant(self) -> None:
        codex = Path("/private/current/bin/codex")
        helper = codex.parent / "codex-code-mode-host"
        plan: dict[str, object] = {
            "requested": True,
            "targets": [codex, helper],
        }
        granted = {
            "state": "granted",
            "authorized": [codex, helper],
            "missing": [],
            "detail": "granted",
        }
        completed = mock.Mock(returncode=0)
        output = io.StringIO()
        with mock.patch.object(
            setup_wizard.subprocess, "run", return_value=completed
        ) as run_process, mock.patch.object(
            setup_wizard, "prompt", return_value=""
        ), mock.patch.object(
            setup_wizard, "codex_full_disk_access_status", return_value=granted
        ), contextlib.redirect_stdout(output):
            setup_wizard.guide_macos_full_disk_access(plan)

        commands = [call.args[0] for call in run_process.call_args_list]
        self.assertEqual(commands.count(["pbcopy"]), 2)
        self.assertIn(["open", "-R", str(codex)], commands)
        self.assertIn(["open", "-R", str(helper)], commands)
        self.assertIn(
            ["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"],
            commands,
        )
        clipboard_values = [
            call.kwargs["input"]
            for call in run_process.call_args_list
            if call.args[0] == ["pbcopy"]
        ]
        self.assertEqual(clipboard_values, [str(codex), str(helper)])
        self.assertIn("will not already appear in the list", output.getvalue())
        self.assertIn("one at a time", output.getvalue())
        self.assertEqual(plan["status"], granted)

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
            self.assertIn("Whole-Computer Codex Control", second)
            self.assertIn("Communication Trust", second)
            self.assertIn("official Gmail and Google Calendar Codex plugins", second)
            self.assertNotIn("Google Workspace CLI", second)

    def test_linux_capabilities_block_uses_linux_language(self) -> None:
        with mock.patch.object(setup_wizard, "platform_family", return_value="linux"), mock.patch.object(
            setup_wizard, "platform_display_name", return_value="Linux computer"
        ):
            block = setup_wizard.build_local_capabilities_block()
        self.assertIn("Whole-Computer Codex Control", block)
        self.assertIn("trusted, dedicated Linux computer", block)
        self.assertIn("local Linux user", block)
        self.assertNotIn("Whole-Mac", block)

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

    def test_openai_validation_probe_includes_real_transcription_request(self) -> None:
        body, boundary = setup_wizard.openai_transcription_probe_body()
        self.assertIn(f"--{boundary}".encode(), body)
        self.assertIn(b"gpt-4o-transcribe", body)
        self.assertIn(b'filename="health.wav"', body)

    def test_google_background_configuration_is_stored_securely(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory_dir = root / "memory"
            telegram_dir = root / "telegram"
            agents = root / "AGENTS.md"
            with mock.patch.object(setup_wizard, "MEMORY_DIR", memory_dir), mock.patch.object(
                setup_wizard, "MEMORY_ENV", memory_dir / ".env"
            ), mock.patch.object(
                setup_wizard, "MEMORY_CONFIG", memory_dir / "config.json"
            ), mock.patch.object(
                setup_wizard, "CALENDAR_SOURCES", memory_dir / "calendar_sources.json"
            ), mock.patch.object(
                setup_wizard, "TELEGRAM_ENV", telegram_dir / ".env"
            ), mock.patch.object(
                setup_wizard, "TELEGRAM_CONFIG", telegram_dir / "config.json"
            ):
                setup_wizard.configure_memory(
                    openai_key="sk-test",
                    agents_md_path=agents,
                    timezone_name="America/New_York",
                    calendar_enabled=True,
                    calendar_sources=[
                        {
                            "name": "Primary",
                            "url": "https://calendar.google.com/calendar/ical/example/private/basic.ics",
                        }
                    ],
                )
                setup_wizard.configure_telegram(
                    telegram_token="123:token",
                    openai_key="sk-test",
                    project_dir=root,
                    model="gpt-current",
                    effort="high",
                    sandbox_mode="dangerFullAccess",
                    network_access=True,
                    timezone_name="America/New_York",
                    google_apps_enabled=True,
                    email_notifications_enabled=True,
                    gmail_email="owner@gmail.com",
                    gmail_app_password="abcd efgh ijkl mnop",
                )

            memory_config = setup_wizard.load_json(memory_dir / "config.json")
            telegram_config = setup_wizard.load_json(telegram_dir / "config.json")
            self.assertEqual(memory_config["calendar_provider"], "ical")
            self.assertEqual(memory_config["timezone"], "America/New_York")
            self.assertTrue(memory_config["enable_calendar"])
            self.assertTrue(telegram_config["enable_google_apps"])
            self.assertEqual(telegram_config["timezone"], "America/New_York")
            self.assertTrue(telegram_config["enable_email_notifications"])
            self.assertNotIn("private/basic.ics", json.dumps(memory_config))
            self.assertNotIn("GMAIL_IMAP_APP_PASSWORD", json.dumps(telegram_config))
            self.assertEqual(
                setup_wizard.read_env_value(telegram_dir / ".env", "GMAIL_IMAP_APP_PASSWORD"),
                "abcdefghijklmnop",
            )
            self.assertEqual((memory_dir / "calendar_sources.json").stat().st_mode & 0o777, 0o600)

    def test_google_connection_steps_are_plain_and_ordered(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            setup_wizard.print_google_connection_steps(start=3)
        text = output.getvalue()
        self.assertIn("3. In Terminal, run: codex", text)
        self.assertIn("4. At the Codex prompt, type: /apps", text)
        self.assertIn("5. Select Gmail, choose Connect", text)
        self.assertIn("6. Return to /apps", text)
        self.assertIn("7. Exit Codex with /quit", text)

    def test_google_setup_explains_optional_background_features(self) -> None:
        args = mock.Mock(
            google_integration="yes",
            email_notifications="no",
            calendar_context="no",
        )
        output = io.StringIO()
        with mock.patch.object(setup_wizard, "load_json", return_value={}), mock.patch.object(
            setup_wizard, "read_calendar_sources", return_value=[]
        ), contextlib.redirect_stdout(output):
            result = setup_wizard.resolve_google_setup(args)
        self.assertTrue(result["enabled"])
        self.assertFalse(result["email_enabled"])
        self.assertFalse(result["calendar_enabled"])
        self.assertIn("optional background features", output.getvalue())
        self.assertIn("not required for the official Gmail and Calendar apps", output.getvalue())

    @staticmethod
    def make_cli_args(**overrides) -> "argparse.Namespace":
        import argparse

        values = {
            "project_dir": None,
            "google_integration": None,
            "email_notifications": None,
            "gmail_email": None,
            "gmail_app_password": None,
            "calendar_context": None,
            "calendar_ical_url": None,
            "telegram_token": None,
            "openai_api_key": None,
            "model": None,
            "effort": None,
            "timezone": None,
            "sandbox_mode": None,
            "network_access": None,
            "start_bridge": None,
            "pair_now": None,
            "skip_credential_checks": False,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_quick_menu_is_skipped_on_fresh_installs_and_cli_runs(self) -> None:
        args = self.make_cli_args()
        with mock.patch.object(setup_wizard, "prompt") as menu_prompt:
            self.assertIsNone(setup_wizard.choose_quick_section(args, existing_installation=False))
            self.assertIsNone(
                setup_wizard.choose_quick_section(
                    self.make_cli_args(telegram_token="123:t"), existing_installation=True
                )
            )
        menu_prompt.assert_not_called()

    def test_quick_menu_maps_choices_to_sections(self) -> None:
        args = self.make_cli_args()
        expected = {"1": None, "2": "telegram", "3": "openai", "4": "google", "5": "machine"}
        for choice, section in expected.items():
            with mock.patch.object(setup_wizard, "prompt", return_value=choice), contextlib.redirect_stdout(
                io.StringIO()
            ):
                self.assertEqual(
                    setup_wizard.choose_quick_section(args, existing_installation=True), section
                )
        with mock.patch.object(setup_wizard, "prompt", return_value="0"), contextlib.redirect_stdout(
            io.StringIO()
        ):
            with self.assertRaises(SystemExit):
                setup_wizard.choose_quick_section(args, existing_installation=True)

    def test_google_setup_from_existing_round_trips_saved_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            telegram_dir = root / "telegram"
            memory_dir = root / "memory"
            telegram_dir.mkdir()
            memory_dir.mkdir()
            (telegram_dir / "config.json").write_text(
                json.dumps({"enable_google_apps": True, "enable_email_notifications": True}),
                encoding="utf-8",
            )
            (telegram_dir / ".env").write_text(
                "GMAIL_IMAP_EMAIL=owner@gmail.com\nGMAIL_IMAP_APP_PASSWORD=abcdefghijklmnop\n",
                encoding="utf-8",
            )
            (memory_dir / "config.json").write_text(
                json.dumps({"enable_calendar": True}), encoding="utf-8"
            )
            (memory_dir / "calendar_sources.json").write_text(
                json.dumps({"sources": [{"name": "Primary", "url": "https://calendar.google.com/x.ics"}]}),
                encoding="utf-8",
            )
            with mock.patch.object(setup_wizard, "TELEGRAM_CONFIG", telegram_dir / "config.json"), mock.patch.object(
                setup_wizard, "TELEGRAM_ENV", telegram_dir / ".env"
            ), mock.patch.object(
                setup_wizard, "MEMORY_CONFIG", memory_dir / "config.json"
            ), mock.patch.object(
                setup_wizard, "CALENDAR_SOURCES", memory_dir / "calendar_sources.json"
            ):
                result = setup_wizard.google_setup_from_existing()
        self.assertTrue(result["enabled"])
        self.assertTrue(result["email_enabled"])
        self.assertTrue(result["email_kept"])
        self.assertEqual(result["gmail_email"], "owner@gmail.com")
        self.assertEqual(result["gmail_app_password"], "abcdefghijklmnop")
        self.assertTrue(result["calendar_enabled"])
        self.assertEqual(len(result["calendar_sources"]), 1)

    def test_runtime_validation_installs_dependencies_before_running_tests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            requirements = root / "plugins/codex-long-term-memory/requirements.txt"
            requirements.parent.mkdir(parents=True)
            requirements.write_text("example==1 --hash=sha256:" + "0" * 64 + "\n", encoding="utf-8")
            calls: list[tuple[list[str], dict[str, object]]] = []

            def fake_run(command, **kwargs):
                calls.append((list(command), dict(kwargs)))
                return mock.Mock(returncode=0)

            with mock.patch.object(setup_wizard.subprocess, "run", side_effect=fake_run):
                setup_wizard.validate_source_runtime(root)

        self.assertEqual(calls[0][0][2:5], ["pip", "install", "--disable-pip-version-check"])
        self.assertIn("--target", calls[0][0])
        self.assertEqual(len(calls), 5)
        for _, kwargs in calls[1:]:
            self.assertIn("PYTHONPATH", kwargs["env"])

    def test_jsonrpc_reader_keeps_responses_already_read_from_pipe(self) -> None:
        read_fd, write_fd = os.pipe()
        try:
            os.write(
                write_fd,
                b'{"jsonrpc":"2.0","id":1,"result":{}}\n'
                b'{"jsonrpc":"2.0","id":2,"result":{"ok":true}}\n',
            )
            os.close(write_fd)
            write_fd = -1
            with os.fdopen(read_fd, "r", encoding="utf-8") as stream:
                read_fd = -1
                reader = jsonrpc_io.JsonRpcLineReader(stream)
                process = mock.Mock()
                process.poll.return_value = None
                second = reader.wait_for_id(process, 2, timeout=1)
                first = reader.wait_for_id(process, 1, timeout=1)
            self.assertTrue(second["result"]["ok"])
            self.assertEqual(first["id"], 1)
        finally:
            if read_fd >= 0:
                os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)


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

    def test_runtime_install_rebases_mcp_path_from_previous_runtime(self) -> None:
        # Rerunning setup from an installed runtime copies a manifest whose
        # plugin-local MCP path is already absolute. It must be moved to the
        # replacement runtime rather than retaining a dependency on the old
        # version directory.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            script = source / "plugins/codex-telegram-bridge/scripts/telegram_actions_mcp.py"
            script.parent.mkdir(parents=True)
            script.write_text("# test server\n", encoding="utf-8")
            previous = (
                root
                / "Application Support/PermaEvidenceCodex/versions/commit-old"
                / "plugins/codex-telegram-bridge/scripts/telegram_actions_mcp.py"
            )
            external = root / "external/python3"
            (source / "plugins/codex-telegram-bridge/.mcp.json").write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "telegram-actions": {
                                "command": str(external),
                                "args": ["-u", str(previous)],
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
                current = runtime_install.install_runtime(source, cachebuster="local-reinstall")
                installed_dir = current.resolve()

            server = json.loads(
                (current / "plugins/codex-telegram-bridge/.mcp.json").read_text(encoding="utf-8")
            )["mcpServers"]["telegram-actions"]
            expected = installed_dir / "plugins/codex-telegram-bridge/scripts/telegram_actions_mcp.py"
            self.assertEqual(Path(server["args"][1]).resolve(), expected.resolve())
            self.assertNotIn("commit-old", server["args"][1])
            self.assertEqual(server["command"], str(external))

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
            ), mock.patch.object(
                runtime_install, "platform_family", return_value="macos"
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

    def test_runtime_prune_preserves_linux_systemd_references(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            home = root / "home"
            app = home / ".local/share/permaevidence-codex"
            unit_dir = home / ".config/systemd/user"
            unit_dir.mkdir(parents=True)
            with mock.patch.object(runtime_install, "APP_SUPPORT_ROOT", app), mock.patch.object(
                runtime_install, "VERSIONS_DIR", app / "versions"
            ), mock.patch.object(runtime_install, "CURRENT_LINK", app / "current"), mock.patch.object(
                runtime_install.Path, "home", return_value=home
            ), mock.patch.object(
                runtime_install, "platform_family", return_value="linux"
            ), mock.patch.object(
                runtime_install, "systemd_user_dir", return_value=unit_dir
            ):
                installed = [
                    runtime_install.install_runtime(source, cachebuster=name).resolve()
                    for name in ("systemd-ref", "victim", "spare", "active")
                ]
                for index, path in enumerate(installed, start=1):
                    os.utime(path, (index, index))
                (unit_dir / "bridge.service").write_text(
                    f"ExecStart=python3 {installed[0]}/bridge.py\n", encoding="utf-8"
                )
                with mock.patch.object(
                    runtime_install.subprocess,
                    "run",
                    return_value=mock.Mock(returncode=0, stdout=""),
                ):
                    runtime_install.prune_old_versions(active=installed[-1])
            self.assertTrue(installed[0].exists())
            self.assertFalse(installed[1].exists())
            self.assertTrue(installed[-1].exists())

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


class PlatformSupportTests(unittest.TestCase):
    def test_runtime_paths_follow_platform_conventions(self) -> None:
        home = Path("/home/alice")
        self.assertEqual(
            platform_support.runtime_data_root(home=home, platform="darwin"),
            home / "Library/Application Support/PermaEvidenceCodex",
        )
        self.assertEqual(
            platform_support.runtime_data_root(home=home, platform="linux", environ={}),
            home / ".local/share/permaevidence-codex",
        )
        self.assertEqual(
            platform_support.runtime_data_root(
                home=home,
                platform="linux",
                environ={"XDG_DATA_HOME": "/srv/alice/data"},
            ),
            Path("/srv/alice/data/permaevidence-codex"),
        )
        self.assertEqual(
            platform_support.runtime_data_root(
                home=home,
                platform="linux",
                environ={"XDG_DATA_HOME": "relative/data"},
            ),
            home / ".local/share/permaevidence-codex",
        )

    def test_linux_service_path_honors_xdg_config_home(self) -> None:
        path = platform_support.service_definition_path(
            home=Path("/home/alice"),
            platform="linux",
            environ={"XDG_CONFIG_HOME": "/srv/alice/config"},
        )
        self.assertEqual(
            path,
            Path("/srv/alice/config/systemd/user/permaevidence-codex-telegram-bridge.service"),
        )



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
