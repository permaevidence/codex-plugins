#!/usr/bin/env python3
"""Small operator CLI for the Codex Telegram bridge supervisor."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = SCRIPT_DIR.parent
REPO_ROOT = PLUGIN_ROOT.parents[1]
START_SCRIPT = SCRIPT_DIR / "start_bridge.sh"

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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start, stop, and inspect the Codex Telegram bridge.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("start", help="Start the singleton bridge supervisor in the background.")
    subparsers.add_parser("stop", help="Stop the bridge supervisor cleanly.")
    subparsers.add_parser("quit", help="Alias for stop.")
    subparsers.add_parser("restart", help="Stop and then start the bridge supervisor.")
    subparsers.add_parser("status", help="Show bridge process/runtime status.")
    subparsers.add_parser("doctor", help="Check common setup problems.")

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


def start_bridge() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    pid = supervisor_pid()
    if is_pid_alive(pid):
        print(f"Bridge supervisor already running (pid {pid}).")
        return 0

    if not START_SCRIPT.exists():
        print(f"Missing supervisor script: {START_SCRIPT}", file=sys.stderr)
        return 1

    STOP_FILE.unlink(missing_ok=True)
    subprocess.Popen(
        ["/bin/zsh", str(START_SCRIPT)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        cwd=str(PLUGIN_ROOT),
    )

    deadline = time.time() + 8
    while time.time() < deadline:
        pid = supervisor_pid()
        if is_pid_alive(pid):
            print(f"Bridge supervisor started (pid {pid}).")
            if LOG_FILE.exists():
                print(f"Log: {LOG_FILE}")
            return 0
        time.sleep(0.2)

    print("Bridge start was requested, but no live supervisor pid appeared.", file=sys.stderr)
    if LOG_FILE.exists():
        print(f"Check log: {LOG_FILE}", file=sys.stderr)
    return 1


def stop_bridge() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STOP_FILE.touch()

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


def doctor() -> int:
    checks: list[tuple[str, bool, str]] = []

    codex = shutil.which("codex")
    checks.append(("codex CLI on PATH", bool(codex), codex or "missing"))
    if codex:
        version = run_text([codex, "--version"])
        checks.append(("codex version", bool(version), version or "unknown"))

    checks.append(("repo marketplace file", (REPO_ROOT / ".agents/plugins/marketplace.json").exists(), str(REPO_ROOT)))
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

    config = load_json(CONFIG_FILE)
    checks.append(("Telegram config.json", bool(config), str(CONFIG_FILE) if config else "missing or invalid"))
    if config:
        default_cwd = Path(str(config.get("default_cwd") or "")).expanduser()
        checks.append(("default_cwd exists", default_cwd.is_dir(), str(default_cwd) if str(default_cwd) else "not set"))
        checks.append(("sandbox mode set", bool(config.get("sandbox_mode")), str(config.get("sandbox_mode") or "not set")))

    env_values = load_env_keys(ENV_FILE)
    checks.append(("Telegram .env", ENV_FILE.exists(), str(ENV_FILE) if ENV_FILE.exists() else "missing"))
    checks.append(("TELEGRAM_BOT_TOKEN set", "TELEGRAM_BOT_TOKEN" in env_values, "set" if "TELEGRAM_BOT_TOKEN" in env_values else "missing"))

    access = load_json(ACCESS_FILE)
    allow_list = access.get("allowFrom", []) if isinstance(access, dict) else []
    pending = access.get("pending", {}) if isinstance(access, dict) else {}
    checks.append(("access.json", isinstance(access, dict), str(ACCESS_FILE) if isinstance(access, dict) else "missing or invalid"))
    checks.append(("at least one allowed chat", bool(allow_list), f"{len(allow_list)} allowed" if allow_list else "none yet"))
    if pending:
        checks.append(("pending pairing codes", False, f"{len(pending)} pending; approve with scripts/access.py pair <code>"))

    memory_config = load_json(MEMORY_CONFIG_FILE)
    checks.append(("long-term-memory config", bool(memory_config), str(MEMORY_CONFIG_FILE) if memory_config else "missing or invalid"))
    if memory_config:
        transport = str(memory_config.get("injection_transport") or "")
        checks.append(("memory transport configured", bool(transport), transport or "not set"))
        if transport == "agents_md":
            agents_path = Path(str(memory_config.get("agents_md_path") or "")).expanduser()
            checks.append(("AGENTS.md memory target set", bool(str(agents_path)), str(agents_path) if str(agents_path) else "missing"))

    checks.append(("bridge supervisor running", bridge_running(), format_pid(supervisor_pid())))
    checks.append(("bridge child running", is_pid_alive(child_pid()), format_pid(child_pid())))

    failures = 0
    print("Codex Telegram bridge doctor")
    for label, ok, detail in checks:
        marker = "OK" if ok else "WARN"
        if not ok:
            failures += 1
        print(f"[{marker}] {label}: {detail}")

    print()
    if failures:
        print("Doctor found setup items to review.")
        print("Common fixes:")
        print(f"- Install plugins: python3 {REPO_ROOT}/scripts/install_plugins.py")
        print(f"- Configure Telegram: edit {CONFIG_FILE} and {ENV_FILE}")
        print(f"- Start bridge: python3 {SCRIPT_DIR}/bridge.py start")
        print("- After plugin or hook changes, start a new Codex thread or send /newsession.")
        return 1

    print("Doctor checks passed.")
    return 0


def plugin_line(plugin_list: str, name: str) -> str:
    for line in plugin_list.splitlines():
        if line.strip().startswith(f"{name}@"):
            return " ".join(line.split())
    return ""


def run_text(command: list[str]) -> str:
    try:
        proc = subprocess.run(command, capture_output=True, text=True, check=False, timeout=20)
    except Exception:
        return ""
    return (proc.stdout or proc.stderr or "").strip()


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
        return doctor()
    raise AssertionError(f"unhandled command: {command}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        raise SystemExit(130)
