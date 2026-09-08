#!/usr/bin/env python3
"""Update OpenAI Codex and safely activate it at stable macOS paths."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from codex_runtime import (
    CodexRuntimeError,
    rollback_stable_codex_runtime,
    stable_codex_command,
    sync_stable_codex_runtime,
)
from platform_support import runtime_data_root
from update_validation import maintenance_lock, validate_runtime, resolve_update_failure


INSTALLER_URL = "https://chatgpt.com/codex/install.sh"
RUNTIME_ROOT = runtime_data_root()
CURRENT_PLUGIN_RUNTIME = RUNTIME_ROOT / "current"
STATE_DIR = Path.home() / ".codex" / "telegram-bridge"
CONFIG_FILE = STATE_DIR / "config.json"
UPDATE_STATE_FILE = STATE_DIR / "codex_update_state.json"
HANDOFF_LOG = STATE_DIR / "codex-update-handoff.log"


def write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temp = Path(raw_path)
    try:
        with open(descriptor, "w", encoding="utf-8", closefd=True) as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def read_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def record_state(**fields: object) -> None:
    try:
        write_json_atomic(UPDATE_STATE_FILE, dict(fields))
    except OSError as exc:
        print(f"Could not persist Codex update state: {exc}", file=sys.stderr)


def schedule_deferred_update(delay: int, *, restart: bool = True) -> Path:
    if delay < 1:
        raise ValueError("Deferred update delay must be at least one second")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, str(Path(__file__).resolve()), "--run-after-delay", str(delay)]
    if not restart:
        command.append("--no-restart")
    with HANDOFF_LOG.open("ab") as log:
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    return HANDOFF_LOG


def install_official_codex() -> None:
    request = urllib.request.Request(INSTALLER_URL, headers={"User-Agent": "permaevidence-codex-updater"})
    with urllib.request.urlopen(request, timeout=60) as response:
        script = response.read()
    if not script.startswith(b"#!") or len(script) < 100:
        raise RuntimeError("The official Codex installer response was not a shell script.")
    with tempfile.TemporaryDirectory(prefix="codex-installer-") as tmp:
        installer = Path(tmp) / "install.sh"
        installer.write_bytes(script)
        installer.chmod(0o700)
        completed = subprocess.run(["/bin/sh", str(installer)], check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"The official Codex installer failed with exit code {completed.returncode}.")


def configure_stable_command() -> None:
    config = json.loads(CONFIG_FILE.read_bytes()) if CONFIG_FILE.exists() else {}
    if not isinstance(config, dict):
        raise RuntimeError("Telegram configuration must be a JSON object; existing file preserved.")
    config["codex_cmd"] = str(stable_codex_command())
    write_json_atomic(CONFIG_FILE, config)


def bridge_command(*parts: str) -> list[str]:
    bridge = CURRENT_PLUGIN_RUNTIME / "plugins/codex-telegram-bridge/scripts/bridge.py"
    if not bridge.is_file():
        raise RuntimeError(f"Telegram bridge operator is missing at {bridge}")
    return [sys.executable, str(bridge), *parts]


def run_checked(command: list[str]) -> None:
    print("+", " ".join(command), flush=True)
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"Command failed with exit code {completed.returncode}: {' '.join(command)}")


def perform_update(*, restart: bool = True) -> dict:
    with maintenance_lock(STATE_DIR):
        return _perform_update(restart=restart)


def _perform_update(*, restart: bool = True) -> dict:
    started = time.time()
    record_state(status="running", stage="preflight", started_at=started, announced=False)
    changed = False
    restarted = False
    stage = "preflight"
    original_config = None
    report = {}
    result = {}
    try:
        original_config = CONFIG_FILE.read_bytes() if CONFIG_FILE.exists() else None
        if original_config is not None and not isinstance(json.loads(original_config), dict):
            raise RuntimeError("Telegram configuration must be a JSON object; existing file preserved.")
        stage = "install"
        install_official_codex()
        stage = "synchronize"
        result = sync_stable_codex_runtime()
        changed = bool(result.get("changed"))
        stage = "configure"
        previous_command = read_json(CONFIG_FILE).get("codex_cmd")
        configure_stable_command()
        needs_restart = changed or previous_command != str(stable_codex_command())
        if restart:
            if needs_restart:
                stage = "activate"
                run_checked(bridge_command("restart", "--reason", "codex_update"))
                restarted = True
            stage = "validate"
            report = validate_runtime(bridge_command("doctor"))
        record_state(
            status="completed", stage="validated" if restart else "prepared",
            started_at=started, previous_version=result.get("previous_version"),
            version=result.get("version"), changed=changed, restarted=restarted,
            runtime_validated=report.get("runtime_validated", False),
            integration_status=report.get("integration_status", "not_checked"), doctor=report,
            completed_at=time.time(), announced=False,
        )
        return result
    except Exception as exc:
        rollback_error = ""
        try:
            if changed and result.get("previous_version"):
                rollback_stable_codex_runtime()
            # On first native install there is no previous stable package: restore
            # the old launcher configuration, preserving the verified staged package.
            if stage in {"configure", "activate", "validate"}:
                if original_config is None:
                    CONFIG_FILE.unlink(missing_ok=True)
                else:
                    write_json_atomic(CONFIG_FILE, json.loads(original_config))
            if restart and (changed or stage == "activate"):
                run_checked(bridge_command("restart", "--reason", "rollback"))
        except Exception as rollback_exc:
            rollback_error = str(rollback_exc)
        record_state(
            status="failed", stage=stage, started_at=started, error=str(exc),
            doctor=getattr(exc, "report", {}), rollback_error=rollback_error,
            version=result.get("version"), changed=changed,
            completed_at=time.time(), announced=False,
        )
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run OpenAI's standalone installer, verify signatures, and atomically update stable Codex paths."
    )
    parser.add_argument("--defer-seconds", type=int, default=0, help="Run once in a detached process after this delay.")
    parser.add_argument("--run-after-delay", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--revalidate", action="store_true", help="Run full doctor and resolve the last failure without changing its history or restarting.")
    parser.add_argument("--no-restart", action="store_true", help="Prepare the stable runtime without restarting the bridge.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.revalidate:
        with maintenance_lock(STATE_DIR):
            resolve_update_failure(UPDATE_STATE_FILE, bridge_command("doctor"))
        print("Last Codex CLI update failure resolved by full doctor; original history preserved.")
        return 0
    if args.defer_seconds:
        log = schedule_deferred_update(args.defer_seconds, restart=not args.no_restart)
        print(f"One-shot Codex update scheduled in {args.defer_seconds}s. Log: {log}")
        return 0
    if args.run_after_delay:
        time.sleep(args.run_after_delay)
    result = perform_update(restart=not args.no_restart)
    action = "installed" if result.get("changed") else "already current"
    print(f"Stable Codex {result.get('version')} is {action} at {result.get('root')}.")
    if args.no_restart:
        print("The bridge was not restarted. Grant Full Disk Access to the stable paths before restarting it.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except (CodexRuntimeError, RuntimeError) as exc:
        print(f"Codex update failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
