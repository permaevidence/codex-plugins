#!/usr/bin/env python3
"""Safely update an installed Perma Evidence Codex runtime from an exact Git commit."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from platform_support import runtime_data_root

REPOSITORY = "permaevidence/codex-plugins"
APP_SUPPORT_ROOT = runtime_data_root()
CURRENT_LINK = APP_SUPPORT_ROOT / "current"
BRIDGE_STATE_DIR = Path.home() / ".codex" / "telegram-bridge"
UPDATE_STATE_FILE = BRIDGE_STATE_DIR / "update_state.json"
RECOVERY_QUEUE_FILE = BRIDGE_STATE_DIR / "turn_recovery_queue.json"


def write_json_atomic(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temp_path = Path(raw_path)
    try:
        with open(descriptor, "w", encoding="utf-8", closefd=True) as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)


def write_update_state(**fields: object) -> None:
    """Record update progress where the bridge can find it after restarting.

    The bridge announces the outcome to the owner chat on its next startup,
    so an update initiated from inside a Telegram turn still produces a
    visible confirmation even though the initiating turn dies with the old
    bridge process.
    """
    try:
        write_json_atomic(UPDATE_STATE_FILE, dict(fields))
    except OSError as exc:
        print(f"Could not persist update state: {exc}", file=sys.stderr)


def annotate_recovery_for_restart(queue_file: Path = RECOVERY_QUEUE_FILE) -> int:
    """Mark in-flight turn recovery records as interrupted by this update.

    The bridge restart is intentional, so records should retry promptly once
    the new bridge is up instead of waiting out their crash-recovery delay —
    and their reason should say what actually happened.
    """
    try:
        records = json.loads(queue_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(records, list):
        return 0
    changed = 0
    for record in records:
        if not isinstance(record, dict) or record.get("state") not in {"in_progress", "starting"}:
            continue
        record.update(
            {
                "state": "pending",
                "active_retry_turn_id": None,
                "due_at": time.time() + 90,
                "reason": "interrupted by runtime update restart",
            }
        )
        changed += 1
    if changed:
        write_json_atomic(queue_file, records)
    return changed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download, validate, and atomically activate the latest plugin runtime.")
    parser.add_argument("--ref", default="main", help="Git ref to resolve. Defaults to main.")
    parser.add_argument(
        "--defer-seconds",
        type=int,
        default=0,
        help="Run once in a detached process after this delay; safe for post-reply handoffs.",
    )
    parser.add_argument("--run-after-delay", type=int, default=0, help=argparse.SUPPRESS)
    return parser.parse_args()


def schedule_deferred_update(ref: str, delay: int) -> Path:
    """Start one detached updater process without registering a persistent service."""
    if delay < 1:
        raise ValueError("Deferred update delay must be at least one second")
    log_dir = Path.home() / ".codex" / "telegram-bridge"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "update-handoff.log"
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--ref",
        ref,
        "--run-after-delay",
        str(delay),
    ]
    with log_path.open("ab") as log:
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    return log_path


def run(command: list[str]) -> None:
    print("+", " ".join(command))
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"Command failed with exit code {completed.returncode}: {' '.join(command)}")


def resolve_commit(ref: str) -> str:
    request = urllib.request.Request(
        f"https://api.github.com/repos/{REPOSITORY}/commits/{ref}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "permaevidence-codex-updater"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    commit = str(payload.get("sha") or "")
    if len(commit) != 40:
        raise RuntimeError("GitHub did not return an immutable commit SHA")
    return commit


def import_module(path: Path, name: str):
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    if not spec or not spec.loader:
        raise RuntimeError(f"Could not load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def activate_runtime(source: Path, commit: str) -> Path:
    setup_module = import_module(source / "scripts/setup.py", "downloaded_setup_validation")
    setup_module.validate_source_runtime(source)
    runtime_module = import_module(source / "scripts/runtime_install.py", "downloaded_runtime_install")
    installed = runtime_module.install_runtime(source, cachebuster=f"commit-{commit[:12]}")
    metadata = {"repository": REPOSITORY, "commit": commit}
    (installed / "INSTALL-METADATA.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return installed


def configure_runtime(root: Path) -> None:
    setup = import_module(root / "scripts/setup.py", "installed_setup")
    config = setup.load_json(setup.TELEGRAM_CONFIG)
    install_command = [
        sys.executable,
        str(root / "scripts/install_plugins.py"),
        "--replace-marketplace",
    ]
    if config.get("enable_google_apps"):
        install_command.append("--with-google-apps")
    run(install_command)
    run([sys.executable, str(root / "plugins/codex-long-term-memory/scripts/install.py")])

    # Older releases used an external Workspace CLI for both background
    # integrations. Do not leave those toggles enabled unless replacement
    # credentials were actually configured through the setup wizard.
    env_email = setup.read_env_value(setup.TELEGRAM_ENV, "GMAIL_IMAP_EMAIL")
    env_password = setup.read_env_value(setup.TELEGRAM_ENV, "GMAIL_IMAP_APP_PASSWORD")
    if config.get("enable_email_notifications") and not (env_email and env_password):
        config["enable_email_notifications"] = False
        config["email_notification_provider"] = "imap"
        setup.write_json(setup.TELEGRAM_CONFIG, config)
        print("Disabled legacy email polling until Gmail IMAP is configured through setup.py.")

    memory_config = setup.load_json(setup.MEMORY_CONFIG)
    timezone_name = str(config.get("timezone") or memory_config.get("timezone") or "").strip()
    if not timezone_name:
        timezone_name = setup.detect_system_timezone()
    if config.get("timezone") != timezone_name:
        config["timezone"] = timezone_name
        setup.write_json(setup.TELEGRAM_CONFIG, config)
    if memory_config.get("timezone") != timezone_name:
        memory_config["timezone"] = timezone_name
        setup.write_json(setup.MEMORY_CONFIG, memory_config)
    print(f"Using {timezone_name} for prompt, Telegram, memory, and calendar timestamps.")
    if memory_config.get("enable_calendar") and not setup.CALENDAR_SOURCES.is_file():
        memory_config["enable_calendar"] = False
        memory_config["calendar_provider"] = "ical"
        setup.write_json(setup.MEMORY_CONFIG, memory_config)
        print("Disabled legacy calendar injection until a private iCal feed is configured through setup.py.")

    cwd = Path(str(config.get("default_cwd") or Path.home())).expanduser()
    agents_raw = str(memory_config.get("agents_md_path") or "").strip()
    agents_path = Path(agents_raw).expanduser() if agents_raw else cwd / "AGENTS.md"
    setup.write_local_capabilities_block(agents_path)
    setup.trust_memory_hooks(cwd)
    run([sys.executable, str(root / "plugins/codex-long-term-memory/scripts/update_agents_injection.py"), "--cwd", str(cwd)])
    bridge = root / "plugins/codex-telegram-bridge/scripts/bridge.py"
    # Stop the old bridge before touching the recovery queue: its in-process
    # lock does not extend to other processes, and it is about to die anyway.
    subprocess.run([sys.executable, str(bridge), "stop"], check=False)
    annotated = annotate_recovery_for_restart()
    if annotated:
        print(f"Marked {annotated} in-flight task(s) as interrupted by this update; they retry after restart.")
    run([sys.executable, str(bridge), "install-service"])
    run([sys.executable, str(bridge), "doctor"])


def restore(previous: Path | None) -> None:
    if not previous or not previous.exists():
        return
    temp_link = APP_SUPPORT_ROOT / ".current.rollback"
    temp_link.unlink(missing_ok=True)
    temp_link.symlink_to(previous)
    os.replace(temp_link, CURRENT_LINK)
    try:
        configure_runtime(CURRENT_LINK)
    except Exception as exc:
        print(f"Automatic rollback activation also needs attention: {exc}", file=sys.stderr)


def main() -> int:
    args = parse_args()
    if args.defer_seconds:
        log_path = schedule_deferred_update(args.ref, args.defer_seconds)
        print(f"One-shot update scheduled in {args.defer_seconds}s. Log: {log_path}")
        return 0
    if args.run_after_delay:
        time.sleep(args.run_after_delay)
    previous = CURRENT_LINK.resolve() if CURRENT_LINK.exists() else None
    commit = resolve_commit(args.ref)
    print(f"Resolved {args.ref} to {commit}")
    write_update_state(status="running", ref=args.ref, commit=commit, started_at=time.time(), announced=False)
    try:
        with tempfile.TemporaryDirectory(prefix="permaevidence-update-") as tmp:
            tmp_path = Path(tmp)
            archive = tmp_path / "source.zip"
            url = f"https://github.com/{REPOSITORY}/archive/{commit}.zip"
            with urllib.request.urlopen(url, timeout=60) as response, archive.open("wb") as handle:
                shutil.copyfileobj(response, handle)
            with zipfile.ZipFile(archive) as bundle:
                bundle.extractall(tmp_path / "source")
            roots = [path for path in (tmp_path / "source").iterdir() if path.is_dir()]
            if len(roots) != 1:
                raise RuntimeError("Downloaded archive had an unexpected layout")
            try:
                installed = activate_runtime(roots[0], commit)
                configure_runtime(installed)
                runtime_module = import_module(installed / "scripts/runtime_install.py", "installed_runtime_cleanup")
                runtime_module.prune_old_versions(active=installed.resolve())
            except Exception:
                restore(previous)
                raise
    except Exception as exc:
        write_update_state(
            status="failed",
            ref=args.ref,
            commit=commit,
            error=str(exc),
            completed_at=time.time(),
            announced=False,
        )
        raise
    write_update_state(status="completed", ref=args.ref, commit=commit, completed_at=time.time(), announced=False)
    print(f"Update complete at commit {commit[:12]}.")
    print("Send /newsession in Telegram before testing updated MCP tools or skills.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"Update failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
