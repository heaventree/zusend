"""WSGI entry point for local and staging OpenWA webhook ingestion."""

import json
import logging
import sqlite3
from collections.abc import Callable, Iterable
from contextlib import closing
from pathlib import Path
from typing import Any

from zusend_support.inbox import accept_event, open_inbox
from zusend_support.openwa import InvalidWebhook, parse_event

logger = logging.getLogger(__name__)
StartResponse = Callable[[str, list[tuple[str, str]]], Any]


def build_webhook_app(
    db_path: Path, *, secret: bytes, session_id: str, max_body_bytes: int = 1024 * 1024
) -> Callable[[dict[str, Any], StartResponse], Iterable[bytes]]:
    if not secret or not session_id or max_body_bytes < 1:
        raise ValueError("A webhook secret, session and positive body limit are required")
    with closing(open_inbox(db_path)):
        pass

    def respond(start_response: StartResponse, status: str, result: str) -> list[bytes]:
        body = json.dumps({"result": result}).encode()
        start_response(
            status,
            [("Content-Type", "application/json"), ("Content-Length", str(len(body)))],
        )
        return [body]

    def app(environ: dict[str, Any], start_response: StartResponse) -> Iterable[bytes]:
        if environ.get("PATH_INFO") != "/webhooks/openwa":
            return respond(start_response, "404 Not Found", "not_found")
        if environ.get("REQUEST_METHOD") != "POST":
            return respond(start_response, "405 Method Not Allowed", "method_not_allowed")
        try:
            size = int(environ.get("CONTENT_LENGTH", ""))
        except ValueError:
            return respond(start_response, "411 Length Required", "length_required")
        if size > max_body_bytes:
            return respond(start_response, "413 Content Too Large", "body_too_large")
        if size < 1:
            return respond(start_response, "400 Bad Request", "empty_body")
        body = environ["wsgi.input"].read(size)
        if len(body) != size:
            return respond(start_response, "400 Bad Request", "incomplete_body")
        try:
            event = parse_event(
                body,
                environ.get("HTTP_X_OPENWA_SIGNATURE"),
                secret=secret,
                session_id=session_id,
                max_body_bytes=max_body_bytes,
            )
        except InvalidWebhook:
            # Never expose a signing or validation diagnostic to the requester.
            return respond(start_response, "403 Forbidden", "invalid_webhook")
        if event is None:
            return respond(start_response, "200 OK", "ignored")
        try:
            with closing(sqlite3.connect(db_path)) as db:
                inserted = accept_event(db, event)
        except sqlite3.Error:
            logger.exception("Could not persist OpenWA webhook event")
            return respond(start_response, "503 Service Unavailable", "retry")
        return respond(start_response, "200 OK", "accepted" if inserted else "duplicate")

    return app
