import contextlib
import importlib.util
import io
import json
import shlex
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import update_validation as validation


def load(name):
    spec = importlib.util.spec_from_file_location("reliability_" + name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DoctorValidationTests(unittest.TestCase):
    def test_delayed_notification_preserves_resolution_and_new_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "update.json"
            observed = {"status": "failed", "completed_at": 123}
            path.write_text(json.dumps({**observed, "resolution": {"status": "recovered"}}))
            self.assertTrue(validation.mark_announced(path, observed))
            self.assertEqual(json.loads(path.read_text())["resolution"]["status"], "recovered")
            newer = {"status": "completed", "completed_at": 456}
            path.write_text(json.dumps(newer))
            self.assertFalse(validation.mark_announced(path, observed))
            self.assertEqual(json.loads(path.read_text()), newer)

    def report(self, scope="runtime", ok=True):
        return {"schema_version": 1, "checks": [{"label": "test check", "scope": scope, "ok": ok}]}

    def run_report(self, report, status, **kwargs):
        def run(command, **options):
            Path(command[-1]).write_text(json.dumps(report))
            return mock.Mock(returncode=status)
        with mock.patch.object(validation.subprocess, "run", side_effect=run):
            return validation.validate_runtime(["doctor"], **kwargs)

    def test_only_integration_failures_allow_degraded_completion(self):
        result = self.run_report(self.report("integration", False), 1)
        self.assertEqual(result["integration_status"], "degraded")
        self.assertTrue(result["runtime_validated"])
        with self.assertRaises(validation.DoctorFailure):
            self.run_report(self.report("runtime", False), 1)
        with self.assertRaises(validation.DoctorFailure):
            self.run_report(self.report("integration", False), 1, allow_degraded=False)

    def test_malformed_missing_and_inconsistent_reports_fail_closed(self):
        for report, status in (([], 1), ({}, 0), (self.report(), 1), (self.report("integration", False), 0), (self.report(), 75)):
            with self.subTest(report=report, status=status), self.assertRaises(validation.DoctorFailure):
                self.run_report(report, status)
        with mock.patch.object(validation.subprocess, "run", return_value=mock.Mock(returncode=0)):
            with self.assertRaises(validation.DoctorFailure):
                validation.validate_runtime(["doctor"])

    def test_resolution_preserves_failure_and_refuses_partial_or_changed_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "update.json"
            original = {"status": "failed", "stage": "validate", "error": "original", "completed_at": 123}
            path.write_text(json.dumps(original))
            with mock.patch.object(validation, "validate_runtime", return_value=self.report()) as doctor:
                with validation.maintenance_lock(Path(tmp)):
                    result = validation.resolve_update_failure(path, ["doctor"])
                doctor.assert_called_once_with(["doctor"], allow_degraded=False)
            for key, value in original.items():
                self.assertEqual(result[key], value)
            self.assertEqual(result["resolution"]["status"], "recovered")
            path.write_text(json.dumps(original))
            before = path.read_bytes()
            with mock.patch.object(validation, "validate_runtime", side_effect=validation.DoctorFailure("offline")):
                with self.assertRaises(validation.DoctorFailure):
                    validation.resolve_update_failure(path, ["doctor"])
            self.assertEqual(path.read_bytes(), before)
            def changed(*args, **kwargs):
                path.write_text('{"status":"running"}')
                return self.report()
            with mock.patch.object(validation, "validate_runtime", side_effect=changed):
                with self.assertRaisesRegex(RuntimeError, "changed"):
                    validation.resolve_update_failure(path, ["doctor"])
            self.assertEqual(json.loads(path.read_text()), {"status": "running"})

    def test_maintenance_lock_excludes_another_owner_and_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with validation.maintenance_lock(root):
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    with validation.maintenance_lock(root):
                        self.fail("second owner acquired lock")
            (root / ".update.lock").unlink()
            (root / ".update.lock").symlink_to(root / "target")
            with self.assertRaises(OSError):
                with validation.maintenance_lock(root):
                    self.fail("followed a symlink")


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.cli = load("update_codex")
        self.plugin = load("update")
        self.config = self.root / "config.json"
        self.config.write_text('{"codex_cmd":"/old/codex","keep":true}')
        for module, fields in ((self.cli, {"STATE_DIR": self.root, "CONFIG_FILE": self.config, "UPDATE_STATE_FILE": self.root / "cli-update.json"}),
                               (self.plugin, {"BRIDGE_STATE_DIR": self.root, "UPDATE_STATE_FILE": self.root / "plugin-update.json", "CURRENT_LINK": self.root / "current"})):
            for name, value in fields.items():
                self.stack.enter_context(mock.patch.object(module, name, value))

    def mock_cli(self, *, changed=True, report=None, failure=None):
        self.stack.enter_context(mock.patch.object(self.cli, "install_official_codex"))
        self.stack.enter_context(mock.patch.object(self.cli, "sync_stable_codex_runtime", return_value={"changed": changed, "previous_version": "1", "version": "2"}))
        self.stack.enter_context(mock.patch.object(self.cli, "stable_codex_command", return_value=Path("/stable/codex")))
        self.stack.enter_context(mock.patch.object(self.cli, "bridge_command", side_effect=lambda *parts: ["bridge", *parts]))
        self.run = self.stack.enter_context(mock.patch.object(self.cli, "run_checked"))
        self.rollback = self.stack.enter_context(mock.patch.object(self.cli, "rollback_stable_codex_runtime"))
        self.doctor = self.stack.enter_context(mock.patch.object(self.cli, "validate_runtime", return_value=report or {"runtime_validated": True, "integration_status": "ok"}, side_effect=failure))

    def test_degraded_discovery_does_not_rollback_cli(self):
        self.mock_cli(report={"runtime_validated": True, "integration_status": "degraded"})
        self.cli.perform_update()
        self.rollback.assert_not_called()
        state = json.loads(self.cli.UPDATE_STATE_FILE.read_text())
        self.assertEqual((state["status"], state["integration_status"]), ("completed", "degraded"))
        self.assertTrue(state["restarted"])

    def test_plugin_update_accepts_degraded_integrations_but_rolls_back_runtime_failure(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("source/README.md", "fixture")
        report = {"runtime_validated": True, "integration_status": "degraded"}
        with mock.patch.object(self.plugin, "resolve_commit", return_value="b" * 40), mock.patch.object(
            self.plugin, "preflight_native_runtime"
        ), mock.patch.object(self.plugin.urllib.request, "urlopen", side_effect=lambda *a, **k: io.BytesIO(archive.getvalue())), mock.patch.object(
            self.plugin, "activate_runtime", return_value=self.root
        ), mock.patch.object(self.plugin, "configure_runtime", return_value=report) as configure, mock.patch.object(
            self.plugin, "import_module", return_value=mock.Mock()
        ), mock.patch.object(self.plugin, "restore", return_value={"attempted": True, "validated": True}) as restore:
            self.assertEqual(self.plugin.update_from_ref("main"), 0)
            restore.assert_not_called()
            state = json.loads(self.plugin.UPDATE_STATE_FILE.read_text())
            self.assertEqual(state["integration_status"], "degraded")
            configure.side_effect = validation.DoctorFailure("runtime failed", {"checks": []})
            with self.assertRaises(validation.DoctorFailure):
                self.plugin.update_from_ref("main")
            restore.assert_called_once()
            state = json.loads(self.plugin.UPDATE_STATE_FILE.read_text())
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["doctor"], {"checks": []})
            self.assertTrue(state["rollback"]["validated"])

    def test_runtime_failure_rolls_back_and_preserves_stage(self):
        self.mock_cli(failure=validation.DoctorFailure("runtime broken", {"checks": []}))
        with self.assertRaises(validation.DoctorFailure):
            self.cli.perform_update()
        self.rollback.assert_called_once()
        self.assertEqual(json.loads(self.config.read_text())["codex_cmd"], "/old/codex")
        state = json.loads(self.cli.UPDATE_STATE_FILE.read_text())
        self.assertEqual(state["stage"], "validate")
        self.assertEqual(state["doctor"], {"checks": []})
        self.assertEqual(self.run.call_count, 2)

    def test_noop_does_not_restart_but_command_migration_does(self):
        self.mock_cli(changed=False)
        self.config.write_text('{"codex_cmd":"/stable/codex"}')
        self.cli.perform_update()
        self.run.assert_not_called()
        self.doctor.assert_called_once()
        self.config.write_text('{"codex_cmd":"/old/codex"}')
        self.cli.perform_update()
        self.run.assert_called_once()

    def test_native_preparation_does_not_restart_or_claim_live_validation(self):
        self.mock_cli()
        self.cli.perform_update(restart=False)
        self.run.assert_not_called()
        self.doctor.assert_not_called()
        state = json.loads(self.cli.UPDATE_STATE_FILE.read_text())
        self.assertEqual(state["stage"], "prepared")
        self.assertFalse(state["runtime_validated"])

    def test_invalid_configuration_is_preserved_before_native_install(self):
        self.mock_cli()
        for data in (b"broken JSON", b"[]"):
            self.config.write_bytes(data)
            with self.assertRaises((RuntimeError, ValueError)):
                self.cli.perform_update(restart=False)
            self.assertEqual(self.config.read_bytes(), data)
        self.cli.install_official_codex.assert_not_called()

    def test_npm_preflight_refuses_before_service_or_activation(self):
        self.config.write_text('{"sandbox_mode":"dangerFullAccess","codex_cmd":"/npm/codex"}')
        with mock.patch.object(self.plugin, "platform_family", return_value="macos"), mock.patch.object(
            self.plugin, "stable_runtime_status", return_value={"state": "missing"}
        ), mock.patch.object(self.plugin, "resolve_commit", return_value="a" * 40), mock.patch.object(
            self.plugin, "activate_runtime"
        ) as activate, mock.patch.object(self.plugin, "configure_runtime") as configure, mock.patch.object(
            self.plugin, "restore"
        ) as restore:
            with self.assertRaisesRegex(RuntimeError, "--no-restart"):
                self.plugin.update_from_ref("main")
        activate.assert_not_called()
        configure.assert_not_called()
        restore.assert_not_called()
        self.assertEqual(json.loads(self.plugin.UPDATE_STATE_FILE.read_text())["stage"], "preflight")

    def test_ready_native_preflight_and_linux_are_accepted(self):
        self.config.write_text('{"sandbox_mode":"dangerFullAccess","codex_cmd":"/stable/codex"}')
        with mock.patch.object(self.plugin, "platform_family", return_value="macos"), mock.patch.object(
            self.plugin, "stable_runtime_status", return_value={"state": "ready"}
        ), mock.patch.object(self.plugin, "stable_codex_command", return_value=Path("/stable/codex")):
            self.plugin.preflight_native_runtime()
        with mock.patch.object(self.plugin, "platform_family", return_value="linux"), mock.patch.object(
            self.plugin, "stable_runtime_status"
        ) as status:
            self.plugin.preflight_native_runtime()
            status.assert_not_called()


class SupervisorLoggingTests(unittest.TestCase):
    def test_real_supervisor_preserves_nonzero_and_signal_exit_status(self):
        source = (SCRIPTS.parent / "plugins/codex-telegram-bridge/scripts/start_bridge.sh").read_text()
        state_assignment = 'STATE_DIR="$HOME/.codex/telegram-bridge"'
        self.assertEqual(source.count(state_assignment), 1, "Supervisor isolation anchor changed; refuse to run")
        for ending, expected in (("sys.exit(23)", 23), ("os.kill(os.getpid(), signal.SIGTERM)", 143)):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                state = root / "state"
                state.mkdir()
                # The actual supervisor runs against a disposable child and state.
                script = root / "start_bridge.sh"
                isolated = source.replace(state_assignment, f"STATE_DIR={shlex.quote(str(state))}")
                self.assertNotIn(state_assignment, isolated)
                script.write_text(isolated)
                (root / "telegram_bridge.py").write_text(
                    "import os, sys, signal\nfrom pathlib import Path\n"
                    + f"Path({str(state / '.stop-supervisor')!r}).touch()\n" + ending + "\n"
                )
                result = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                log = (state / "bridge.log").read_text()
                self.assertIn(f"status={expected}", log)
                self.assertIn("supervisor_pid=", log)
                self.assertIn("bridge_pid=", log)


if __name__ == "__main__":
    unittest.main()
