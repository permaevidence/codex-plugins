#!/usr/bin/env python3
"""Reliable JSON-lines reads for subprocess-based JSON-RPC clients."""

from __future__ import annotations

import json
import os
import select
import time
from collections import defaultdict, deque
from typing import Any, BinaryIO, TextIO


class JsonRpcLineReader:
    """Read JSON-RPC messages without mixing ``select`` with buffered reads.

    ``TextIOWrapper.readline()`` may read several lines from the OS pipe at
    once. A later ``select()`` then sees an empty pipe even though another line
    is waiting inside Python's private text buffer. Reading the descriptor
    directly keeps all read-ahead in this class, where it remains visible to
    subsequent waits.
    """

    def __init__(self, stream: TextIO | BinaryIO) -> None:
        self._fd = stream.fileno()
        self._buffer = bytearray()
        self._eof = False
        self._pending: dict[object, deque[dict[str, Any]]] = defaultdict(deque)

    def wait_for_id(self, process: Any, request_id: object, *, timeout: float) -> dict[str, Any]:
        pending = self._pending.get(request_id)
        if pending:
            return pending.popleft()

        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {}
            message = self._read_message(process, remaining)
            if message is None:
                return {}
            message_id = message.get("id")
            if message_id == request_id:
                return message
            if isinstance(message_id, (str, int, float)):
                self._pending[message_id].append(message)

    def _read_message(self, process: Any, timeout: float) -> dict[str, Any] | None:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            raw_line = self._pop_line()
            if raw_line is not None:
                if not raw_line.strip():
                    continue
                try:
                    payload = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if isinstance(payload, dict):
                    return payload
                continue

            if self._eof:
                return None

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            readable, _, _ = select.select([self._fd], [], [], min(0.5, remaining))
            if not readable:
                if process is not None and process.poll() is not None:
                    self._eof = True
                continue
            try:
                chunk = os.read(self._fd, 65536)
            except BlockingIOError:
                continue
            if chunk:
                self._buffer.extend(chunk)
            else:
                self._eof = True

    def _pop_line(self) -> bytes | None:
        newline = self._buffer.find(b"\n")
        if newline >= 0:
            line = bytes(self._buffer[:newline]).rstrip(b"\r")
            del self._buffer[: newline + 1]
            return line
        if self._eof and self._buffer:
            line = bytes(self._buffer).rstrip(b"\r")
            self._buffer.clear()
            return line
        return None
