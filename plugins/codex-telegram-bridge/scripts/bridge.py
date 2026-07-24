#!/usr/bin/env python3
"""Small operator CLI for the Codex Telegram bridge supervisor."""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import plistlib
import pwd
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
import uuid
import wave
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SCRIPT_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = SCRIPT_DIR.parent
REPO_ROOT = PLUGIN_ROOT.parents[1]
ROOT_SCRIPTS = REPO_ROOT / "scripts"
if str(ROOT_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(ROOT_SCRIPTS))
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))
from lib.gmail_imap import probe_imap
from lib.audio_tools import ffmpeg_status
from jsonrpc_io import JsonRpcLineReader
from macos_permissions import codex_full_disk_access_status, codex_permission_installation
from platform_support import (
    LAUNCHD_LABEL,
    SYSTEMD_SERVICE_NAME,
    platform_family,
    systemd_user_dir,
)

START_SCRIPT = SCRIPT_DIR / "start_bridge.sh"
SERVICE_LABEL = LAUNCHD_LABEL
PLATFORM_FAMILY = platform_family()
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
LAUNCH_AGENT_FILE = LAUNCH_AGENTS_DIR / f"{SERVICE_LABEL}.plist"
SYSTEMD_USER_DIR = systemd_user_dir()
SYSTEMD_UNIT_FILE = SYSTEMD_USER_DIR / SYSTEMD_SERVICE_NAME

STATE_DIR = Path.home() / ".codex" / "telegram-bridge"
SUPERVISOR_PID_FILE = STATE_DIR / ".bridge-supervisor.pid"
CHILD_PID_FILE = STATE_DIR / ".bridge-child.pid"
STOP_FILE = STATE_DIR / ".stop-supervisor"
LAUNCH_LOCK_DIR = STATE_DIR / ".bridge-launch.lock"
LOG_FILE = STATE_DIR / "bridge.log"
CONFIG_FILE = STATE_DIR / "config.json"
ENV_FILE = STATE_DIR / ".env"
ACCESS_FILE = STATE_DIR / "access.json"
RUNTIME_STATE_FILE = STATE_DIR / "runtime_state.json"

MEMORY_CONFIG_FILE = Path.home() / ".codex" / "long-term-memory" / "config.json"
MEMORY_ENV_FILE = Path.home() / ".codex" / "long-term-memory" / ".env"
MEMORY_ALERT_FILE = Path.home() / ".codex" / "long-term-memory" / "pending" / "memory-maintenance.stuck.json"
CALENDAR_SOURCES_FILE = Path.home() / ".codex" / "long-term-memory" / "calendar_sources.json"
CALENDAR_CACHE_FILE = Path.home() / ".codex" / "long-term-memory" / "calendar_cache.json"
GMAIL_CONNECTOR_ID = "connector_2128aebfecb84f64a069897515042a44"
CALENDAR_CONNECTOR_ID = "connector_947e0d954944416db111db556030eea6"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start, stop, and inspect the Codex Telegram bridge.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("start", help="Start the bridge, using the installed user service when available.")
    subparsers.add_parser("stop", help="Stop the bridge supervisor cleanly.")
    subparsers.add_parser("quit", help="Alias for stop.")
    subparsers.add_parser("restart", help="Stop and then start the bridge supervisor.")
    subparsers.add_parser("status", help="Show bridge process/runtime status.")
    subparsers.add_parser("doctor", help="Check common setup problems.")
    doctor_parser = subparsers.choices["doctor"]
    doctor_parser.add_argument(
        "--allow-unpaired",
        action="store_true",
        help="Treat the expected first-run unpaired state as pending rather than a failure.",
    )
    doctor_parser.add_argument(
        "--allow-stopped",
        action="store_true",
        help="Do not fail when the user service and bridge are intentionally not started.",
    )
    doctor_parser.add_argument(
        "--allow-google-unconnected",
        action="store_true",
        help="Treat unconnected official Google apps as pending during first-time setup.",
    )
    subparsers.add_parser("install-service", help="Install and start the macOS launchd or Linux systemd user service.")
    subparsers.add_parser("uninstall-service", help="Stop and remove the platform user service.")

    logs = subparsers.add_parser("logs", help="Show bridge logs.")
    logs.add_argument("-f", "--follow", action="store_true", help="Follow new log lines.")
    logs.add_argument("-n", "--lines", type=int, default=80, help="Number of recent lines to show.")

    return parser.parse_args()


def read_pid(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return None
        return int(raw)
    except Exception:
        return None


def is_pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def process_command(pid: int | None) -> str:
    if not is_pid_alive(pid):
        return ""
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except Exception:
        return ""
    return (proc.stdout or "").strip()


def wait_dead(pid: int | None, timeout: float = 8.0) -> bool:
    if not pid:
        return True
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_pid_alive(pid):
            return True
        time.sleep(0.1)
    return not is_pid_alive(pid)


def terminate_pid(pid: int | None, *, timeout: float = 8.0) -> bool:
    if not is_pid_alive(pid):
        return True
    assert pid is not None
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return True
    if wait_dead(pid, timeout):
        return True
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        return True
    return wait_dead(pid, 2.0)


def supervisor_pid() -> int | None:
    return read_pid(SUPERVISOR_PID_FILE)


def child_pid() -> int | None:
    return read_pid(CHILD_PID_FILE)


def bridge_running() -> bool:
    return is_pid_alive(supervisor_pid())


def launch_domain() -> str:
    return f"gui/{os.getuid()}"


def service_installed() -> bool:
    if PLATFORM_FAMILY == "macos":
        return LAUNCH_AGENT_FILE.is_file()
    if PLATFORM_FAMILY == "linux":
        return SYSTEMD_UNIT_FILE.is_file()
    return False


def service_loaded() -> bool:
    if PLATFORM_FAMILY == "macos":
        command = ["launchctl", "print", f"{launch_domain()}/{SERVICE_LABEL}"]
    elif PLATFORM_FAMILY == "linux":
        command = ["systemctl", "--user", "is-active", "--quiet", SYSTEMD_SERVICE_NAME]
    else:
        return False
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def start_launch_service() -> int:
    if not LAUNCH_AGENT_FILE.exists():
        return 1
    was_loaded = service_loaded()
    if not was_loaded:
        completed = subprocess.run(
            ["launchctl", "bootstrap", launch_domain(), str(LAUNCH_AGENT_FILE)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            print((completed.stderr or completed.stdout or "launchctl bootstrap failed").strip(), file=sys.stderr)
            return completed.returncode
    else:
        subprocess.run(
            ["launchctl", "kickstart", "-k", f"{launch_domain()}/{SERVICE_LABEL}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    return wait_for_bridge_start()


def start_systemd_service() -> int:
    if not SYSTEMD_UNIT_FILE.exists():
        return 1
    try:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False, timeout=20)
        completed = subprocess.run(
            ["systemctl", "--user", "enable", "--now", SYSTEMD_SERVICE_NAME],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"Could not invoke systemctl: {exc}", file=sys.stderr)
        return 1
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "systemctl --user failed").strip()
        print(detail, file=sys.stderr)
        print(
            "A Linux user-service manager is required for unattended startup. "
            "On a normal systemd installation, log in as the target user and try again.",
            file=sys.stderr,
        )
        return completed.returncode
    return wait_for_bridge_start()


def start_platform_service() -> int:
    if PLATFORM_FAMILY == "macos":
        return start_launch_service()
    if PLATFORM_FAMILY == "linux":
        return start_systemd_service()
    print(f"Unsupported operating system: {sys.platform}", file=sys.stderr)
    return 1


def wait_for_bridge_start() -> int:
    deadline = time.time() + 12
    while time.time() < deadline:
        if bridge_running() and is_pid_alive(child_pid()):
            print(f"Bridge supervisor started (pid {supervisor_pid()}).")
            print(f"Log: {LOG_FILE}")
            return 0
        time.sleep(0.2)
    print("Bridge did not become healthy. Check the bridge log.", file=sys.stderr)
    return 1


def start_bridge() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if service_installed():
        return start_platform_service()
    pid = supervisor_pid()
    if is_pid_alive(pid):
        print(f"Bridge supervisor already running (pid {pid}).")
        return 0

    if not START_SCRIPT.exists():
        print(f"Missing supervisor script: {START_SCRIPT}", file=sys.stderr)
        return 1

    STOP_FILE.unlink(missing_ok=True)
    bash = shutil.which("bash")
    if not bash:
        print("bash is required to run the bridge supervisor.", file=sys.stderr)
        return 1
    subprocess.Popen(
        [bash, str(START_SCRIPT)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        cwd=str(PLUGIN_ROOT),
    )

    return wait_for_bridge_start()


def stop_bridge() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STOP_FILE.touch()

    if service_installed() or service_loaded():
        if PLATFORM_FAMILY == "macos":
            command = ["launchctl", "bootout", f"{launch_domain()}/{SERVICE_LABEL}"]
        elif PLATFORM_FAMILY == "linux":
            command = ["systemctl", "--user", "stop", SYSTEMD_SERVICE_NAME]
        else:
            command = []
        if command:
            try:
                subprocess.run(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=20,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass

    sup = supervisor_pid()
    child = child_pid()
    stopped = True

    if is_pid_alive(sup):
        print(f"Stopping bridge supervisor (pid {sup})...")
        stopped = terminate_pid(sup)
    else:
        print("Bridge supervisor is not running.")

    if is_pid_alive(child):
        print(f"Stopping bridge child (pid {child})...")
        stopped = terminate_pid(child) and stopped

    if stopped:
        for path in (SUPERVISOR_PID_FILE, CHILD_PID_FILE):
            pid = read_pid(path)
            if not is_pid_alive(pid):
                path.unlink(missing_ok=True)
        if not is_pid_alive(supervisor_pid()):
            try:
                if LAUNCH_LOCK_DIR.exists():
                    lock_pid = read_pid(LAUNCH_LOCK_DIR / "pid")
                    if not is_pid_alive(lock_pid):
                        shutil.rmtree(LAUNCH_LOCK_DIR, ignore_errors=True)
            except Exception:
                pass
        print("Bridge stopped.")
        return 0

    print("Bridge did not stop cleanly. Check the pid files and bridge log.", file=sys.stderr)
    return 1


def restart_bridge() -> int:
    stop_code = stop_bridge()
    if stop_code != 0:
        return stop_code
    return start_bridge()


def systemd_quote(value: str) -> str:
    escaped = value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def systemd_path(value: str) -> str:
    """Escape systemd specifiers while preserving a path directive literally."""
    return value.replace("%", "%%")


def build_systemd_unit(*, bash: str, path: str) -> str:
    return "\n".join(
        [
            "[Unit]",
            "Description=Perma Evidence Codex Telegram bridge",
            "",
            "[Service]",
            "Type=simple",
            f"ExecStart={systemd_quote(bash)} {systemd_quote(str(START_SCRIPT))}",
            f"WorkingDirectory={systemd_path(str(PLUGIN_ROOT))}",
            f"Environment={systemd_quote(f'HOME={Path.home()}')}",
            f"Environment={systemd_quote(f'PATH={path}')}",
            "Restart=always",
            "RestartSec=3",
            "KillMode=control-group",
            "TimeoutStopSec=25",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )


def enable_linux_linger() -> bool:
    """Best-effort boot startup for a user service on a headless Linux host."""
    loginctl = shutil.which("loginctl")
    if not loginctl:
        print(
            "Warning: loginctl was not found. The bridge will start with the user session, "
            "but automatic startup before login could not be enabled.",
            file=sys.stderr,
        )
        return False
    username = os.environ.get("USER") or pwd.getpwuid(os.getuid()).pw_name
    try:
        check = subprocess.run(
            [loginctl, "show-user", username, "--property=Linger", "--value"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"Warning: could not inspect systemd lingering: {exc}", file=sys.stderr)
        return False
    if check.returncode == 0 and check.stdout.strip().lower() == "yes":
        print(f"systemd lingering is enabled for {username}; the bridge can start before login.")
        return True
    try:
        enabled = subprocess.run(
            [loginctl, "enable-linger", username],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(
            f"Warning: could not enable systemd lingering for {username}: {exc}. "
            f"For startup before login, run: sudo loginctl enable-linger {shlex.quote(username)}",
            file=sys.stderr,
        )
        return False
    if enabled.returncode == 0:
        print(f"Enabled systemd lingering for {username}; the bridge can start before login.")
        return True
    detail = (enabled.stderr or enabled.stdout or "permission denied").strip()
    print(
        f"Warning: could not enable systemd lingering for {username}: {detail}. "
        f"For startup before login, run: sudo loginctl enable-linger {shlex.quote(username)}",
        file=sys.stderr,
    )
    return False


def install_service() -> int:
    if PLATFORM_FAMILY not in {"macos", "linux"}:
        print("Automatic bridge startup is supported on macOS and Linux.", file=sys.stderr)
        return 1
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    stop_bridge()
    path = os.environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin"
    bash = shutil.which("bash")
    if not bash:
        print("bash is required to run the bridge supervisor.", file=sys.stderr)
        return 1

    if PLATFORM_FAMILY == "macos":
        LAUNCH_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "Label": SERVICE_LABEL,
            "ProgramArguments": [bash, str(START_SCRIPT)],
            "WorkingDirectory": str(PLUGIN_ROOT),
            "RunAtLoad": True,
            "KeepAlive": True,
            "ProcessType": "Background",
            "EnvironmentVariables": {"PATH": path, "HOME": str(Path.home())},
            "StandardOutPath": str(STATE_DIR / "launchd.log"),
            "StandardErrorPath": str(STATE_DIR / "launchd-error.log"),
        }
        temp = LAUNCH_AGENT_FILE.with_suffix(".plist.tmp")
        temp.write_bytes(plistlib.dumps(payload, sort_keys=True))
        temp.chmod(0o600)
        temp.replace(LAUNCH_AGENT_FILE)
        print(f"Installed LaunchAgent: {LAUNCH_AGENT_FILE}")
        return start_launch_service()

    SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
    unit = build_systemd_unit(bash=bash, path=path)
    temp = SYSTEMD_UNIT_FILE.with_suffix(".service.tmp")
    temp.write_text(unit, encoding="utf-8")
    temp.chmod(0o600)
    temp.replace(SYSTEMD_UNIT_FILE)
    print(f"Installed systemd user service: {SYSTEMD_UNIT_FILE}")
    enable_linux_linger()
    return start_systemd_service()


def uninstall_service() -> int:
    stop_bridge()
    if PLATFORM_FAMILY == "macos":
        LAUNCH_AGENT_FILE.unlink(missing_ok=True)
        print(f"Removed LaunchAgent: {LAUNCH_AGENT_FILE}")
    elif PLATFORM_FAMILY == "linux":
        try:
            subprocess.run(
                ["systemctl", "--user", "disable", "--now", SYSTEMD_SERVICE_NAME],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=20,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        SYSTEMD_UNIT_FILE.unlink(missing_ok=True)
        try:
            subprocess.run(
                ["systemctl", "--user", "daemon-reload"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=20,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        print(f"Removed systemd user service: {SYSTEMD_UNIT_FILE}")
    return 0


def show_status() -> int:
    sup = supervisor_pid()
    child = child_pid()
    print("Codex Telegram bridge status")
    print(f"- supervisor: {format_pid(sup)}")
    if is_pid_alive(sup):
        print(f"  command: {process_command(sup)}")
    print(f"- child: {format_pid(child)}")
    if is_pid_alive(child):
        print(f"  command: {process_command(child)}")
    print(f"- config: {CONFIG_FILE if CONFIG_FILE.exists() else 'missing'}")
    print(f"- env: {ENV_FILE if ENV_FILE.exists() else 'missing'}")
    print(f"- log: {LOG_FILE if LOG_FILE.exists() else 'missing'}")
    if PLATFORM_FAMILY == "macos":
        print(f"- LaunchAgent: {'installed' if LAUNCH_AGENT_FILE.exists() else 'not installed'}")
        print(f"- launchd service: {'loaded' if service_loaded() else 'not loaded'}")
    elif PLATFORM_FAMILY == "linux":
        print(f"- systemd unit: {'installed' if SYSTEMD_UNIT_FILE.exists() else 'not installed'}")
        print(f"- systemd user service: {'active' if service_loaded() else 'not active'}")

    runtime = load_json(RUNTIME_STATE_FILE)
    if runtime:
        print(f"- active chat: {runtime.get('active_chat_id') or 'none'}")
        print(f"- active thread: {runtime.get('active_thread_id') or 'none'}")
        print(f"- last inbound message: {runtime.get('last_inbound_message_id') or 'none'}")
        print(f"- last turn status: {runtime.get('last_turn_status') or 'unknown'}")
    return 0


def format_pid(pid: int | None) -> str:
    if is_pid_alive(pid):
        return f"running pid {pid}"
    if pid:
        return f"stale pid {pid}"
    return "not running"


def show_logs(lines: int, follow: bool) -> int:
    if not LOG_FILE.exists():
        print(f"Missing bridge log: {LOG_FILE}", file=sys.stderr)
        return 1
    command = ["tail", "-n", str(max(lines, 0))]
    if follow:
        command.append("-f")
    command.append(str(LOG_FILE))
    return subprocess.call(command)


def doctor(
    *,
    allow_unpaired: bool = False,
    allow_stopped: bool = False,
    allow_google_unconnected: bool = False,
) -> int:
    checks: list[tuple[str, bool, str]] = []
    pending_steps: list[tuple[str, str]] = []
    notices: list[tuple[str, str]] = []

    codex = shutil.which("codex")
    checks.append(("codex CLI on PATH", bool(codex), codex or "missing"))
    if codex:
        version = run_text([codex, "--version"])
        checks.append(("codex version", bool(version), version or "unknown"))

    checks.append(("runtime marketplace file", (REPO_ROOT / ".agents/plugins/marketplace.json").exists(), str(REPO_ROOT)))
    plugin_list = run_text([codex, "plugin", "list"]) if codex else ""
    checks.append(
        (
            "long-term-memory plugin installed",
            "codex-long-term-memory@permaevidence-local" in plugin_list and "not installed" not in plugin_line(plugin_list, "codex-long-term-memory"),
            plugin_line(plugin_list, "codex-long-term-memory") or "not visible",
        )
    )

    checks.append(
        (
            "telegram bridge plugin installed",
            "codex-telegram-bridge@permaevidence-local" in plugin_list and "not installed" not in plugin_line(plugin_list, "codex-telegram-bridge"),
            plugin_line(plugin_list, "codex-telegram-bridge") or "not visible",
        )
    )

    mcp_list = run_text([codex, "mcp", "list"]) if codex else ""
    checks.append(("telegram-actions MCP visible", "telegram-actions" in mcp_list, "found" if "telegram-actions" in mcp_list else "not visible"))
    mcp_ok, mcp_detail = smoke_test_telegram_mcp()
    checks.append(("telegram-actions MCP initializes", mcp_ok, mcp_detail))

    config = load_json(CONFIG_FILE)
    google_apps_enabled = bool(config.get("enable_google_apps")) if isinstance(config, dict) else False
    if google_apps_enabled:
        gmail_line = plugin_line(plugin_list, "gmail")
        calendar_line = plugin_line(plugin_list, "google-calendar")
        checks.append(
            (
                "official Gmail plugin installed",
                bool(gmail_line) and "not installed" not in gmail_line,
                gmail_line or "not visible",
            )
        )
        checks.append(
            (
                "official Google Calendar plugin installed",
                bool(calendar_line) and "not installed" not in calendar_line,
                calendar_line or "not visible",
            )
        )
        apps = query_codex_apps(config.get("default_cwd"))
        for app_kind, label in (
            ("gmail", "Gmail app connected and available to Codex"),
            ("calendar", "Google Calendar app connected and available to Codex"),
        ):
            app = find_google_app(apps, app_kind)
            connected, detail = google_app_connection_status(app)
            if connected or not allow_google_unconnected:
                checks.append((label, connected, detail))
            else:
                pending_steps.append((label, f"{detail}; run `codex`, enter `/apps`, and connect it"))
    checks.append(("Telegram config.json", bool(config), str(CONFIG_FILE) if config else "missing or invalid"))
    if config:
        default_cwd = Path(str(config.get("default_cwd") or "")).expanduser()
        checks.append(("default_cwd exists", default_cwd.is_dir(), str(default_cwd) if str(default_cwd) else "not set"))
        checks.append(("sandbox mode set", bool(config.get("sandbox_mode")), str(config.get("sandbox_mode") or "not set")))
        telegram_timezone = str(config.get("timezone") or "").strip()
        try:
            ZoneInfo(telegram_timezone)
            timezone_ok = True
            timezone_detail = telegram_timezone
        except (ZoneInfoNotFoundError, ValueError):
            timezone_ok = False
            timezone_detail = telegram_timezone or "missing"
        checks.append(("Telegram timezone configured", timezone_ok, timezone_detail))
        if PLATFORM_FAMILY == "macos" and config.get("sandbox_mode") == "dangerFullAccess":
            installation = codex_permission_installation(
                str(config.get("codex_cmd") or codex or "")
            )
            installation_kind = str(installation.get("kind") or "")
            targets = list(installation.get("targets") or [])
            if installation_kind == "native" and len(targets) >= 2:
                full_disk = codex_full_disk_access_status(targets)
                if full_disk.get("state") == "granted":
                    checks.append(
                        (
                            "macOS Full Disk Access",
                            True,
                            "enabled for Codex and codex-code-mode-host",
                        )
                    )
                elif full_disk.get("state") == "unknown":
                    notices.append(
                        (
                            "macOS Full Disk Access",
                            str(full_disk.get("detail") or "could not inspect; confirm visually in System Settings"),
                        )
                    )
                else:
                    missing = ", ".join(str(path) for path in full_disk.get("missing") or targets)
                    pending_steps.append(
                        (
                            "macOS Full Disk Access",
                            (
                                f"{full_disk.get('detail') or 'not confirmed'} "
                                f"Current path(s): {missing}. Rerun the permissions section in "
                                f"`python3 {REPO_ROOT}/scripts/setup.py`."
                            ),
                        )
                    )
            elif installation_kind == "npm":
                pending_steps.append(
                    (
                        "macOS Full Disk Access",
                        "npm/Node Codex detected; use native Codex instead of granting broad access to shared Node",
                    )
                )
            else:
                pending_steps.append(
                    (
                        "macOS Full Disk Access",
                        str(installation.get("detail") or "native Codex executables could not be identified"),
                    )
                )

    env_values = load_env_keys(ENV_FILE)
    checks.append(("Telegram .env", ENV_FILE.exists(), str(ENV_FILE) if ENV_FILE.exists() else "missing"))
    checks.append(("TELEGRAM_BOT_TOKEN set", "TELEGRAM_BOT_TOKEN" in env_values, "set" if "TELEGRAM_BOT_TOKEN" in env_values else "missing"))
    checks.append(("Telegram OPENAI_API_KEY set", "OPENAI_API_KEY" in env_values, "set" if "OPENAI_API_KEY" in env_values else "missing; voice transcription will not work"))
    converter_ok, converter_detail = ffmpeg_status()
    checks.append(("Telegram voice-note converter", converter_ok, converter_detail))
    if config.get("enable_email_notifications"):
        gmail_email = _env_value(ENV_FILE, "GMAIL_IMAP_EMAIL")
        gmail_password = _env_value(ENV_FILE, "GMAIL_IMAP_APP_PASSWORD")
        checks.append(("Gmail IMAP email set", bool(gmail_email), "set" if gmail_email else "missing"))
        checks.append(("Gmail IMAP app password set", bool(gmail_password), "set" if gmail_password else "missing"))
        imap_ok, imap_detail = probe_imap(gmail_email, gmail_password)
        checks.append(("Gmail read-only IMAP access", imap_ok, imap_detail))
    telegram_ok, telegram_detail = smoke_test_telegram_api()
    checks.append(("Telegram bot API reachable", telegram_ok, telegram_detail))

    access = load_json(ACCESS_FILE)
    allow_list = access.get("allowFrom", []) if isinstance(access, dict) else []
    pairing_pending = access.get("pending", {}) if isinstance(access, dict) else {}
    if allow_unpaired and not allow_list:
        print("[PENDING] Telegram pairing: send the bot a DM, then approve the displayed code.")
    else:
        checks.append(("access.json", isinstance(access, dict), str(ACCESS_FILE) if isinstance(access, dict) else "missing or invalid"))
        checks.append(("at least one allowed chat", bool(allow_list), f"{len(allow_list)} allowed" if allow_list else "none yet"))
    if pairing_pending and not allow_unpaired:
        checks.append(("pending pairing codes", False, f"{len(pairing_pending)} pending; approve with scripts/access.py pair <code>"))

    memory_config = load_json(MEMORY_CONFIG_FILE)
    checks.append(("long-term-memory config", bool(memory_config), str(MEMORY_CONFIG_FILE) if memory_config else "missing or invalid"))
    memory_alert = load_json(MEMORY_ALERT_FILE)
    checks.append(
        (
            "memory maintenance not parked",
            not bool(memory_alert),
            "ready" if not memory_alert else f"parked: {str(memory_alert.get('last_error') or 'unknown error')[:300]}; use Telegram /retrymemory",
        )
    )
    memory_env_values = load_env_keys(MEMORY_ENV_FILE)
    checks.append(("memory OPENAI_API_KEY set", "OPENAI_API_KEY" in memory_env_values, "set" if "OPENAI_API_KEY" in memory_env_values else "missing; model-backed memory will not work"))
    openai_ok, openai_detail = smoke_test_openai_api()
    checks.append(("OpenAI API reachable", openai_ok, openai_detail))
    if memory_config:
        memory_timezone = str(memory_config.get("timezone") or "").strip()
        checks.append(
            (
                "memory and Telegram timezones agree",
                bool(memory_timezone) and memory_timezone == str(config.get("timezone") or "").strip(),
                f"memory={memory_timezone or 'missing'}; Telegram={str(config.get('timezone') or '').strip() or 'missing'}",
            )
        )
        transport = str(memory_config.get("injection_transport") or "")
        checks.append(("memory transport configured", bool(transport), transport or "not set"))
        if transport == "agents_md":
            agents_raw = str(memory_config.get("agents_md_path") or "").strip()
            agents_path = Path(agents_raw).expanduser() if agents_raw else None
            checks.append(("AGENTS.md memory target set", bool(agents_path and agents_path.is_file()), str(agents_path) if agents_path else "missing"))
        if memory_config.get("enable_calendar"):
            checks.append(
                (
                    "private calendar source configuration",
                    CALENDAR_SOURCES_FILE.is_file(),
                    str(CALENDAR_SOURCES_FILE) if CALENDAR_SOURCES_FILE.is_file() else "missing",
                )
            )
            calendar_ok, calendar_detail = smoke_test_calendar_feeds(memory_config)
            checks.append(("private iCal calendar retrieval", calendar_ok, calendar_detail))

    hook_records = query_memory_hooks(config.get("default_cwd") if config else None)
    expected_hooks = {"sessionStart", "userPromptSubmit", "preCompact", "stop"}
    trusted_hooks = {
        str(item.get("eventName"))
        for item in hook_records
        if item.get("enabled") and item.get("trustStatus") == "trusted"
    }
    checks.append(("memory hooks enabled and trusted", expected_hooks.issubset(trusted_hooks), f"{len(trusted_hooks & expected_hooks)}/4 trusted"))
    hooks_ok, hooks_detail = smoke_test_memory_hooks(hook_records, config.get("default_cwd") if config else None)
    checks.append(("memory hooks execute", hooks_ok, hooks_detail))

    if not allow_stopped:
        if PLATFORM_FAMILY == "macos":
            checks.append(("LaunchAgent installed", LAUNCH_AGENT_FILE.is_file(), str(LAUNCH_AGENT_FILE) if LAUNCH_AGENT_FILE.exists() else "missing"))
            checks.append(("launchd service loaded", service_loaded(), SERVICE_LABEL if service_loaded() else "not loaded"))
        elif PLATFORM_FAMILY == "linux":
            checks.append(("systemd user unit installed", SYSTEMD_UNIT_FILE.is_file(), str(SYSTEMD_UNIT_FILE) if SYSTEMD_UNIT_FILE.exists() else "missing"))
            checks.append(("systemd user service active", service_loaded(), SYSTEMD_SERVICE_NAME if service_loaded() else "not active"))
        else:
            checks.append(("supported operating system", False, sys.platform))

    if not allow_stopped:
        checks.append(("bridge supervisor running", bridge_running(), format_pid(supervisor_pid())))
        checks.append(("bridge child running", is_pid_alive(child_pid()), format_pid(child_pid())))
        child_command = process_command(child_pid())
        expected_bridge = str(SCRIPT_DIR / "telegram_bridge.py")
        checks.append(
            (
                "bridge child command valid",
                expected_bridge in child_command,
                child_command or "not running",
            )
        )
        app_server_ok, app_server_detail = app_server_child_status(child_pid())
        checks.append(("Codex app-server child running", app_server_ok, app_server_detail))

    failures = 0
    print("Codex Telegram bridge doctor")
    for label, ok, detail in checks:
        marker = "OK" if ok else "WARN"
        if not ok:
            failures += 1
        print(f"[{marker}] {label}: {detail}")
    for label, detail in pending_steps:
        print(f"[PENDING] {label}: {detail}")
    for label, detail in notices:
        print(f"[INFO] {label}: {detail}")

    print()
    if failures:
        print("Doctor found setup items to review.")
        print("Common fixes:")
        print(f"- First-time setup wizard: python3 {REPO_ROOT}/scripts/setup.py")
        print(f"- Install plugins: python3 {REPO_ROOT}/scripts/install_plugins.py")
        print(f"- Configure Telegram: edit {CONFIG_FILE} and {ENV_FILE}")
        print(f"- Start bridge: python3 {SCRIPT_DIR}/bridge.py start")
        if google_apps_enabled:
            print("- Connect Google apps: run `codex`, enter `/apps`, then connect Gmail and Google Calendar.")
        print("- After plugin or hook changes, start a new Codex thread or send /newsession.")
        return 1

    if pending_steps:
        print("Doctor checks passed; the clearly marked first-time connection steps remain pending.")
    else:
        print("Doctor checks passed.")
    return 0


def plugin_line(plugin_list: str, name: str) -> str:
    for line in plugin_list.splitlines():
        if line.strip().startswith(f"{name}@"):
            return " ".join(line.split())
    return ""


def google_app_connection_status(app: dict) -> tuple[bool, str]:
    if not app:
        return False, "not returned by Codex app/list"
    enabled = bool(app.get("isEnabled"))
    accessible = bool(app.get("isAccessible"))
    name = str(app.get("name") or "Google app")
    return enabled and accessible, f"{name}; enabled={enabled}; accessible={accessible}"


def find_google_app(apps: dict[str, dict], kind: str) -> dict:
    """Find an official Google app without depending solely on a connector ID."""

    normalized_kind = str(kind or "").strip().lower()
    known_id = {
        "gmail": GMAIL_CONNECTOR_ID,
        "calendar": CALENDAR_CONNECTOR_ID,
    }.get(normalized_kind, "")
    if known_id and apps.get(known_id):
        return apps[known_id]

    for app in apps.values():
        if not isinstance(app, dict):
            continue
        identity = " ".join(
            str(app.get(field) or "")
            for field in (
                "id",
                "name",
                "title",
                "displayName",
                "slug",
                "pluginName",
            )
        ).lower()
        compact = re.sub(r"[^a-z0-9]+", "", identity)
        if normalized_kind == "gmail" and "gmail" in compact:
            return app
        if normalized_kind == "calendar" and "googlecalendar" in compact:
            return app
    return {}


def run_text(command: list[str]) -> str:
    try:
        proc = subprocess.run(command, capture_output=True, text=True, check=False, timeout=20)
    except Exception:
        return ""
    return (proc.stdout or proc.stderr or "").strip()


def query_memory_hooks(cwd: str | None) -> list[dict]:
    codex = shutil.which("codex")
    working_dir = Path(str(cwd or Path.home())).expanduser()
    if not codex or not working_dir.is_dir():
        return []
    try:
        process = subprocess.Popen(
            [codex, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            cwd=str(working_dir),
        )
    except Exception:
        return []
    assert process.stdin is not None and process.stdout is not None
    reader = JsonRpcLineReader(process.stdout)
    try:
        for message in (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "permaevidence_doctor", "title": "Perma Evidence Doctor", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "initialized", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "hooks/list", "params": {}},
        ):
            process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()
        reply = reader.wait_for_id(process, 2, timeout=15)
        if reply:
            records: list[dict] = []
            for group in (reply.get("result") or {}).get("data", []):
                records.extend(
                    hook for hook in group.get("hooks", [])
                    if "codex-long-term-memory/hooks/" in str(hook.get("command") or "")
                )
            return records
    except Exception:
        return []
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
    return []


def query_codex_apps(cwd: str | None) -> dict[str, dict]:
    codex = shutil.which("codex")
    working_dir = Path(str(cwd or Path.home())).expanduser()
    if not codex or not working_dir.is_dir():
        return {}
    try:
        process = subprocess.Popen(
            [codex, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            cwd=str(working_dir),
        )
    except Exception:
        return {}
    assert process.stdin is not None and process.stdout is not None
    reader = JsonRpcLineReader(process.stdout)
    found: dict[str, dict] = {}
    try:
        process.stdin.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "clientInfo": {
                            "name": "permaevidence_doctor",
                            "title": "Perma Evidence Doctor",
                            "version": "1",
                        }
                    },
                }
            )
            + "\n"
        )
        process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "initialized", "params": {}}) + "\n")
        process.stdin.flush()
        cursor: str | None = None
        for page in range(6):
            request_id = 100 + page
            params: dict[str, object] = {"limit": 200, "forceRefetch": page == 0}
            if cursor:
                params["cursor"] = cursor
            process.stdin.write(
                json.dumps(
                    {"jsonrpc": "2.0", "id": request_id, "method": "app/list", "params": params}
                )
                + "\n"
            )
            process.stdin.flush()
            reply = reader.wait_for_id(process, request_id, timeout=10)
            if not reply or reply.get("error"):
                break
            result = reply.get("result") or {}
            for app in result.get("data", []):
                app_id = str(app.get("id") or "") if isinstance(app, dict) else ""
                if app_id:
                    found[app_id] = app
            cursor = str(result.get("nextCursor") or "") or None
            if not cursor:
                break
        return found
    except Exception:
        return found
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()

def smoke_test_calendar_feeds(config: dict) -> tuple[bool, str]:
    helper_path = REPO_ROOT / "plugins/codex-long-term-memory/lib/ical_calendar.py"
    try:
        spec = importlib.util.spec_from_file_location("permaevidence_calendar_doctor", helper_path)
        if spec is None or spec.loader is None:
            return False, f"could not load {helper_path}"
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        timezone_name = str(config.get("timezone") or "").strip()
        try:
            display_timezone = ZoneInfo(timezone_name) if timezone_name else datetime.now().astimezone().tzinfo
        except (ZoneInfoNotFoundError, ValueError):
            return False, f"invalid configured timezone: {timezone_name}"
        start = datetime.now().astimezone(display_timezone).replace(hour=0, minute=0, second=0, microsecond=0)
        _, report = module.fetch_calendar_events(
            CALENDAR_SOURCES_FILE,
            CALENDAR_CACHE_FILE,
            start,
            start + timedelta(days=max(1, int(config.get("calendar_days", 30)))),
            timeout=int(config.get("calendar_timeout_seconds", 15)),
            max_stale_seconds=int(config.get("calendar_cache_max_stale_seconds", 604800)),
        )
        status = str(report.get("status") or "")
        detail = f"{report.get('sources', 0)} source(s); stale={report.get('stale_sources', 0)}"
        return status in {"ok", "warning"}, detail
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def smoke_test_telegram_mcp() -> tuple[bool, str]:
    script = REPO_ROOT / "plugins/codex-telegram-bridge/scripts/telegram_actions_mcp.py"
    if not script.is_file():
        return False, f"missing {script}"
    try:
        process = subprocess.Popen(
            [sys.executable, str(script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert process.stdin is not None and process.stdout is not None
        reader = JsonRpcLineReader(process.stdout)
        for message in (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "doctor", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ):
            process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()
        reply = reader.wait_for_id(process, 2, timeout=10)
        if reply:
            names = {item.get("name") for item in (reply.get("result") or {}).get("tools", [])}
            expected = {"reply", "edit_message", "react", "download_attachment"}
            return expected.issubset(names), f"{len(names & expected)}/4 tools"
        return False, "tools/list timed out"
    except Exception as exc:
        return False, str(exc)
    finally:
        if "process" in locals():
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()


def smoke_test_memory_hooks(records: list[dict], cwd: str | None) -> tuple[bool, str]:
    if not records:
        return False, "no hooks discovered"
    failures: list[str] = []
    for record in records:
        event = str(record.get("eventName") or "unknown")
        command = str(record.get("command") or "")
        if not command:
            failures.append(f"{event}: missing command")
            continue
        try:
            completed = subprocess.run(
                shlex.split(command),
                input=json.dumps({"cwd": str(cwd or Path.home()), "thread_id": "doctor-smoke"}),
                capture_output=True,
                text=True,
                check=False,
                timeout=20,
                cwd=str(Path(str(cwd or Path.home())).expanduser()),
            )
            if completed.returncode != 0:
                failures.append(f"{event}: exit {completed.returncode}")
        except Exception as exc:
            failures.append(f"{event}: {exc}")
    return not failures, "4/4 executed" if not failures else "; ".join(failures)


def _env_value(path: Path, key: str) -> str:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                found, value = line.split("=", 1)
                if found.strip() == key:
                    return value.strip()
    except OSError:
        pass
    return ""


def smoke_test_telegram_api() -> tuple[bool, str]:
    token = _env_value(ENV_FILE, "TELEGRAM_BOT_TOKEN")
    if not token:
        return False, "token missing"
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getMe", timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return bool(payload.get("ok")), "getMe succeeded" if payload.get("ok") else "getMe rejected"
    except Exception as exc:
        return False, type(exc).__name__


def smoke_test_openai_api() -> tuple[bool, str]:
    memory_key = _env_value(MEMORY_ENV_FILE, "OPENAI_API_KEY")
    transcription_key = _env_value(ENV_FILE, "OPENAI_API_KEY")
    if not memory_key or not transcription_key:
        return False, "memory or Telegram OpenAI key missing"
    try:
        request = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(
                {
                    "model": "gpt-5.6-luna",
                    "input": "Reply with OK.",
                    "max_output_tokens": 32,
                    "store": False,
                }
            ).encode("utf-8"),
            method="POST",
            headers={"Authorization": f"Bearer {memory_key}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            if not 200 <= response.status < 300:
                return False, f"Responses API HTTP {response.status}"
        body, boundary = _transcription_probe_body()
        request = urllib.request.Request(
            "https://api.openai.com/v1/audio/transcriptions",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {transcription_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            ok = 200 <= response.status < 300
        return ok, "real Responses and transcription requests succeeded" if ok else f"Transcription API HTTP {response.status}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"[:300]


def _transcription_probe_body() -> tuple[bytes, str]:
    wav = io.BytesIO()
    with wave.open(wav, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x00" * 800)
    boundary = f"----PermaEvidence{uuid.uuid4().hex}"
    body = bytearray()
    body.extend(f"--{boundary}\r\n".encode())
    body.extend(b'Content-Disposition: form-data; name="model"\r\n\r\n')
    body.extend(b"gpt-4o-transcribe\r\n")
    body.extend(f"--{boundary}\r\n".encode())
    body.extend(b'Content-Disposition: form-data; name="file"; filename="health.wav"\r\n')
    body.extend(b"Content-Type: audio/wav\r\n\r\n")
    body.extend(wav.getvalue())
    body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode())
    return bytes(body), boundary


def app_server_child_status(parent_pid: int | None) -> tuple[bool, str]:
    if not is_pid_alive(parent_pid):
        return False, "bridge child not running"
    try:
        proc = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True, check=False, timeout=5
        )
        for line in proc.stdout.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) != 3:
                continue
            pid, ppid, command = parts
            if ppid == str(parent_pid) and "codex app-server" in command:
                return True, f"pid {pid}"
    except Exception as exc:
        return False, str(exc)
    return False, "no direct app-server child"


def load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_env_keys(path: Path) -> set[str]:
    keys: set[str] = set()
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if value.strip():
                keys.add(key.strip())
    except Exception:
        pass
    return keys


def main() -> int:
    args = parse_args()
    command = args.command
    if command == "start":
        return start_bridge()
    if command in {"stop", "quit"}:
        return stop_bridge()
    if command == "restart":
        return restart_bridge()
    if command == "status":
        return show_status()
    if command == "logs":
        return show_logs(args.lines, args.follow)
    if command == "doctor":
        return doctor(
            allow_unpaired=args.allow_unpaired,
            allow_stopped=args.allow_stopped,
            allow_google_unconnected=args.allow_google_unconnected,
        )
    if command == "install-service":
        return install_service()
    if command == "uninstall-service":
        return uninstall_service()
    raise AssertionError(f"unhandled command: {command}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        raise SystemExit(130)
