from __future__ import annotations

import imaplib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.gmail_imap import (
    IMAP_TIMEOUT_SECONDS,
    GmailImapError,
    normalize_app_password,
    poll_unread_messages,
)


class FakeImap:
    all_uids = [1, 2]
    unseen_uids = [2]
    uid_validity = 55
    headers = {
        2: (
            b"From: Alice Example <alice@example.com>\r\n"
            b"Subject: Project update\r\n"
            b"Date: Mon, 13 Jul 2026 09:00:00 -0400\r\n"
            b"Message-ID: <message-2@example.com>\r\n\r\n"
        )
    }
    last_readonly = None
    last_fetch_query = None
    last_timeout = None
    search_criteria = []

    def __init__(self, host, port, timeout):
        self.host = host
        self.port = port
        self.timeout = timeout
        type(self).last_timeout = timeout
        self.readonly = False

    def login(self, address, password):
        if password == "bad":
            raise imaplib.IMAP4.error("authentication failed")
        return "OK", [b"authenticated"]

    def select(self, mailbox, readonly=False):
        self.readonly = readonly
        type(self).last_readonly = readonly
        return "OK", [b"2"]

    def response(self, name):
        if name == "UIDNEXT":
            return name, [str(max(self.all_uids, default=0) + 1).encode()]
        return name, [str(self.uid_validity).encode()]

    def uid(self, command, *args):
        if command == "search":
            criterion = args[-1]
            type(self).search_criteria.append(criterion)
            values = self.all_uids if criterion == "ALL" else self.unseen_uids
            return "OK", [" ".join(str(value) for value in values).encode()]
        if command == "fetch":
            uid = int(args[0])
            type(self).last_fetch_query = args[1]
            return "OK", [(b"header", self.headers[uid])]
        raise AssertionError(command)

    def logout(self):
        return "BYE", []


class GmailImapTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeImap.all_uids = [1, 2]
        FakeImap.unseen_uids = [2]
        FakeImap.uid_validity = 55
        FakeImap.headers = {
            2: (
                b"From: Alice Example <alice@example.com>\r\n"
                b"Subject: Project update\r\n"
                b"Date: Mon, 13 Jul 2026 09:00:00 -0400\r\n"
                b"Message-ID: <message-2@example.com>\r\n\r\n"
            )
        }
        FakeImap.last_timeout = None
        FakeImap.search_criteria = []

    def test_app_password_whitespace_is_removed(self) -> None:
        self.assertEqual(normalize_app_password("abcd efgh ijkl mnop"), "abcdefghijklmnop")

    def test_first_poll_establishes_baseline_without_backlog_notice(self) -> None:
        messages, state = poll_unread_messages(
            "owner@gmail.com", "abcd efgh ijkl mnop", {}, client_factory=FakeImap
        )
        self.assertEqual(messages, [])
        self.assertTrue(state["initialized"])
        self.assertEqual(state["last_uid"], 2)
        self.assertNotIn("ALL", FakeImap.search_criteria)

    def test_default_socket_timeout_is_two_minutes(self) -> None:
        poll_unread_messages(
            "owner@gmail.com", "abcd efgh ijkl mnop", {}, client_factory=FakeImap
        )
        self.assertEqual(IMAP_TIMEOUT_SECONDS, 120)
        self.assertEqual(FakeImap.last_timeout, 120)

    def test_new_unread_message_is_returned_without_marking_it_read(self) -> None:
        FakeImap.all_uids = [1, 2]
        FakeImap.unseen_uids = [2]
        messages, state = poll_unread_messages(
            "owner@gmail.com",
            "abcdefghijklmnop",
            {"initialized": True, "uid_validity": 55, "last_uid": 1},
            client_factory=FakeImap,
        )
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["message_id"], "<message-2@example.com>")
        self.assertEqual(messages[0]["subject"], "Project update")
        self.assertIn("<message-2@example.com>", state["notified_message_ids"])
        self.assertTrue(FakeImap.last_readonly)
        self.assertIn("BODY.PEEK[HEADER.FIELDS", FakeImap.last_fetch_query)
        self.assertIn("UID 2:* UNSEEN", FakeImap.search_criteria)

    def test_large_unread_burst_is_drained_without_skipping_messages(self) -> None:
        FakeImap.all_uids = list(range(1, 17))
        FakeImap.unseen_uids = list(range(2, 17))
        FakeImap.headers = {
            uid: (
                f"From: Sender {uid} <sender{uid}@example.com>\r\n"
                f"Subject: Message {uid}\r\n"
                f"Message-ID: <message-{uid}@example.com>\r\n\r\n"
            ).encode()
            for uid in FakeImap.unseen_uids
        }
        state = {"initialized": True, "uid_validity": 55, "last_uid": 1}

        first, state = poll_unread_messages(
            "owner@gmail.com",
            "abcdefghijklmnop",
            state,
            max_results=10,
            client_factory=FakeImap,
        )
        second, state = poll_unread_messages(
            "owner@gmail.com",
            "abcdefghijklmnop",
            state,
            max_results=10,
            client_factory=FakeImap,
        )

        self.assertEqual([int(message["uid"]) for message in first], list(range(2, 12)))
        self.assertEqual([int(message["uid"]) for message in second], list(range(12, 17)))
        self.assertEqual(state["last_uid"], 16)
        self.assertNotIn("ALL", FakeImap.search_criteria)

    def test_uidvalidity_change_resets_baseline_safely(self) -> None:
        messages, state = poll_unread_messages(
            "owner@gmail.com",
            "abcdefghijklmnop",
            {"initialized": True, "uid_validity": 1, "last_uid": 1},
            client_factory=FakeImap,
        )
        self.assertEqual(messages, [])
        self.assertEqual(state["uid_validity"], 55)

    def test_authentication_errors_never_include_the_password(self) -> None:
        with self.assertRaises(GmailImapError) as raised:
            poll_unread_messages("owner@gmail.com", "bad", {}, client_factory=FakeImap)
        self.assertNotIn("bad", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
