#!/usr/bin/env python3
"""Minimal read-only Gmail IMAP polling for proactive Telegram notices."""

from __future__ import annotations

import imaplib
from email import policy
from email.header import decode_header, make_header
from email.parser import BytesParser
from typing import Any, Callable


IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
HEADER_QUERY = "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])"


class GmailImapError(RuntimeError):
    """An IMAP configuration, authentication, or protocol failure."""


def normalize_app_password(value: str) -> str:
    return "".join(str(value or "").split())


def poll_unread_messages(
    email_address: str,
    app_password: str,
    state: dict[str, Any] | None,
    *,
    max_results: int = 10,
    client_factory: Callable[..., Any] = imaplib.IMAP4_SSL,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Return newly arrived unread message headers without changing read state.

    The first successful connection establishes a UID baseline and intentionally
    emits no notices, preventing a fresh install from flooding Telegram with an
    old unread backlog. UIDVALIDITY and RFC Message-ID values make restarts and
    mailbox resets safe.
    """

    address = str(email_address or "").strip()
    password = normalize_app_password(app_password)
    if not address or "@" not in address:
        raise GmailImapError("A valid Gmail address is required for IMAP polling.")
    if not password:
        raise GmailImapError("A Gmail app password is required for IMAP polling.")

    current = dict(state or {})
    client = None
    try:
        client = client_factory(IMAP_HOST, IMAP_PORT, timeout=30)
        status, _ = client.login(address, password)
        if status != "OK":
            raise GmailImapError("Gmail rejected the IMAP login.")
        status, _ = client.select("INBOX", readonly=True)
        if status != "OK":
            raise GmailImapError("Gmail would not open INBOX in read-only mode.")

        uid_validity = _response_number(client.response("UIDVALIDITY"))
        uid_next = _response_number(client.response("UIDNEXT"))
        newest_uid = uid_next - 1 if uid_next > 0 else max(_search_uids(client, "ALL"), default=0)
        prior_validity = int(current.get("uid_validity") or 0)
        initialized = bool(current.get("initialized"))

        if not initialized or (prior_validity and uid_validity and prior_validity != uid_validity):
            return [], {
                "initialized": True,
                "uid_validity": uid_validity,
                "last_uid": newest_uid,
                "notified_message_ids": [],
            }

        last_uid = int(current.get("last_uid") or 0)
        unseen_uids = sorted(
            uid
            for uid in _search_uids(client, f"UID {max(1, last_uid + 1)}:* UNSEEN")
            if uid > last_uid
        )
        candidates = unseen_uids[: max(1, int(max_results))]
        notified = {
            str(value)
            for value in current.get("notified_message_ids", [])
            if str(value).strip()
        }
        messages: list[dict[str, str]] = []
        for uid in candidates:
            raw_headers = _fetch_header_bytes(client, uid)
            message = BytesParser(policy=policy.default).parsebytes(raw_headers)
            message_id = _decoded_header(message.get("Message-ID"))
            stable_id = message_id or f"imap:{uid_validity}:{uid}"
            if stable_id in notified:
                continue
            notified.add(stable_id)
            messages.append(
                {
                    "uid": str(uid),
                    "message_id": message_id,
                    "stable_id": stable_id,
                    "from": _decoded_header(message.get("From")) or "?",
                    "subject": _decoded_header(message.get("Subject")) or "(no subject)",
                    "date": _decoded_header(message.get("Date")),
                }
            )

        backlog_remaining = len(unseen_uids) > len(candidates)
        highest_processed = max(candidates, default=last_uid)
        next_last_uid = highest_processed if backlog_remaining else max(highest_processed, newest_uid)
        return messages, {
            "initialized": True,
            "uid_validity": uid_validity,
            "last_uid": max(last_uid, next_last_uid),
            "notified_message_ids": sorted(notified)[-200:],
        }
    except GmailImapError:
        raise
    except imaplib.IMAP4.error as exc:
        detail = str(exc) or type(exc).__name__
        raise GmailImapError(f"Gmail IMAP authentication or protocol error: {detail}") from exc
    except (OSError, TimeoutError) as exc:
        raise GmailImapError(f"Could not reach Gmail IMAP: {type(exc).__name__}: {exc}") from exc
    except Exception as exc:
        raise GmailImapError(f"Gmail IMAP polling failed: {type(exc).__name__}: {exc}") from exc
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:
                pass


def probe_imap(
    email_address: str,
    app_password: str,
    *,
    client_factory: Callable[..., Any] = imaplib.IMAP4_SSL,
) -> tuple[bool, str]:
    try:
        poll_unread_messages(
            email_address,
            app_password,
            {},
            max_results=1,
            client_factory=client_factory,
        )
        return True, "read-only IMAP login succeeded"
    except GmailImapError as exc:
        return False, str(exc)


def _search_uids(client: Any, criterion: str) -> list[int]:
    status, payload = client.uid("search", None, criterion)
    if status != "OK":
        raise GmailImapError(f"Gmail IMAP search failed for {criterion}.")
    raw = payload[0] if payload else b""
    if isinstance(raw, str):
        raw = raw.encode("ascii", errors="ignore")
    values: list[int] = []
    for token in bytes(raw or b"").split():
        try:
            values.append(int(token))
        except ValueError:
            continue
    return values


def _fetch_header_bytes(client: Any, uid: int) -> bytes:
    status, payload = client.uid("fetch", str(uid), HEADER_QUERY)
    if status != "OK":
        raise GmailImapError(f"Gmail IMAP could not fetch message UID {uid}.")
    for item in payload or []:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
            return item[1]
        if isinstance(item, bytes) and b"\r\n" in item:
            return item
    raise GmailImapError(f"Gmail IMAP returned no headers for message UID {uid}.")


def _response_number(response: Any) -> int:
    if not isinstance(response, tuple) or len(response) < 2:
        return 0
    values = response[1] or []
    for value in values:
        if isinstance(value, bytes):
            value = value.decode("ascii", errors="ignore")
        try:
            return int(str(value).strip())
        except ValueError:
            continue
    return 0


def _decoded_header(value: Any) -> str:
    if value is None:
        return ""
    try:
        return " ".join(str(make_header(decode_header(str(value)))).split())
    except Exception:
        return " ".join(str(value).split())
