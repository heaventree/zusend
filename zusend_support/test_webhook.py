import hashlib
import hmac
import io
import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from zusend_support.inbox import open_inbox
from zusend_support.webhook import build_webhook_app


class WebhookTest(unittest.TestCase):
    def test_signed_delivery_and_retry_through_wsgi(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "test.sqlite"
            secret = b"fixture-only"
            app = build_webhook_app(db_path, secret=secret, session_id="test-session")
            body = json.dumps(
                {
                    "event": "message.received",
                    "timestamp": "2026-11-19T10:00:00Z",
                    "sessionId": "test-session",
                    "idempotencyKey": "fixture-event",
                    "data": {
                        "kind": "individual",
                        "chatId": "customer@lid",
                        "id": "message-1",
                        "fromMe": False,
                        "timestamp": 1795082400,
                        "type": "text",
                        "body": "Hello",
                    },
                }
            ).encode()
            signature = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()

            def send(header: str) -> tuple[str, str]:
                statuses = []
                response = app(
                    {
                        "PATH_INFO": "/webhooks/openwa",
                        "REQUEST_METHOD": "POST",
                        "CONTENT_LENGTH": str(len(body)),
                        "HTTP_X_OPENWA_SIGNATURE": header,
                        "wsgi.input": io.BytesIO(body),
                    },
                    lambda status, headers: statuses.append(status),
                )
                return statuses[0], json.loads(b"".join(response))["result"]

            self.assertEqual(send("sha256=forged"), ("403 Forbidden", "invalid_webhook"))
            self.assertEqual(send(signature), ("200 OK", "accepted"))
            self.assertEqual(send(signature), ("200 OK", "duplicate"))
            with closing(open_inbox(db_path)) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM messages").fetchone()[0], 1)
