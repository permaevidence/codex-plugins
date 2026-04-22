#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BRIDGE_SCRIPT="$SCRIPT_DIR/telegram_bridge.py"
STATE_DIR="$HOME/.codex/telegram-bridge"
SUPERVISOR_PID_FILE="$STATE_DIR/.bridge-supervisor.pid"
CHILD_PID_FILE="$STATE_DIR/.bridge-child.pid"
STOP_FILE="$STATE_DIR/.stop-supervisor"
LOG_FILE="$STATE_DIR/bridge.log"

mkdir -p "$STATE_DIR"
rm -f "$STOP_FILE"
echo "$$" > "$SUPERVISOR_PID_FILE"

child_pid=""

cleanup() {
  if [[ -n "$child_pid" ]] && kill -0 "$child_pid" 2>/dev/null; then
    kill "$child_pid" 2>/dev/null || true
    wait "$child_pid" 2>/dev/null || true
  fi
  rm -f "$CHILD_PID_FILE" "$SUPERVISOR_PID_FILE"
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
