#!/usr/bin/env python3
import json
import mimetypes
import os
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.telegram_api import (
    download_attachment_to_dir,
    fetch_telegram_file,
    send_chat_action,
    send_message,
    set_message_reaction,
    telegram_request,
)
from lib.telegram_common import (
    INBOX_DIR,
    load_access,
    load_chat_map,
    load_config,
    load_email_state,
    load_reminders,
    load_runtime_state,
    load_version_state,
    make_pair_code,
    save_access,
    save_chat_map,
    save_email_state,
    save_reminders,
    save_runtime_state,
    save_version_state,
)

EMAIL_CHECK_INTERVAL = 5 * 60
REMINDER_CHECK_INTERVAL = 60
VERSION_CHECK_INTERVAL = 5 * 60
TRANSCRIPTION_MAX_BYTES = 25 * 1024 * 1024


def normalize_command(text: str, bot_username: str) -> str:
    token = (text or "").strip().split()[0] if text else ""
    if not token.startswith("/"):
        return token
    lowered = token.lower()
    suffix = f"@{bot_username.lower()}"
    if lowered.endswith(suffix):
        return token[: -len(suffix)]
    return token


def is_mentioned(message: dict[str, Any], bot_username: str, extra_patterns: list[str]) -> bool:
    text = message.get("text") or message.get("caption") or ""
    entities = message.get("entities") or message.get("caption_entities") or []
    for entity in entities:
        entity_type = entity.get("type")
        offset = entity.get("offset", 0)
        length = entity.get("length", 0)
        if entity_type == "mention":
            mentioned = text[offset : offset + length]
            if mentioned.lower() == f"@{bot_username.lower()}":
                return True
        if entity_type == "text_mention":
            user = entity.get("user", {})
            if user.get("is_bot") and (user.get("username") or "").lower() == bot_username.lower():
                return True

    reply_from = ((message.get("reply_to_message") or {}).get("from") or {}).get("username")
    if (reply_from or "").lower() == bot_username.lower():
        return True

    for pattern in extra_patterns:
        try:
            if re.search(pattern, text, flags=re.IGNORECASE):
                return True
        except re.error:
            continue
    return False


def gate_message(message: dict[str, Any], token: str, bot_username: str) -> bool:
    access = load_access()
    chat = message.get("chat", {})
    chat_type = chat.get("type")
    chat_id = str(chat.get("id"))
    sender_id = str((message.get("from") or {}).get("id"))

    if access.get("dmPolicy") == "disabled":
        return False

    if chat_type == "private":
        if sender_id in access.get("allowFrom", []):
            return True
        if access.get("dmPolicy") == "allowlist":
            return False

        pending = access.setdefault("pending", {})
        for code, entry in pending.items():
            if str(entry.get("senderId")) == sender_id:
                send_message(token, chat_id, f"Pairing pending. Approve this code locally:\n{code}")
                return False

        code = make_pair_code()
        pending[code] = {
            "senderId": sender_id,
            "chatId": chat_id,
            "createdAt": int(time.time()),
        }
        save_access(access)
        send_message(
            token,
            chat_id,
            "This bot is locked down.\n"
            f"Approve this pairing code locally to allow this Telegram account:\n{code}",
        )
        return False

    if chat_type in {"group", "supergroup"}:
        policy = access.get("groups", {}).get(chat_id)
        if not policy:
            return False
        allow_from = policy.get("allowFrom") or []
        if allow_from and sender_id not in allow_from:
            return False
        require_mention = policy.get("requireMention", True)
        if require_mention and not is_mentioned(message, bot_username, access.get("mentionPatterns", [])):
            return False
        return True

    return False


def encode_multipart(fields: dict[str, str], file_bytes: bytes, filename: str, mime_type: str) -> tuple[bytes, str]:
    boundary = f"----CodexTelegramBridge{int(time.time() * 1000)}"
    body = bytearray()

    for name, value in fields.items():
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        body.extend(value.encode("utf-8"))
        body.extend(b"\r\n")

    body.extend(f"--{boundary}\r\n".encode("utf-8"))
    body.extend(
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode("utf-8")
    )
    body.extend(f"Content-Type: {mime_type}\r\n\r\n".encode("utf-8"))
    body.extend(file_bytes)
    body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode("utf-8"))

    return bytes(body), boundary


def transcribe_audio(file_bytes: bytes, filename: str, mime_type: str, api_key: str) -> str | None:
    if not api_key or len(file_bytes) > TRANSCRIPTION_MAX_BYTES:
        return None

    fields = {"model": "gpt-4o-transcribe"}
    payload, boundary = encode_multipart(fields, file_bytes, filename, mime_type)
    req = urllib.request.Request(
        "https://api.openai.com/v1/audio/transcriptions",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            result = json.loads(response.read().decode("utf-8"))
        return (result.get("text") or "").strip() or None
    except Exception:
        return None


class CodexAppServerClient:
    def __init__(self, config: dict[str, Any], send_callback) -> None:
        self.config = config
        self.send_callback = send_callback
        self.process = subprocess.Popen(
            [config["codex_cmd"], "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            text=True,
            bufsize=1,
        )
        self._lock = threading.Lock()
        self._next_id = 1
        self._pending: dict[int, queue.Queue] = {}
        self._turns: dict[str, dict[str, Any]] = {}
        self._active_turn_by_chat: dict[str, str] = {}
        self._thread_to_chat: dict[str, str] = {}
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._initialize()

    def _initialize(self) -> None:
        self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "permaevidence_telegram",
                    "title": "Perma Evidence Telegram Bridge",
                    "version": "0.2.0",
                }
            },
        )
        self.notify("initialized", {})

    def request(self, method: str, params: dict[str, Any]) -> Any:
        request_id = self._reserve_id()
        reply_queue: queue.Queue = queue.Queue(maxsize=1)
        self._pending[request_id] = reply_queue
        self._send_json(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        result = reply_queue.get()
        if "error" in result:
            raise RuntimeError(result["error"])
        return result["result"]

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send_json({"jsonrpc": "2.0", "method": method, "params": params})

    def _reserve_id(self) -> int:
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            return request_id

    def _send_json(self, payload: dict[str, Any]) -> None:
        if self.process.stdin is None:
            raise RuntimeError("app-server stdin is unavailable")
        line = json.dumps(payload, ensure_ascii=False)
        with self._lock:
            self.process.stdin.write(line + "\n")
            self.process.stdin.flush()

    def _read_loop(self) -> None:
        if self.process.stdout is None:
            return
        for raw_line in self.process.stdout:
            line = raw_line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._handle_message(message)

    def _handle_message(self, message: dict[str, Any]) -> None:
        if "id" in message and "method" not in message:
            pending = self._pending.pop(message["id"], None)
            if pending is not None:
                pending.put(message)
            return

        if "id" in message and "method" in message:
            self._handle_server_request(message)
            return

        method = message.get("method")
        params = message.get("params", {})

        if method == "item/agentMessage/delta":
            state = self._turns.get(params.get("turnId"))
            if state is not None:
                state["text"] += params.get("delta", "")
        elif method == "item/completed":
            item = params.get("item", {})
            if item.get("type") == "agentMessage":
                state = self._turns.get(params.get("turnId"))
                if state is not None:
                    state["text"] = item.get("text", state["text"])
        elif method == "turn/completed":
            turn = params.get("turn", {})
            turn_id = turn.get("id")
            if turn_id in self._turns:
                self._finish_turn(turn_id, turn)
        elif method == "error":
            error = params.get("error", {})
            turn_id = params.get("turnId")
            thread_id = params.get("threadId")
            if turn_id and turn_id in self._turns:
                self._turns[turn_id]["error"] = error.get("message", "Unknown error")
            elif thread_id:
                chat_id = self._thread_to_chat.get(thread_id)
                if chat_id:
                    self.send_callback(chat_id, f"Codex error: {error.get('message', 'Unknown error')}")

    def _handle_server_request(self, message: dict[str, Any]) -> None:
        method = message["method"]
        request_id = message["id"]
        if method == "item/commandExecution/requestApproval":
            self._send_json({"jsonrpc": "2.0", "id": request_id, "result": {"decision": "acceptForSession"}})
            return
        if method == "item/fileChange/requestApproval":
            self._send_json({"jsonrpc": "2.0", "id": request_id, "result": {"decision": "acceptForSession"}})
            return
        if method == "item/permissions/requestApproval":
            permissions = (message.get("params") or {}).get("permissions", {})
            self._send_json(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"permissions": permissions, "scope": "session"},
                }
            )
            return
        if method == "item/tool/requestUserInput":
            auto_answers = build_auto_user_input_answers(message.get("params") or {})
            if auto_answers:
                self._send_json({"jsonrpc": "2.0", "id": request_id, "result": {"answers": auto_answers}})
            else:
                self._send_json({"jsonrpc": "2.0", "id": request_id, "result": {"answers": {}}})
            params = message.get("params", {})
            chat_id = self._thread_to_chat.get(params.get("threadId"))
            if chat_id:
                if auto_answers:
                    self.send_callback(chat_id, "Codex requested interactive tool input. The bridge auto-answered with the default options.")
                else:
                    self.send_callback(
                        chat_id,
                        "Codex asked for interactive tool input, and the bridge could not infer safe answers.",
                    )
            return
        self._send_json(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"Unsupported server request: {method}"},
            }
        )

    def ensure_thread(self, chat_id: str, chat_map: dict[str, Any]) -> str:
        entry = chat_map.get(chat_id)
        if entry and entry.get("thread_id"):
            thread_id = entry["thread_id"]
            if thread_id not in self._thread_to_chat:
                self.request("thread/resume", {"threadId": thread_id})
            self._thread_to_chat[thread_id] = chat_id
            return thread_id

        result = self.request("thread/start", {})
        thread_id = result["thread"]["id"]
        chat_map[chat_id] = {"thread_id": thread_id, "created_at": time.time()}
        self._thread_to_chat[thread_id] = chat_id
        return thread_id

    def new_thread(self, chat_id: str, chat_map: dict[str, Any]) -> str:
        result = self.request("thread/start", {})
        thread_id = result["thread"]["id"]
        chat_map[chat_id] = {"thread_id": thread_id, "created_at": time.time()}
        self._thread_to_chat[thread_id] = chat_id
        self._active_turn_by_chat.pop(chat_id, None)
        return thread_id

    def start_turn(self, chat_id: str, thread_id: str, text: str) -> str:
        result = self.request("turn/start", self._turn_params(thread_id, text))
        turn = result["turn"]
        turn_id = turn["id"]
        self._turns[turn_id] = {
            "chat_id": chat_id,
            "thread_id": thread_id,
            "text": "",
            "started_at": time.time(),
            "status": turn.get("status", "inProgress"),
            "error": None,
        }
        self._active_turn_by_chat[chat_id] = turn_id
        save_runtime_state(
            {
                **load_runtime_state(),
                "active_chat_id": chat_id,
                "active_thread_id": thread_id,
                "active_turn_id": turn_id,
                "updated_at": time.time(),
            }
        )
        return turn_id

    def steer_turn(self, chat_id: str, text: str) -> str | None:
        turn_id = self._active_turn_by_chat.get(chat_id)
        if not turn_id:
            return None
        state = self._turns.get(turn_id)
        if not state:
            return None
        self.request(
            "turn/steer",
            {
                "threadId": state["thread_id"],
                "input": [{"type": "text", "text": text}],
                "expectedTurnId": turn_id,
            },
        )
        save_runtime_state(
            {
                **load_runtime_state(),
                "active_chat_id": chat_id,
                "active_thread_id": state["thread_id"],
                "active_turn_id": turn_id,
                "updated_at": time.time(),
            }
        )
        return turn_id

    def interrupt_turn(self, chat_id: str) -> bool:
        turn_id = self._active_turn_by_chat.get(chat_id)
        if not turn_id:
            return False
        state = self._turns.get(turn_id)
        if not state:
            return False
        self.request("turn/interrupt", {"threadId": state["thread_id"]})
        save_runtime_state(
            {
                **load_runtime_state(),
                "active_chat_id": chat_id,
                "active_thread_id": state["thread_id"],
                "active_turn_id": turn_id,
                "last_turn_status": "interruptRequested",
                "updated_at": time.time(),
            }
        )
        return True

    def inject_external_message(self, chat_id: str, chat_map: dict[str, Any], text: str) -> None:
        if self.steer_turn(chat_id, text):
            return
        thread_id = self.ensure_thread(chat_id, chat_map)
        self.start_turn(chat_id, thread_id, text)

    def status_text(self, chat_id: str, chat_map: dict[str, Any]) -> str:
        entry = chat_map.get(chat_id)
        version = current_codex_version(str(self.config.get("codex_cmd") or "codex"))
        last_seen = ""
        if entry and entry.get("last_seen_at"):
            try:
                age = int(time.time() - float(entry["last_seen_at"]))
                last_seen = f"\nLast inbound message: {age}s ago"
            except Exception:
                last_seen = ""
        turn_id = self._active_turn_by_chat.get(chat_id)
        if turn_id and turn_id in self._turns:
            state = self._turns[turn_id]
            elapsed = int(time.time() - state["started_at"])
            suffix = f"\nCLI: {version}" if version else ""
            return (
                f"Active turn: {turn_id}\nThread: {state['thread_id']}\nRunning for: {elapsed}s"
                f"{last_seen}{suffix}"
            )
        if entry and entry.get("thread_id"):
            suffix = f"\nCLI: {version}" if version else ""
            return f"Idle.\nCurrent thread: {entry['thread_id']}{last_seen}{suffix}"
        suffix = f"\nCLI: {version}" if version else ""
        return f"Idle.\nNo thread has been created for this chat yet.{suffix}"

    def _turn_params(self, thread_id: str, text: str) -> dict[str, Any]:
        return {
            "threadId": thread_id,
            "input": [{"type": "text", "text": text}],
            "approvalPolicy": self.config.get("approval_policy", "never"),
            "model": self.config.get("model"),
            "effort": self.config.get("effort"),
            "cwd": self.config.get("default_cwd"),
            "personality": self.config.get("personality"),
            "sandboxPolicy": self._sandbox_policy(),
        }

    def _sandbox_policy(self) -> dict[str, Any]:
        mode = self.config.get("sandbox_mode", "workspaceWrite")
        if mode == "dangerFullAccess":
            return {"type": "dangerFullAccess"}
        if mode == "readOnly":
            return {"type": "readOnly", "networkAccess": bool(self.config.get("network_access", False))}
        writable_roots = [str(Path(root).expanduser()) for root in self.config.get("writable_roots", [])]
        return {
            "type": "workspaceWrite",
            "networkAccess": bool(self.config.get("network_access", False)),
            "writableRoots": writable_roots,
        }

    def _finish_turn(self, turn_id: str, turn: dict[str, Any]) -> None:
        state = self._turns.get(turn_id)
        if state is None:
            return
        state["status"] = turn.get("status")
        chat_id = state["chat_id"]
        self._active_turn_by_chat.pop(chat_id, None)

        text = state["text"].strip()
        status = turn.get("status")
        runtime_state = load_runtime_state()
        runtime_state["active_chat_id"] = chat_id
        runtime_state["active_thread_id"] = state["thread_id"]
        runtime_state["active_turn_id"] = None
        runtime_state["last_turn_status"] = status
        runtime_state["updated_at"] = time.time()
        save_runtime_state(runtime_state)
        if status == "completed":
            self.send_callback(chat_id, text or "Turn completed with no final assistant text.")
        elif status == "interrupted":
            self.send_callback(chat_id, "Turn interrupted.")
        elif status == "failed":
            self.send_callback(chat_id, f"Turn failed: {state.get('error') or 'Unknown error'}")
        else:
            self.send_callback(chat_id, f"Turn ended with status: {status}")


def maybe_start_reminder_loop(
    config: dict[str, Any],
    codex: CodexAppServerClient,
    chat_map: dict[str, Any],
    chat_map_lock: threading.Lock,
) -> None:
    if not config.get("enable_reminders", True):
        return

    def worker() -> None:
        while True:
            try:
                reminders = load_reminders()
                if reminders:
                    now = time.time()
                    kept: list[dict[str, Any]] = []
                    changed = False
                    for reminder in reminders:
                        if reminder.get("fired"):
                            changed = True
                            continue
                        due = parse_due(reminder.get("due"))
                        if due is None or now < due:
                            kept.append(reminder)
                            continue

                        chat_id = str(reminder.get("chat_id") or config.get("owner_chat_id") or "")
                        prompt = str(reminder.get("prompt") or "").strip()
                        if chat_id and prompt:
                            event_text = f'[SYSTEM EVENT source="reminder"] {prompt}'
                            with chat_map_lock:
                                codex.inject_external_message(chat_id, chat_map, event_text)
                                save_chat_map(chat_map)

                        recurring = reminder.get("recurring")
                        if recurring:
                            reminder["due"] = advance_recurring(reminder["due"], recurring)
                            kept.append(reminder)
                        changed = True

                    if changed:
                        save_reminders(kept)
            except Exception as exc:
                print(f"reminder loop failed: {exc}", file=sys.stderr)
            time.sleep(REMINDER_CHECK_INTERVAL)

    threading.Thread(target=worker, daemon=True).start()


def parse_due(value: Any) -> float | None:
    if not value:
        return None
    text = str(value).strip()
    try:
        return time.mktime(time.strptime(text, "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        try:
            return time.mktime(time.strptime(text, "%Y-%m-%dT%H:%M"))
        except ValueError:
            return None


def advance_recurring(due: str, interval: str) -> str:
    current = parse_due(due) or time.time()
    if interval == "daily":
        current += 24 * 60 * 60
    elif interval == "weekly":
        current += 7 * 24 * 60 * 60
    elif interval == "monthly":
        current += 30 * 24 * 60 * 60
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(current))


def maybe_start_email_loop(
    config: dict[str, Any],
    codex: CodexAppServerClient,
    chat_map: dict[str, Any],
    chat_map_lock: threading.Lock,
) -> None:
    if not config.get("enable_email_notifications"):
        return
    if not config.get("owner_chat_id"):
        return
    if shutil_which("gws") is None:
        return

    def worker() -> None:
        while True:
            try:
                owner_chat_id = str(config.get("owner_chat_id"))
                state = load_email_state()
                notified_ids = set(state.get("notified_ids", []))
                since = int(time.time() - EMAIL_CHECK_INTERVAL)
                proc = subprocess.run(
                    [
                        "gws",
                        "gmail",
                        "+triage",
                        "--max",
                        "10",
                        "--query",
                        f"is:unread after:{since}",
                        "--format",
                        "json",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=90,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    data = json.loads(proc.stdout)
                    messages = data.get("messages") or []
                    fresh = []
                    for message in messages:
                        message_id = str(message.get("id") or message.get("threadId") or "")
                        if message_id and message_id in notified_ids:
                            continue
                        fresh.append(message)
                        if message_id:
                            notified_ids.add(message_id)
                    if fresh:
                        lines = []
                        for message in fresh:
                            sender = message.get("from", "?")
                            subject = message.get("subject", "(no subject)")
                            date = message.get("date", "")
                            lines.append(f"From: {sender}\nSubject: {subject}\nDate: {date}")
                        content = "[SYSTEM EVENT source=\"email\"] New unread email(s):\n\n" + "\n\n".join(lines)
                        with chat_map_lock:
                            codex.inject_external_message(owner_chat_id, chat_map, content)
                            save_chat_map(chat_map)
                        save_email_state({"notified_ids": sorted(notified_ids)[-100:]})
            except Exception as exc:
                print(f"email loop failed: {exc}", file=sys.stderr)
            time.sleep(EMAIL_CHECK_INTERVAL)

    threading.Thread(target=worker, daemon=True).start()


def maybe_start_version_monitor_loop(
    config: dict[str, Any],
    token: str,
    codex: CodexAppServerClient,
    chat_map: dict[str, Any],
    chat_map_lock: threading.Lock,
) -> None:
    owner_chat_id = str(config.get("owner_chat_id") or "").strip()
    codex_cmd = str(config.get("codex_cmd") or "codex")
    if not owner_chat_id:
        return

    state = load_version_state()
    initial_version = current_codex_version(codex_cmd)
    if initial_version:
        state["last_known_version"] = initial_version
        save_version_state(state)

    def worker() -> None:
        while True:
            try:
                current_version = current_codex_version(codex_cmd)
                if not current_version:
                    time.sleep(VERSION_CHECK_INTERVAL)
                    continue

                version_state = load_version_state()
                previous_version = str(version_state.get("last_known_version") or "").strip()
                if not previous_version:
                    version_state["last_known_version"] = current_version
                    save_version_state(version_state)
                elif previous_version != current_version:
                    version_state["last_known_version"] = current_version
                    save_version_state(version_state)
                    send_message(
                        token,
                        owner_chat_id,
                        (
                            f"Codex CLI updated on disk:\n{previous_version} -> {current_version}\n\n"
                            "The running bridge may still be using the older app-server process. "
                            "Consider /newsession if you want new turns to start from a fresh state."
                        ),
                    )
                    with chat_map_lock:
                        codex.inject_external_message(
                            owner_chat_id,
                            chat_map,
                            (
                                f'[SYSTEM EVENT source="version-monitor"] Codex CLI changed from '
                                f"{previous_version} to {current_version}. "
                                "Audit for hook, app-server, or approval-flow regressions and report any findings."
                            ),
                        )
                        save_chat_map(chat_map)
            except Exception as exc:
                print(f"version monitor failed: {exc}", file=sys.stderr)
            time.sleep(VERSION_CHECK_INTERVAL)

    threading.Thread(target=worker, daemon=True).start()


def shutil_which(name: str) -> str | None:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / name
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def current_codex_version(codex_cmd: str) -> str | None:
    try:
        proc = subprocess.run(
            [codex_cmd, "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except Exception:
        return None
    output = (proc.stdout or proc.stderr or "").strip()
    return output or None


def get_bot_username(token: str) -> str:
    result = telegram_request(token, "getMe", {})
    return ((result.get("result") or {}).get("username") or "").strip()


def extract_message_text(message: dict[str, Any], token: str, config: dict[str, Any]) -> str:
    text = (message.get("text") or message.get("caption") or "").strip()

    voice = message.get("voice")
    if voice:
        if config.get("enable_voice_transcription", True) and config.get("openai_api_key"):
            fetched = fetch_telegram_file(token, voice.get("file_id"))
            if fetched is not None:
                file_bytes, filename, mime_type = fetched
                transcript = transcribe_audio(file_bytes, filename, mime_type, str(config.get("openai_api_key")))
                if transcript:
                    return f"🎤 {transcript}"
        return text or "(voice message)"

    audio = message.get("audio")
    if audio:
        return text or f"(audio: {audio.get('title') or audio.get('file_name') or 'audio'})"

    video = message.get("video")
    if video:
        return text or "(video)"

    video_note = message.get("video_note")
    if video_note:
        return text or "(video note)"

    document = message.get("document")
    if document:
        return text or f"(document: {document.get('file_name') or 'document'})"

    photo = message.get("photo")
    if photo:
        return text or "(photo)"

    sticker = message.get("sticker")
    if sticker:
        emoji = str(sticker.get("emoji") or "").strip()
        return text or (f"(sticker {emoji})" if emoji else "(sticker)")

    return text


def extract_attachment_meta(message: dict[str, Any], token: str) -> dict[str, Any]:
    photo = message.get("photo")
    if isinstance(photo, list) and photo:
        selected = photo[-1]
        file_id = str(selected.get("file_id") or "").strip()
        if file_id:
            image_path = download_attachment_to_dir(token, file_id, INBOX_DIR, preferred_name="telegram-photo.jpg")
            meta: dict[str, Any] = {
                "attachment_kind": "photo",
                "attachment_file_id": file_id,
            }
            if image_path:
                meta["image_path"] = image_path
            size = selected.get("file_size")
            if size is not None:
                meta["attachment_size"] = str(size)
            return meta

    document = message.get("document")
    if isinstance(document, dict) and document.get("file_id"):
        return attachment_meta(
            kind="document",
            file_id=document.get("file_id"),
            size=document.get("file_size"),
            mime=document.get("mime_type"),
            name=document.get("file_name"),
        )

    audio = message.get("audio")
    if isinstance(audio, dict) and audio.get("file_id"):
        return attachment_meta(
            kind="audio",
            file_id=audio.get("file_id"),
            size=audio.get("file_size"),
            mime=audio.get("mime_type"),
            name=audio.get("file_name") or audio.get("title"),
        )

    video = message.get("video")
    if isinstance(video, dict) and video.get("file_id"):
        return attachment_meta(
            kind="video",
            file_id=video.get("file_id"),
            size=video.get("file_size"),
            mime=video.get("mime_type"),
            name=video.get("file_name"),
        )

    video_note = message.get("video_note")
    if isinstance(video_note, dict) and video_note.get("file_id"):
        return attachment_meta(
            kind="video_note",
            file_id=video_note.get("file_id"),
            size=video_note.get("file_size"),
            mime=video_note.get("mime_type"),
            name="video-note.mp4",
        )

    voice = message.get("voice")
    if isinstance(voice, dict) and voice.get("file_id"):
        return attachment_meta(
            kind="voice",
            file_id=voice.get("file_id"),
            size=voice.get("file_size"),
            mime=voice.get("mime_type"),
            name="voice.ogg",
        )

    return {}


def attachment_meta(kind: str, file_id: Any, size: Any, mime: Any, name: Any) -> dict[str, Any]:
    meta = {
        "attachment_kind": kind,
        "attachment_file_id": str(file_id or ""),
    }
    if size not in (None, ""):
        meta["attachment_size"] = str(size)
    if mime:
        meta["attachment_mime"] = safe_attr(str(mime))
    if name:
        meta["attachment_name"] = safe_name(str(name))
    return meta


def build_channel_message(message: dict[str, Any], text: str, attachment: dict[str, Any]) -> str:
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    attrs = {
        "source": "telegram",
        "chat_id": str(chat.get("id") or ""),
        "message_id": str(message.get("message_id") or ""),
        "user": str(sender.get("id") or ""),
        "ts": str(message.get("date") or ""),
    }
    attrs.update({key: str(value) for key, value in attachment.items() if value not in (None, "")})
    attr_text = " ".join(f'{key}="{safe_attr(value)}"' for key, value in attrs.items() if value)
    body = text.strip() or "(empty message)"
    return f"<channel {attr_text}>{body}"


def safe_attr(value: str) -> str:
    return value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def safe_name(value: str) -> str:
    return re.sub(r'[<>\[\]\r\n;"]', "_", value)


def build_auto_user_input_answers(params: dict[str, Any]) -> dict[str, str]:
    questions = params.get("questions")
    if not isinstance(questions, list):
        request = params.get("request")
        if isinstance(request, dict):
            questions = request.get("questions")
    if not isinstance(questions, list):
        return {}

    answers: dict[str, str] = {}
    for question in questions:
        if not isinstance(question, dict):
            continue
        question_id = str(question.get("id") or question.get("key") or question.get("name") or "").strip()
        if not question_id:
            continue
        options = question.get("options")
        if isinstance(options, list) and options:
            first_option = options[0]
            if isinstance(first_option, dict):
                answer = first_option.get("label") or first_option.get("value") or first_option.get("id") or ""
            else:
                answer = str(first_option)
        else:
            answer = question.get("default") or ""
        answers[question_id] = str(answer)
    return {key: value for key, value in answers.items() if value != ""}


def help_text() -> str:
    return (
        "Available commands:\n"
        "/help - show this message\n"
        "/status - show current Codex status\n"
        "/stop - interrupt the active turn\n"
        "/newsession - start a fresh Codex thread\n\n"
        "Text, voice, photos, and file metadata are forwarded to Codex."
    )


def main() -> None:
    config = load_config()
    token = config.get("bot_token")
    if not token:
        raise SystemExit(
            "Missing Telegram bot token. Set bot_token in ~/.codex/telegram-bridge/config.json or TELEGRAM_BOT_TOKEN in ~/.codex/telegram-bridge/.env."
        )

    bot_username = get_bot_username(str(token))
    chat_map = load_chat_map()
    chat_map_lock = threading.Lock()
    offset = 0

    def send_callback(chat_id: str, text: str) -> None:
        access = load_access()
        with chat_map_lock:
            entry = dict(chat_map.get(chat_id, {}))
        reply_to_message_id = entry.get("last_message_id")
        if not isinstance(reply_to_message_id, int):
            reply_to_message_id = None
        sent = send_message(
            str(token),
            chat_id,
            text,
            reply_to_message_id=reply_to_message_id,
            access=access,
        )
        if sent:
            with chat_map_lock:
                state_entry = chat_map.setdefault(chat_id, {})
                state_entry["last_outbound_message_ids"] = [
                    int(item["message_id"]) for item in sent if isinstance(item.get("message_id"), int)
                ]
                if state_entry["last_outbound_message_ids"]:
                    state_entry["last_outbound_message_id"] = state_entry["last_outbound_message_ids"][-1]
                save_chat_map(chat_map)
                save_runtime_state(
                    {
                        **load_runtime_state(),
                        "active_chat_id": chat_id,
                        "last_outbound_message_id": state_entry.get("last_outbound_message_id"),
                        "last_outbound_message_ids": state_entry.get("last_outbound_message_ids", []),
                        "last_inbound_message_id": state_entry.get("last_message_id"),
                        "updated_at": time.time(),
                    }
                )

    codex = CodexAppServerClient(config, send_callback)
    maybe_start_reminder_loop(config, codex, chat_map, chat_map_lock)
    maybe_start_email_loop(config, codex, chat_map, chat_map_lock)
    maybe_start_version_monitor_loop(config, str(token), codex, chat_map, chat_map_lock)

    print("Codex Telegram bridge is running.")
    while True:
        try:
            result = telegram_request(
                str(token),
                "getUpdates",
                {
                    "timeout": "30",
                    "offset": str(offset),
                },
            )
        except Exception as exc:
            print(f"telegram poll failed: {exc}", file=sys.stderr)
            time.sleep(3)
            continue

        for update in result.get("result", []):
            try:
                offset = max(offset, int(update["update_id"]) + 1)
                message = update.get("message") or update.get("edited_message")
                if not message:
                    continue

                if not gate_message(message, str(token), bot_username):
                    continue

                chat = message.get("chat", {})
                chat_id = str(chat.get("id"))
                sender_id = str((message.get("from") or {}).get("id"))
                text = extract_message_text(message, str(token), config).strip()
                attachment = extract_attachment_meta(message, str(token))
                access = load_access()

                with chat_map_lock:
                    entry = chat_map.setdefault(chat_id, {})
                    entry["last_message_id"] = message.get("message_id")
                    entry["last_sender_id"] = sender_id
                    entry["last_seen_at"] = time.time()
                    entry["chat_type"] = chat.get("type")
                    save_chat_map(chat_map)
                    save_runtime_state(
                        {
                            **load_runtime_state(),
                            "active_chat_id": chat_id,
                            "last_inbound_message_id": message.get("message_id"),
                            "last_sender_id": sender_id,
                            "chat_type": chat.get("type"),
                            "updated_at": time.time(),
                        }
                    )

                if not text:
                    send_message(str(token), chat_id, "This message type is not supported yet.", access=access)
                    continue

                command = normalize_command(text, bot_username)
                if command.startswith("/") and chat.get("type") != "private":
                    continue
                if command in {"/start", "/help"}:
                    send_message(str(token), chat_id, help_text(), message.get("message_id"), access=access)
                    continue
                if command == "/status":
                    send_message(
                        str(token),
                        chat_id,
                        codex.status_text(chat_id, chat_map),
                        message.get("message_id"),
                        access=access,
                    )
                    continue
                if command == "/newsession":
                    with chat_map_lock:
                        thread_id = codex.new_thread(chat_id, chat_map)
                        save_chat_map(chat_map)
                    send_message(
                        str(token),
                        chat_id,
                        f"Started a new Codex thread:\n{thread_id}",
                        message.get("message_id"),
                        access=access,
                    )
                    continue
                if command == "/stop":
                    if codex.interrupt_turn(chat_id):
                        send_message(str(token), chat_id, "Interrupt requested.", message.get("message_id"), access=access)
                    else:
                        send_message(
                            str(token),
                            chat_id,
                            "No active turn to interrupt.",
                            message.get("message_id"),
                            access=access,
                        )
                    continue

                text = build_channel_message(message, text, attachment)

                send_chat_action(str(token), chat_id, "typing")
                if message.get("message_id") is not None and access.get("ackReaction"):
                    set_message_reaction(
                        str(token),
                        chat_id,
                        int(message["message_id"]),
                        str(access.get("ackReaction") or ""),
                    )

                active_turn = codex.steer_turn(chat_id, text)
                if active_turn:
                    send_message(
                        str(token),
                        chat_id,
                        "Added your message to the active turn.",
                        message.get("message_id"),
                        access=access,
                    )
                    continue

                with chat_map_lock:
                    thread_id = codex.ensure_thread(chat_id, chat_map)
                    save_chat_map(chat_map)
                    save_runtime_state(
                        {
                            **load_runtime_state(),
                            "active_chat_id": chat_id,
                            "active_thread_id": thread_id,
                            "updated_at": time.time(),
                        }
                    )
                    codex.start_turn(chat_id, thread_id, text)
                send_message(
                    str(token),
                    chat_id,
                    "Sent to Codex. Use /status or /stop while it runs.",
                    message.get("message_id"),
                    access=access,
                )
            except Exception as exc:
                print(f"telegram update handling failed: {exc}", file=sys.stderr)
                traceback.print_exc()
                chat_id = str((update.get("message") or update.get("edited_message") or {}).get("chat", {}).get("id", ""))
                if chat_id:
                    try:
                        send_message(str(token), chat_id, f"Bridge error: {exc}")
                    except Exception:
                        pass


if __name__ == "__main__":
    main()
