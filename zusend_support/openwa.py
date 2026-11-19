"""Verify and normalise the OpenWA webhook events used by the support service."""

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime


class InvalidWebhook(ValueError):
    """A request could not be verified or did not match the supported schema."""


@dataclass(frozen=True)
class Message:
    chat_id: str
    message_id: str
    direction: str
    timestamp: int
    kind: str
    body: str
    has_media: bool


@dataclass(frozen=True)
class Receipt:
    message_id: str
    status: str


@dataclass(frozen=True)
class Connection:
    status: str


@dataclass(frozen=True)
class Event:
    session_id: str
    idempotency_key: str
    dispatched_at: datetime
    data: Message | Receipt | Connection


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise InvalidWebhook("Expected an object")
    return value


def _field(data: dict[str, object], name: str) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value:
        raise InvalidWebhook(f"Invalid {name}")
    return value


def parse_event(
    raw_body: bytes,
    signature: str | None,
    *,
    secret: bytes,
    session_id: str,
    max_body_bytes: int = 1024 * 1024,
) -> Event | None:
    """Validate the raw-body signature and the configured session.

    Return None for events outside the supported 1:1 inbox contract. Persist
    accepted events transactionally using (session_id, idempotency_key).
    """
    if not secret or not signature or not signature.isascii():
        raise InvalidWebhook("Invalid signature")
    if len(raw_body) > max_body_bytes:
        raise InvalidWebhook("Webhook body exceeds the configured size limit")
    digest = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, f"sha256={digest}"):
        raise InvalidWebhook("Invalid signature")

    try:
        envelope = _object(json.loads(raw_body))
        dispatched_at = datetime.fromisoformat(_field(envelope, "timestamp"))
    except (ValueError, RecursionError) as exc:
        raise InvalidWebhook("Invalid webhook envelope") from exc
    if dispatched_at.tzinfo is None:
        raise InvalidWebhook("Invalid dispatch timestamp")
    if not session_id or _field(envelope, "sessionId") != session_id:
        raise InvalidWebhook("Unexpected OpenWA session")

    key = _field(envelope, "idempotencyKey")
    event_name = _field(envelope, "event")
    data = _object(envelope.get("data"))

    payload: Message | Receipt | Connection
    if event_name in {"message.received", "message.sent"}:
        if data.get("kind") != "individual":
            return None
        from_me = data.get("fromMe")
        if not isinstance(from_me, bool) or from_me != (event_name == "message.sent"):
            raise InvalidWebhook("Message direction does not match the event")
        body = data.get("body")
        timestamp = data.get("timestamp")
        if not isinstance(body, str) or type(timestamp) is not int or timestamp < 0:
            raise InvalidWebhook("Invalid message content or timestamp")
        payload = Message(
            chat_id=_field(data, "chatId"),
            message_id=_field(data, "id"),
            direction="outbound" if from_me else "inbound",
            timestamp=timestamp,
            kind=_field(data, "type"),
            body=body,
            has_media=isinstance(data.get("media"), dict),
        )
    elif event_name in {"message.ack", "message.failed"}:
        status = _field(data, "status")
        if status not in {"pending", "sent", "delivered", "read", "failed"}:
            raise InvalidWebhook("Invalid delivery status")
        if event_name == "message.failed" and status != "failed":
            raise InvalidWebhook("Invalid failure status")
        payload = Receipt(message_id=_field(data, "messageId"), status=status)
    elif event_name == "session.status":
        if _field(data, "sessionId") != session_id:
            raise InvalidWebhook("Unexpected status session")
        payload = Connection(status=_field(data, "status"))
    else:
        return None

    return Event(session_id, key, dispatched_at.astimezone(UTC), payload)
