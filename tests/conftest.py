"""Local protocol fixture: tests cross real HTTP, subprocess, and SQLite boundaries."""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from evoctl.config import ConfigStore, Profile
from evoctl.service import Service


@dataclass
class ProtocolState:
    """Deterministic upstream data and observed requests for protocol assertions."""

    requests: list[tuple[str, str, Any]] = field(default_factory=list)
    contacts: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {"remoteJid": "15550000001@s.whatsapp.net", "pushName": "Alex Novák"},
            {"remoteJid": "15550000002@s.whatsapp.net", "pushName": "Sam Example"},
            {"remoteJid": "120363000000000000@g.us", "pushName": "Example group"},
        ]
    )
    messages: list[dict[str, Any]] = field(default_factory=list)
    updates: list[dict[str, Any]] = field(default_factory=list)
    send_delay: float = 0
    send_status: int = 201
    whatsapp: str = "open"


def handler_for(state: ProtocolState) -> type[BaseHTTPRequestHandler]:
    """Build a real HTTP fixture implementing only the tested Evolution protocol."""

    class Handler(BaseHTTPRequestHandler):
        """Accept actual urllib requests and record their method, route, and JSON body."""

        def log_message(self, _format: str, *args: Any) -> None:
            """Keep fixture traffic out of test output."""

        def reply(self, body: Any, status: int = 200, headers: dict[str, str] | None = None) -> None:
            """Return an HTTP response with the same JSON framing as Evolution."""
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            with suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(payload)

        def do_GET(self) -> None:
            """Serve root health, instance state, and a redirect leak regression route."""
            state.requests.append(("GET", self.path, None))
            if self.path == "/":
                self.reply({"version": "2.3.7"})
            elif self.path == "/instance/connectionState/Default":
                if self.headers["apikey"] != "test-only":
                    self.reply({}, 401)
                else:
                    self.reply({"instance": {"state": state.whatsapp}})
            elif self.path == "/instance/fetchInstances":
                self.reply([{"name": "Default", "token": "private", "hash": "private", "status": "open"}])
            elif self.path == "/instance/connect/Default":
                self.reply(
                    {
                        "base64": "data:image/png;base64,"
                        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1cAAAAASUVORK5CYII="
                    }
                )
            elif self.path == "/redirect":
                self.reply({}, 302, {"Location": "/must-not-receive-credentials"})
            elif self.path == "/large":
                self.reply({"text": "x" * 5000})
            elif self.path == "/metrics":
                self.reply(
                    {"received": self.headers["Authorization"]},
                    200 if self.headers["Authorization"] == "test-only" else 401,
                )
            else:
                self.reply({}, 404)

        def do_POST(self) -> None:
            """Implement bounded history, contact lookup, receipts, and message acceptance."""
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state.requests.append(("POST", self.path, body))
            if self.headers["apikey"] != "test-only":
                self.reply({}, 401)
                return
            if self.path == "/chat/findContacts/Default":
                offset, page = body["offset"], body["page"]
                self.reply(state.contacts[(page - 1) * offset : page * offset])
            elif self.path == "/chat/findChats/Default":
                self.reply([{"remoteJid": "15550000001@s.whatsapp.net", "pushName": "Alex", "unreadMessages": 2}])
            elif self.path == "/chat/findMessages/Default":
                records = state.messages
                filters = body["where"]["key"]
                for key in ("id", "remoteJid"):
                    if key in filters:
                        records = [message for message in records if message["key"][key] == filters[key]]
                offset, page = body["offset"], body["page"]
                self.reply(
                    {
                        "messages": {
                            "records": records[(page - 1) * offset : page * offset],
                            "total": len(records),
                            "pages": (len(records) + offset - 1) // offset,
                            "currentPage": page,
                        }
                    }
                )
            elif self.path == "/chat/findStatusMessage/Default":
                self.reply([item for item in state.updates if item["keyId"] == body["where"]["id"]])
            elif self.path == "/message/sendText/Default":
                if state.send_delay:
                    time.sleep(state.send_delay)
                if state.send_status != 201:
                    self.reply({}, state.send_status)
                    return
                identifier = f"TEST_MESSAGE_{len(state.messages)}"
                message = {
                    "key": {"id": identifier, "remoteJid": body["number"], "fromMe": True},
                    "message": {"conversation": body["text"]},
                    "messageType": "conversation",
                    "messageTimestamp": 1_780_000_000,
                    "MessageUpdate": [],
                    "status": "PENDING",
                }
                state.messages.append(message)
                self.reply(message, 201)
            else:
                self.reply({}, 404)

    return Handler


@pytest.fixture
def protocol(tmp_path: Path):
    """Run a loopback HTTP server and a private profile for each integration test."""
    state = ProtocolState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    store = ConfigStore(tmp_path / "config")
    store.add("test", Profile(api_url=f"http://127.0.0.1:{server.server_port}", key_env="EVOCTL_TEST_KEY"))
    original = os.environ.get("EVOCTL_TEST_KEY")
    os.environ["EVOCTL_TEST_KEY"] = "test-only"
    service = Service(store, tmp_path / "state")
    try:
        yield service, state, store
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if original is None:
            os.environ.pop("EVOCTL_TEST_KEY", None)
        else:
            os.environ["EVOCTL_TEST_KEY"] = original
