import hashlib
import hmac
import json
import unittest
from datetime import UTC, datetime

from zusend_support.openwa import Connection, InvalidWebhook, Message, Receipt, parse_event

SECRET = b"fixture-only"


class OpenWAWebhookTest(unittest.TestCase):
    def accept(self, event: str, data: dict, **kwargs):
        body = json.dumps(
            {
                "event": event,
                "timestamp": "2026-11-19T10:00:00Z",
                "sessionId": "test-session",
                "idempotencyKey": "stable-event-id",
                "deliveryId": "dlv_fixture",
                "data": data,
            }
        ).encode()
        signature = "sha256=" + hmac.new(SECRET, body, hashlib.sha256).hexdigest()
        kwargs.setdefault("session_id", "test-session")
        kwargs.setdefault("secret", SECRET)
        return parse_event(body, signature, **kwargs)

    def test_received_message_keeps_exact_chat_identity(self) -> None:
        event = self.accept(
            "message.received",
            {
                "kind": "individual",
                "fromMe": False,
                "chatId": "contact@lid",
                "senderPhone": "0000000000",
                "id": "message-1",
                "type": "image",
                "timestamp": 1795082400,
                "body": "Screenshot",
                "media": {"mimetype": "image/png", "omitted": True},
            },
        )
        self.assertEqual(event.idempotency_key, "stable-event-id")
        self.assertEqual(event.dispatched_at, datetime(2026, 11, 19, 10, tzinfo=UTC))
        self.assertEqual(
            event.data,
            Message("contact@lid", "message-1", "inbound", 1795082400, "image", "Screenshot", True),
        )

    def test_receipt_and_connection_events(self) -> None:
        receipt = self.accept("message.ack", {"messageId": "message-1", "status": "read"})
        self.assertEqual(receipt.data, Receipt("message-1", "read"))
        connection = self.accept("session.status", {"sessionId": "test-session", "status": "ready"})
        self.assertEqual(connection.data, Connection("ready"))

    def test_group_and_unsubscribed_events_do_not_open_cases(self) -> None:
        self.assertIsNone(self.accept("message.received", {"kind": "group"}))
        self.assertIsNone(self.accept("session.qr", {"qr": "unused"}))

    def test_invalid_signature_or_session_is_rejected(self) -> None:
        with self.assertRaisesRegex(InvalidWebhook, "Invalid signature"):
            self.accept("message.ack", {}, secret=b"incorrect")
        with self.assertRaisesRegex(InvalidWebhook, "Unexpected OpenWA session"):
            self.accept("message.ack", {}, session_id="other-session")
        with self.assertRaisesRegex(InvalidWebhook, "size limit"):
            self.accept("message.ack", {}, max_body_bytes=10)

    def test_inconsistent_event_is_rejected(self) -> None:
        with self.assertRaisesRegex(InvalidWebhook, "Message direction"):
            self.accept("message.received", {"kind": "individual", "fromMe": True})
        with self.assertRaisesRegex(InvalidWebhook, "Invalid failure status"):
            self.accept("message.failed", {"messageId": "message-1", "status": "read"})
