import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from zusend_support.inbox import (
    ConversationConflict,
    accept_event,
    add_internal_note,
    claim_conversation,
    list_cases,
    open_inbox,
    queue_reply,
    resolve_case,
)
from zusend_support.openwa import Connection, Event, Message, Receipt


class InboxTest(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "support.sqlite"
        self.db = open_inbox(self.path)
        self.addCleanup(self.db.close)

    def event(self, key: str, data: Message | Receipt | Connection, seconds: int = 0) -> Event:
        return Event("test-session", key, datetime(2026, 11, 19, 10, 0, seconds, tzinfo=UTC), data)

    def test_retries_and_replayed_messages_do_not_duplicate_a_conversation(self) -> None:
        message = Message("customer@lid", "in-1", "inbound", 200, "text", "Hello", False)
        self.assertTrue(accept_event(self.db, self.event("event-1", message)))
        self.assertFalse(accept_event(self.db, self.event("event-1", message)))
        self.assertTrue(accept_event(self.db, self.event("replay-1", message)))
        older = Message("customer@lid", "in-0", "inbound", 100, "text", "Earlier", False)
        self.assertTrue(accept_event(self.db, self.event("event-0", older)))
        chats = self.db.execute("SELECT * FROM conversations").fetchall()
        self.assertEqual(len(chats), 1)
        self.assertEqual(chats[0]["last_inbound_at"], 200)
        self.assertEqual(self.db.execute("SELECT count(*) FROM messages").fetchone()[0], 2)

    def test_read_receipt_arriving_first_survives_retry_and_send_echo(self) -> None:
        accept_event(self.db, self.event("read", Receipt("out-1", "read")))
        accept_event(self.db, self.event("failed", Receipt("out-1", "failed")))
        sent = Message("customer@lid", "out-1", "outbound", 200, "text", "Reply", False)
        accept_event(self.db, self.event("echo", sent))
        self.assertEqual(self.db.execute("SELECT status FROM receipts").fetchone()[0], "read")
        self.assertEqual(self.db.execute("SELECT count(*) FROM conversations").fetchone()[0], 0)

    def test_older_connection_updates_do_not_overwrite_newer_state(self) -> None:
        accept_event(self.db, self.event("new", Connection("ready"), 1))
        accept_event(self.db, self.event("old", Connection("disconnected")))
        self.assertEqual(self.db.execute("SELECT status FROM connections").fetchone()[0], "ready")

    def test_event_is_not_acknowledged_in_storage_when_message_insert_fails(self) -> None:
        self.db.execute(
            """
            CREATE TRIGGER reject_message BEFORE INSERT ON messages BEGIN
                SELECT raise(ABORT, 'fixture write failure');
            END
            """
        )
        msg = Message("customer@lid", "in-1", "inbound", 200, "text", "Hello", False)
        with self.assertRaises(sqlite3.IntegrityError):
            accept_event(self.db, self.event("event-1", msg))
        self.assertEqual(self.db.execute("SELECT count(*) FROM accepted_events").fetchone()[0], 0)
        self.db.execute("DROP TRIGGER reject_message")
        self.assertTrue(accept_event(self.db, self.event("event-1", msg)))

    def test_agent_claim_note_reply_and_retry(self) -> None:
        session_id = "test-session"
        chat_id = "customer@lid"
        first = Message(chat_id, "in-1", "inbound", 100, "text", "Hello", False)
        accept_event(self.db, self.event("first", first))
        self.assertTrue(claim_conversation(self.db, session_id, chat_id, "agent-1"))
        self.assertFalse(claim_conversation(self.db, session_id, chat_id, "agent-2"))
        with self.assertRaises(ConversationConflict):
            add_internal_note(self.db, session_id, chat_id, "agent-2", "Unauthorised")
        with self.assertRaises(ConversationConflict):
            queue_reply(self.db, session_id, chat_id, "agent-2", "intent-1", "Unauthorised")

        add_internal_note(self.db, session_id, chat_id, "agent-1", "Internal only")
        self.assertTrue(queue_reply(self.db, session_id, chat_id, "agent-1", "intent-1", "Reply"))
        self.assertEqual(self.db.execute("SELECT state FROM conversations").fetchone()[0], "waiting")
        self.assertEqual(self.db.execute("SELECT body FROM reply_outbox").fetchone()[0], "Reply")
        self.assertEqual(self.db.execute("SELECT body FROM internal_notes").fetchone()[0], "Internal only")

        newer = Message(chat_id, "in-2", "inbound", 200, "text", "Following up", False)
        accept_event(self.db, self.event("second", newer))
        self.assertFalse(queue_reply(self.db, session_id, chat_id, "agent-1", "intent-1", "Reply"))
        self.assertEqual(self.db.execute("SELECT state FROM conversations").fetchone()[0], "open")
        self.assertEqual(self.db.execute("SELECT count(*) FROM reply_outbox").fetchone()[0], 1)

    def test_queue_filters_and_resolution_are_agent_and_session_scoped(self) -> None:
        message = Message("a@lid", "in-1", "inbound", 100, "text", "Hello", False)
        accept_event(self.db, self.event("event-a", message))
        self.assertEqual(len(list_cases(self.db, "test-session", "alice", "unassigned")), 1)
        self.assertEqual(list_cases(self.db, "another-session", "alice", "unassigned"), [])
        self.assertTrue(claim_conversation(self.db, "test-session", "a@lid", "alice"))
        self.assertEqual(list_cases(self.db, "test-session", "bob", "mine"), [])
        self.assertEqual(list_cases(self.db, "test-session", "alice", "mine")[0]["chat_id"], "a@lid")
        self.assertFalse(resolve_case(self.db, "test-session", "a@lid", "bob"))
        self.assertTrue(resolve_case(self.db, "test-session", "a@lid", "alice"))
        self.assertEqual(list_cases(self.db, "test-session", "alice", "mine"), [])
        self.assertEqual(len(list_cases(self.db, "test-session", "alice", "resolved")), 1)

        newer = Message("a@lid", "in-2", "inbound", 200, "text", "Following up", False)
        accept_event(self.db, self.event("event-b", newer))
        self.assertEqual(len(list_cases(self.db, "test-session", "alice", "mine")), 1)
        self.assertEqual(list_cases(self.db, "test-session", "alice", "resolved"), [])
