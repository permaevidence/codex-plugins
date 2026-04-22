#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BRIDGE_SCRIPT="$SCRIPT_DIR/telegram_bridge.py"
STATE_DIR="$HOME/.codex/telegram-bridge"
SUPERVISOR_PID_FILE="$STATE_DIR/.bridge-supervisor.pid"
CHILD_PID_FILE="$STATE_DIR/.bridge-child.pid"
STOP_FILE="$STATE_DIR/.stop-supervisor"
LOG_FILE="$STATE_DIR/bridge.log"
LAUNCH_LOCK_DIR="$STATE_DIR/.bridge-launch.lock"

mkdir -p "$STATE_DIR"
child_pid=""

is_pid_alive() {
  local pid="$1"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

wait_for_pid_exit() {
  local pid="$1"
  local attempts="${2:-50}"
  local i=0
  while is_pid_alive "$pid" && (( i < attempts )); do
    sleep 0.1
    ((i += 1))
  done
  ! is_pid_alive "$pid"
}

kill_tree() {
  local pid="$1"
  local child
  if ! is_pid_alive "$pid"; then
    return 0
  fi
  while read -r child; do
    [[ -n "$child" ]] || continue
    kill_tree "$child"
  done < <(pgrep -P "$pid" 2>/dev/null || true)
  kill "$pid" 2>/dev/null || true
  wait_for_pid_exit "$pid" 20 || kill -9 "$pid" 2>/dev/null || true
}

cleanup_stale_bridge_processes() {
  local pid
  while read -r pid; do
    [[ -n "$pid" ]] || continue
    [[ "$pid" == "$$" ]] && continue
    kill_tree "$pid"
  done < <(pgrep -f "$BRIDGE_SCRIPT" 2>/dev/null || true)
}

if ! mkdir "$LAUNCH_LOCK_DIR" 2>/dev/null; then
  lock_pid=""
  if [[ -f "$LAUNCH_LOCK_DIR/pid" ]]; then
    lock_pid="$(cat "$LAUNCH_LOCK_DIR/pid" 2>/dev/null || true)"
  fi
  if is_pid_alive "$lock_pid"; then
    echo "Bridge supervisor launch already in progress or running (pid $lock_pid)." >&2
    exit 0
  fi
  rm -rf "$LAUNCH_LOCK_DIR"
  mkdir "$LAUNCH_LOCK_DIR"
fi
echo "$$" > "$LAUNCH_LOCK_DIR/pid"

if [[ -f "$SUPERVISOR_PID_FILE" ]]; then
  existing_supervisor="$(cat "$SUPERVISOR_PID_FILE" 2>/dev/null || true)"
  if is_pid_alive "$existing_supervisor"; then
    echo "Bridge supervisor already running (pid $existing_supervisor)." >&2
    rm -rf "$LAUNCH_LOCK_DIR"
    exit 0
  fi
  rm -f "$SUPERVISOR_PID_FILE"
fi

if [[ -f "$CHILD_PID_FILE" ]]; then
  existing_child="$(cat "$CHILD_PID_FILE" 2>/dev/null || true)"
  if is_pid_alive "$existing_child"; then
    kill_tree "$existing_child"
  fi
  rm -f "$CHILD_PID_FILE"
fi

cleanup_stale_bridge_processes

rm -f "$STOP_FILE"
echo "$$" > "$SUPERVISOR_PID_FILE"

cleanup() {
  if [[ -n "$child_pid" ]] && kill -0 "$child_pid" 2>/dev/null; then
    kill_tree "$child_pid"
  fi
  rm -f "$CHILD_PID_FILE" "$SUPERVISOR_PID_FILE"
  rm -rf "$LAUNCH_LOCK_DIR"
}

trap 'touch "$STOP_FILE"; cleanup; exit 0' INT TERM

while true; do
  date +"[%Y-%m-%d %H:%M:%S] starting bridge" >> "$LOG_FILE"
  python3 "$BRIDGE_SCRIPT" >> "$LOG_FILE" 2>&1 &
  child_pid="$!"
  echo "$child_pid" > "$CHILD_PID_FILE"
  wait "$child_pid" || true
  rm -f "$CHILD_PID_FILE"

  if [[ -f "$STOP_FILE" ]]; then
    break
  fi

  date +"[%Y-%m-%d %H:%M:%S] bridge exited; restarting in 3s" >> "$LOG_FILE"
  sleep 3
done

cleanup
