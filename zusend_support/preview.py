"""Run a loopback-only Support preview backed by synthetic conversations."""

import argparse
import html
import secrets
import sqlite3
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlencode
from wsgiref.simple_server import WSGIServer, make_server

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
from zusend_support.openwa import Event, Message

SESSION = "preview"
AGENT = "preview-agent"
QUEUES = {"unassigned": "Unassigned", "mine": "Mine", "waiting": "Waiting", "resolved": "Resolved"}
STYLE = """
:root{font:16px system-ui;color:#242d39;background:#f2f5f7}
*{box-sizing:border-box}body{margin:0}.workspace{display:grid;grid-template-columns:270px 1fr;min-height:100vh}
aside{background:#17363c;color:#effbf8;padding:24px}aside a{color:inherit}
nav{display:grid;gap:4px;margin:24px 0}a{text-decoration:none}
nav a,.case{display:block;padding:9px;border-radius:7px}nav a[aria-current],.case[aria-current]{background:#29565a}
.case{overflow-wrap:anywhere}.case:hover,nav a:hover{background:#29565a}
main{padding:clamp(20px,5vw,64px);max-width:960px}h1,h2{margin-top:0}h2{overflow-wrap:anywhere}
article,form{background:white;border:1px solid #dbe4e8;border-radius:10px;padding:16px;margin:12px 0}
article p{white-space:pre-wrap;overflow-wrap:anywhere;margin-bottom:0}.internal{background:#fff6dd}
small{color:#47596b}label{display:block;font-weight:600}textarea{display:block;width:100%;min-height:96px;margin:8px 0;padding:8px;font:inherit}
button{border:0;border-radius:7px;padding:9px 14px;background:#14675d;color:white;font:inherit;cursor:pointer}
.notice{color:#47596b}@media(max-width:650px){.workspace{grid-template-columns:1fr}nav{grid-template-columns:repeat(2,1fr)}}
"""


def seed(db: sqlite3.Connection) -> None:
    for index, (contact, body) in enumerate(
        [
            ("Alex Morgan", "Can you confirm when the replacement will arrive?"),
            ("Casey Byrne", "I need a copy of my invoice, please."),
            ("Jamie Reid", "Could someone help me update my account details?"),
        ],
        start=1,
    ):
        accept_event(
            db,
            Event(
                SESSION,
                f"preview-{index}",
                datetime.now(UTC),
                Message(contact, str(index), "inbound", index, "text", body, False),
            ),
        )


def preview_app(path: Path, port: int):
    token = secrets.token_urlsafe(32)

    def app(environ, start_response):
        def respond(status, body, headers=()):
            data = body.encode()
            start_response(
                status,
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Content-Length", str(len(data))),
                    ("Cache-Control", "no-store"),
                    ("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"),
                    *headers,
                ],
            )
            return [data]

        if environ.get("HTTP_HOST") != f"127.0.0.1:{port}":
            return respond("403 Forbidden", "This preview is loopback only.")
        if environ.get("PATH_INFO") != "/":
            return respond("404 Not Found", "Page not found")
        method = environ.get("REQUEST_METHOD")
        if method not in {"GET", "POST"}:
            return respond("405 Method Not Allowed", "Unsupported method")
        args = parse_qs(environ.get("QUERY_STRING", ""))
        queue = args.get("queue", ["unassigned"])[0]
        if queue not in QUEUES:
            return respond("404 Not Found", "Queue not found")
        chat = args.get("chat", [""])[0]
        with closing(open_inbox(path)) as db:
            if method == "POST":
                if environ.get("HTTP_ORIGIN") != f"http://127.0.0.1:{port}":
                    return respond("403 Forbidden", "Invalid request origin")
                try:
                    size = int(environ.get("CONTENT_LENGTH", ""))
                except ValueError:
                    return respond("400 Bad Request", "Invalid form length")
                if not 0 < size <= 16_384:
                    return respond("400 Bad Request", "Invalid form length")
                form = parse_qs(environ["wsgi.input"].read(size).decode("utf-8", errors="replace"))
                if form.get("token", [""])[0] != token:
                    return respond("403 Forbidden", "Invalid form token")
                action = form.get("action", [""])[0]
                body = form.get("body", [""])[0]
                try:
                    if action == "claim" and claim_conversation(db, SESSION, chat, AGENT):
                        queue = "mine"
                    elif action == "note":
                        add_internal_note(db, SESSION, chat, AGENT, body)
                    elif action == "reply":
                        queue_reply(db, SESSION, chat, AGENT, form.get("intent", [""])[0], body)
                        queue = "waiting"
                    elif action == "resolve" and resolve_case(db, SESSION, chat, AGENT):
                        queue = "resolved"
                    else:
                        return respond("409 Conflict", "The case action is unavailable.")
                except (ValueError, ConversationConflict):
                    return respond("409 Conflict", "The case action is unavailable.")
                redirect = "/?" + urlencode({"queue": queue, "chat": chat})
                return respond("303 See Other", "", [("Location", redirect)])

            cases = list_cases(db, SESSION, AGENT, queue)
            case = next((entry for entry in cases if entry["chat_id"] == chat), None)
            if chat and case is None:
                return respond("404 Not Found", "Case not found in this queue.")
            return respond("200 OK", render(db, queue, cases, case, token))

    return app


def render(db, queue, cases, case, token) -> str:
    esc = html.escape
    links = "".join(
        f'<a href="?queue={key}"{" aria-current=page" if queue == key else ""}>{label}</a>'
        for key, label in QUEUES.items()
    )
    rows = "".join(
        f'<a class="case" href="?{esc(urlencode({"queue": queue, "chat": entry["chat_id"]}))}"'
        f'{" aria-current=page" if case and case["chat_id"] == entry["chat_id"] else ""}>'
        f'{esc(entry["chat_id"])}</a>'
        for entry in cases
    )
    panel = "<p>Select a case to read the conversation.</p>"
    if case:
        chat = case["chat_id"]
        panel = f"<h2>{esc(chat)}</h2><p class=notice>{esc(case['state'].title())}</p>"
        for message in db.execute(
            """SELECT direction, body FROM messages WHERE session_id = ? AND chat_id = ?
            ORDER BY occurred_at, message_id""",
            (SESSION, chat),
        ):
            label = "Customer" if message["direction"] == "inbound" else "Sent reply"
            panel += f"<article><small>{label}</small><p>{esc(message['body'])}</p></article>"
        if case["assigned_agent"] == AGENT:
            for note in db.execute(
                "SELECT body FROM internal_notes WHERE session_id = ? AND chat_id = ? ORDER BY id",
                (SESSION, chat),
            ):
                panel += f'<article class="internal"><small>Internal note</small><p>{esc(note["body"])}</p></article>'
            for reply in db.execute(
                "SELECT body FROM reply_outbox WHERE session_id = ? AND chat_id = ? ORDER BY rowid",
                (SESSION, chat),
            ):
                panel += f"<article><small>Queued preview reply</small><p>{esc(reply['body'])}</p></article>"
            panel += action_form(token, "note", "Add internal note", "Internal note")
            if case["state"] != "resolved":
                panel += action_form(token, "reply", "Queue customer reply", "Customer reply")
                panel += action_form(token, "resolve", "Resolve case")
        elif case["assigned_agent"] is None:
            panel += action_form(token, "claim", "Claim case")
    return (
        '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" '
        'content="width=device-width, initial-scale=1"><title>Zusend Support preview</title>'
        f"<style>{STYLE}</style><div class=workspace><aside><strong>Zusend Support</strong>"
        f"<nav aria-label=Queues>{links}</nav><h2>{QUEUES[queue]}</h2>{rows or '<p>No cases</p>'}"
        f"</aside><main><h1>Support inbox</h1><p class=notice>Preview with synthetic data. "
        f"Replies are saved locally.</p>{panel}</main></div></html>"
    )


def action_form(token: str, action: str, button: str, label: str = "") -> str:
    field = f'<label>{label}<textarea name="body" required></textarea></label>' if label else ""
    return (
        f'<form method="post"><input type="hidden" name="token" value="{token}">'
        f'<input type="hidden" name="intent" value="{secrets.token_urlsafe(16)}">'
        f'{field}<button name="action" value="{action}">{button}</button></form>'
    )


class PreviewServer(ThreadingMixIn, WSGIServer):
    # Browser preconnections can sit idle before they send any HTTP bytes.
    daemon_threads = True

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        return connection, address


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    opts = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="zusend-preview-") as folder:
        path = Path(folder) / "support.sqlite"
        with closing(open_inbox(path)) as db:
            seed(db)
        with make_server(
            "127.0.0.1", opts.port, preview_app(path, opts.port), server_class=PreviewServer
        ) as server:
            print(f"Zusend Support preview at http://127.0.0.1:{opts.port}", flush=True)
            server.serve_forever()


if __name__ == "__main__":
    main()
