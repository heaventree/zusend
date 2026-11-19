"""Transactional storage for the single-instance support inbox prototype."""

import sqlite3
from pathlib import Path

from zusend_support.openwa import Connection, Event, Message, Receipt

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accepted_events (
    session_id TEXT NOT NULL,
    event_key TEXT NOT NULL,
    PRIMARY KEY (session_id, event_key)
);
CREATE TABLE IF NOT EXISTS conversations (
    session_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    last_inbound_at INTEGER NOT NULL,
    assigned_agent TEXT,
    state TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'waiting', 'resolved')),
    PRIMARY KEY (session_id, chat_id)
);
CREATE TABLE IF NOT EXISTS internal_notes (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    body TEXT NOT NULL,
    FOREIGN KEY (session_id, chat_id) REFERENCES conversations (session_id, chat_id)
);
CREATE TABLE IF NOT EXISTS reply_outbox (
    session_id TEXT NOT NULL,
    intent_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    PRIMARY KEY (session_id, intent_id),
    FOREIGN KEY (session_id, chat_id) REFERENCES conversations (session_id, chat_id)
);
CREATE TABLE IF NOT EXISTS messages (
    session_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    occurred_at INTEGER NOT NULL,
    kind TEXT NOT NULL,
    body TEXT NOT NULL,
    has_media INTEGER NOT NULL,
    PRIMARY KEY (session_id, message_id)
);
CREATE INDEX IF NOT EXISTS messages_by_chat ON messages (session_id, chat_id, occurred_at);
CREATE TABLE IF NOT EXISTS receipts (
    session_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    status TEXT NOT NULL,
    priority INTEGER NOT NULL,
    PRIMARY KEY (session_id, message_id)
);
CREATE TABLE IF NOT EXISTS connections (
    session_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    dispatched_at TEXT NOT NULL
);
"""

# A confirmed delivery wins over a failure received out of order.
_RECEIPT_PRIORITY = {"pending": 0, "sent": 1, "failed": 2, "delivered": 3, "read": 4}


def open_inbox(path: Path) -> sqlite3.Connection:
    """Open a development inbox. Callers own the connection lifetime."""
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys = ON")
        db.executescript(_SCHEMA)
    except sqlite3.Error:
        db.close()
        raise
    return db


def accept_event(db: sqlite3.Connection, event: Event) -> bool:
    """Commit the event and its effects together. Return False for a retry."""
    with db:
        inserted = db.execute(
            """
            INSERT INTO accepted_events VALUES (?, ?)
            ON CONFLICT DO NOTHING
            """,
            (event.session_id, event.idempotency_key),
        )
        if inserted.rowcount == 0:
            return False

        data = event.data
        if isinstance(data, Message):
            _accept_message(db, event.session_id, data)
        elif isinstance(data, Receipt):
            db.execute(
                """
                INSERT INTO receipts VALUES (?, ?, ?, ?)
                ON CONFLICT (session_id, message_id) DO UPDATE SET
                    status = excluded.status,
                    priority = excluded.priority
                WHERE excluded.priority > receipts.priority
                """,
                (event.session_id, data.message_id, data.status, _RECEIPT_PRIORITY[data.status]),
            )
        elif isinstance(data, Connection):
            db.execute(
                """
                INSERT INTO connections VALUES (?, ?, ?)
                ON CONFLICT (session_id) DO UPDATE SET
                    status = excluded.status,
                    dispatched_at = excluded.dispatched_at
                WHERE excluded.dispatched_at > connections.dispatched_at
                """,
                (
                    event.session_id,
                    data.status,
                    event.dispatched_at.isoformat(timespec="microseconds"),
                ),
            )
    return True


def _accept_message(db: sqlite3.Connection, session_id: str, message: Message) -> None:
    inserted = db.execute(
        """
        INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT DO NOTHING
        """,
        (
            session_id,
            message.message_id,
            message.chat_id,
            message.direction,
            message.timestamp,
            message.kind,
            message.body,
            message.has_media,
        ),
    )
    if inserted.rowcount == 1 and message.direction == "inbound":
        db.execute(
            """
            INSERT INTO conversations (session_id, chat_id, last_inbound_at)
            VALUES (?, ?, ?)
            ON CONFLICT (session_id, chat_id) DO UPDATE SET
                last_inbound_at = excluded.last_inbound_at,
                state = 'open'
            WHERE excluded.last_inbound_at > conversations.last_inbound_at
            """,
            (session_id, message.chat_id, message.timestamp),
        )

class ConversationConflict(ValueError):
    """The case was unavailable, changed ownership, or no longer accepts the action."""


def claim_conversation(db: sqlite3.Connection, session_id: str, chat_id: str, agent_id: str) -> bool:
    """Claim an unassigned case for an agent authorised by the caller."""
    if not agent_id:
        raise ValueError("An agent is required")
    with db:
        claimed = db.execute(
            """
            UPDATE conversations SET assigned_agent = ?
            WHERE session_id = ? AND chat_id = ? AND assigned_agent IS NULL
              AND state != 'resolved'
            """,
            (agent_id, session_id, chat_id),
        )
        return claimed.rowcount == 1


def add_internal_note(
    db: sqlite3.Connection, session_id: str, chat_id: str, agent_id: str, body: str
) -> None:
    """Store a private note for the current assignee."""
    if not body.strip():
        raise ValueError("A note cannot be empty")
    with db:
        inserted = db.execute(
            """
            INSERT INTO internal_notes (session_id, chat_id, agent_id, body)
            SELECT session_id, chat_id, assigned_agent, ?
            FROM conversations
            WHERE session_id = ? AND chat_id = ? AND assigned_agent = ?
            """,
            (body, session_id, chat_id, agent_id),
        )
        if inserted.rowcount != 1:
            raise ConversationConflict("The case is assigned to another agent")


def queue_reply(
    db: sqlite3.Connection,
    session_id: str,
    chat_id: str,
    agent_id: str,
    intent_id: str,
    body: str,
) -> bool:
    """Queue a customer reply once, with an atomic case state update."""
    if not intent_id or not body.strip():
        raise ValueError("The reply must have an intent ID and message body")
    with db:
        queued = db.execute(
            """
            INSERT INTO reply_outbox (session_id, intent_id, chat_id, agent_id, body)
            SELECT session_id, ?, chat_id, assigned_agent, ?
            FROM conversations
            WHERE session_id = ? AND chat_id = ? AND assigned_agent = ?
              AND state != 'resolved'
            ON CONFLICT DO NOTHING
            """,
            (intent_id, body, session_id, chat_id, agent_id),
        )
        if queued.rowcount == 1:
            db.execute(
                """
                UPDATE conversations SET state = 'waiting'
                WHERE session_id = ? AND chat_id = ?
                """,
                (session_id, chat_id),
            )
            return True
        existing = db.execute(
            """
            SELECT chat_id, agent_id, body FROM reply_outbox
            WHERE session_id = ? AND intent_id = ?
            """,
            (session_id, intent_id),
        ).fetchone()
        if existing is not None and tuple(existing) == (chat_id, agent_id, body):
            return False
        raise ConversationConflict("The case cannot accept this reply intent")


def list_cases(
    db: sqlite3.Connection,
    session_id: str,
    agent_id: str,
    queue: str,
    *,
    limit: int = 50,
) -> list[sqlite3.Row]:
    """List cases in an inbox the authenticated agent may access."""
    if not agent_id or not 1 <= limit <= 100:
        raise ValueError("An agent and a valid page size are required")
    filters = {
        "unassigned": "assigned_agent IS NULL AND state = 'open'",
        "mine": "assigned_agent = ? AND state = 'open'",
        "waiting": "assigned_agent = ? AND state = 'waiting'",
        "resolved": "assigned_agent = ? AND state = 'resolved'",
    }
    if queue not in filters:
        raise ValueError("Unknown support queue")
    parameters = (session_id, limit) if queue == "unassigned" else (session_id, agent_id, limit)
    return db.execute(
        f"""
        SELECT chat_id, last_inbound_at, assigned_agent, state
        FROM conversations
        WHERE session_id = ? AND {filters[queue]}
        ORDER BY last_inbound_at DESC, chat_id
        LIMIT ?
        """,
        parameters,
    ).fetchall()


def resolve_case(db: sqlite3.Connection, session_id: str, chat_id: str, agent_id: str) -> bool:
    """Resolve a case atomically if it still belongs to this agent."""
    with db:
        result = db.execute(
            """
            UPDATE conversations SET state = 'resolved'
            WHERE session_id = ? AND chat_id = ? AND assigned_agent = ?
              AND state IN ('open', 'waiting')
            """,
            (session_id, chat_id, agent_id),
        )
        return result.rowcount == 1
