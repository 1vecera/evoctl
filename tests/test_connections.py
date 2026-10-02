"""Independent deployments remain selectable through real CLI and MCP sessions."""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from conftest import ProtocolState, handler_for
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters
from test_cli import command

from evoctl.config import ConfigStore, Profile, ReadFallbacks
from evoctl.service import Service
from evoctl.transport import Transport


@pytest.fixture
def two_connections(protocol):
    """Add a separately addressable server with independent readiness and message storage."""
    service, _, store = protocol
    second = ProtocolState(whatsapp="close")
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(second))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    store.add("second", Profile(api_url=f"http://127.0.0.1:{server.server_port}", key_env="EVOCTL_TEST_KEY"))
    try:
        yield service, second, store
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_all_connections_report_failures_without_switching(protocol, two_connections):
    """An unpaired or failing backend never hides a healthy peer or changes the default."""
    _, second, store = two_connections
    result = command(protocol, "status", "--all")
    assert result.returncode == 1
    report = json.loads(result.stdout)["data"]
    assert report["default"] == "test" and not report["ready"]
    assert [(item["profile"], item["ready"]) for item in report["profiles"]] == [("test", True), ("second", False)]
    assert report["profiles"][1]["whatsapp"] == "close"

    second.source_failures["/"] = 503
    doctor = command(protocol, "doctor", "--all")
    assert doctor.returncode == 1
    reports = json.loads(doctor.stdout)["data"]["profiles"]
    assert reports[0]["ready"] and reports[1]["api_error"]["code"] == "API_REJECTED"
    assert store.load().default == "test"
    assert not protocol[1].messages and not second.messages


def test_all_connections_watch_and_empty_configuration(protocol, two_connections, tmp_path):
    """Aggregate readiness and watch exit codes cover both connections, including no configured targets."""
    two_connections[1].whatsapp = "open"
    watch = command(protocol, "status", "--all", "--watch", "--count", "2", "--interval", "1")
    assert watch.returncode == 0
    reports = [json.loads(line)["data"] for line in watch.stdout.splitlines()]
    assert len(reports) == 2 and all(report["ready"] and len(report["profiles"]) == 2 for report in reports)
    conflict = command(protocol, "--profile", "second", "status", "--all")
    assert conflict.returncode == 2 and json.loads(conflict.stdout)["error"]["code"] == "INVALID_INPUT"
    empty = Service(ConfigStore(tmp_path / "empty"), tmp_path / "state").invoke("remotes_status", {})
    assert empty.ok and empty.data == {"default": "", "profiles": [], "ready": False}


async def test_cli_mcp_switching_and_explicit_profile_override(protocol, two_connections):
    """A running MCP process observes CLI default changes; explicit profiles still win."""
    service, second, store = two_connections
    parameters = StdioServerParameters(
        command=str(Path(sys.executable).with_name("evoctl")),
        args=["mcp", "serve", "--mode", "write"],
        env={
            "EVOCTL_CONFIG_DIR": str(store.directory),
            "EVOCTL_STATE_DIR": str(service.state),
            "EVOCTL_TEST_KEY": os.environ["EVOCTL_TEST_KEY"],
        },
    )
    async with Client(parameters, read_timeout_seconds=15) as client:
        discovery = await client.call_tool("evoctl_discover", {"operation": "remote_use"})
        assert discovery.structured_content["data"]["tool"] == "evoctl_write"
        aggregate = await client.call_tool("evoctl_read", {"action": "remotes_status"})
        cli = command(protocol, "status", "--all")
        assert not aggregate.is_error and aggregate.structured_content == json.loads(cli.stdout)

        selected = command(protocol, "remote", "use", "second")
        assert selected.returncode == 0 and json.loads(selected.stdout)["data"] == {"default": "second"}
        default = await client.call_tool("evoctl_read", {"action": "status"})
        assert default.structured_content["data"]["profile"] == "second"
        assert not default.structured_content["data"]["ready"]
        explicit = await client.call_tool("evoctl_read", {"action": "status", "arguments": {"profile": "test"}})
        assert explicit.structured_content["data"]["ready"]
        assert command(protocol, "--profile", "test", "status").returncode == 0

        switched = await client.call_tool("evoctl_write", {"action": "remote_use", "arguments": {"name": "test"}})
        assert not switched.is_error and switched.structured_content["data"] == {"default": "test"}
        assert json.loads(command(protocol, "status").stdout)["data"]["profile"] == "test"
        missing = await client.call_tool("evoctl_write", {"action": "remote_use", "arguments": {"name": "missing"}})
        assert missing.is_error and missing.structured_content["error"]["code"] == "PROFILE_NOT_FOUND"
        assert store.load().default == "test"
        assert not second.messages and not protocol[1].messages

    pinned = Service(store, service.state, mode="read", default_profile="second")
    assert pinned.invoke("status", {}).data["profile"] == "second"
    assert pinned.invoke("remotes_status", {}).data["default"] == "second"
    denied = pinned.invoke("remote_use", {"name": "second"})
    assert not denied.ok and denied.error["code"] == "CAPABILITY_DENIED"
    assert store.load().default == "test"


def test_unready_explicit_send_does_not_fall_back(protocol, two_connections):
    """A rejected send on one connection never reaches the other connection."""
    service, second, store = two_connections
    second.send_status = 503
    store.use("second")
    store.set_read_fallbacks(ReadFallbacks(profiles=["test"]))
    result = service.invoke("message_send", {"to": "15550000001", "text": "Fixture only", "request_id": "no-fallback"})
    assert not result.ok
    assert any(path == "/message/sendText/Default" for _, path, _ in second.requests)
    assert not protocol[1].requests and not protocol[1].messages


def test_fallback_configuration_is_explicit_and_backwards_compatible(protocol, two_connections):
    """Invalid policy edits leave the saved routing intact, and older clients can load profiles."""
    _, _, store = two_connections
    assert command(protocol, "remote", "fallback", "second").returncode == 0
    assert store.read_fallbacks().profiles == ["second"]
    assert "read_fallbacks" not in json.loads(store.path.read_text())
    assert (store.directory / "read-fallbacks.json").stat().st_mode & 0o777 == 0o600
    for arguments in [(), ("missing",), ("second", "second"), ("second", "--clear")]:
        assert command(protocol, "remote", "fallback", *arguments).returncode != 0
        assert store.read_fallbacks().profiles == ["second"]
    listing = json.loads(command(protocol, "remote", "list").stdout)["data"]
    assert listing["read_fallbacks"] == ["second"]
    assert command(protocol, "remote", "fallback", "--clear").returncode == 0
    assert not store.read_fallbacks().profiles


def test_unpaired_default_falls_back_and_recovers_without_switching(protocol, two_connections):
    """Every new read prefers the saved default again; empty healthy results never cause fallback."""
    service, second, store = two_connections
    primary = protocol[1]
    primary.whatsapp = "close"
    second.whatsapp = "open"
    second.chats[0]["pushName"] = "Second backend"
    store.set_read_fallbacks(ReadFallbacks(profiles=["second"]))
    response = json.loads(command(protocol, "chats", "list").stdout)
    assert response["ok"] and response["data"]["chats"][0]["name"] == "Second backend"
    connection = response["data"]["connection"]
    assert connection["profile"] == "second" and connection["preferred"] == "test" and connection["fallback_used"]
    assert connection["failures"][0]["profile"] == "test" and store.load().default == "test"

    second.requests.clear()
    primary.whatsapp = "open"
    recovered = service.invoke("chats_list", {})
    assert recovered.ok and recovered.data["connection"]["profile"] == "test"
    assert not recovered.data["connection"]["fallback_used"] and not second.requests
    primary.chats.clear()
    empty = service.invoke("chats_list", {})
    assert empty.ok and not empty.data["chats"] and not second.requests


def test_unreachable_default_and_exhausted_fallbacks(protocol, two_connections):
    """A real refused TCP connection falls back, while two unavailable targets return both diagnostics."""
    service, second, store = two_connections
    second.whatsapp = "open"
    store.set_read_fallbacks(ReadFallbacks(profiles=["second"]))
    _, settings = store.resolve()
    with socket.socket() as unavailable:
        unavailable.bind(("127.0.0.1", 0))
        store.add(
            "test",
            settings.model_copy(update={"api_url": f"http://127.0.0.1:{unavailable.getsockname()[1]}"}),
            replace=True,
        )
        result = service.invoke("chats_list", {})
        assert result.ok and result.data["connection"]["profile"] == "second"
        assert result.data["connection"]["failures"][0]["error"]["code"] == "API_UNREACHABLE"
        second.whatsapp = "close"
        failed = service.invoke("chats_list", {})
        assert not failed.ok and failed.error["code"] == "NO_READY_CONNECTION"
        assert [item["profile"] for item in failed.data["connection"]["failures"]] == ["test", "second"]
        assert failed.data["connection"]["profile"] == ""


def test_lookup_failure_falls_back_but_pinned_reads_do_not(protocol, two_connections):
    """A 503 after a healthy probe retries the whole read only when profile selection is automatic."""
    service, second, store = two_connections
    second.whatsapp = "open"
    store.set_read_fallbacks(ReadFallbacks(profiles=["second"]))
    protocol[1].source_failures["/chat/findChats/Default"] = 503
    result = service.invoke("chats_list", {})
    assert result.ok and result.data["connection"]["profile"] == "second"
    assert result.data["connection"]["failures"][0]["error"]["retryable"]
    second.requests.clear()
    explicit = command(protocol, "--profile", "test", "chats", "list")
    assert explicit.returncode != 0 and not second.requests
    pinned = Service(store, service.state, default_profile="test")
    assert not pinned.invoke("chats_list", {}).ok and not second.requests
    assert service.invoke("status", {}).data["profile"] == "test"
    direct = service.invoke("api_read", {"operation": "chat.find_chats", "body": {"where": {}, "take": 1, "skip": 0}})
    assert not direct.ok and not second.requests


@pytest.mark.parametrize("http_status", [400, 401, 403, 429])
def test_lookup_validation_auth_and_rate_limits_do_not_fail_over(protocol, two_connections, http_status):
    """Caller/authentication errors and rate limits stay visible on the selected source."""
    service, second, store = two_connections
    second.whatsapp = "open"
    store.set_read_fallbacks(ReadFallbacks(profiles=["second"]))
    protocol[1].source_failures["/chat/findChats/Default"] = http_status
    result = service.invoke("chats_list", {})
    assert not result.ok and result.data["connection"]["profile"] == "test"
    assert not second.requests


def test_search_source_failures_preserve_useful_partial_results(protocol, two_connections):
    """Unavailable sources can fail over, but matching partial results stay bound to their original backend."""
    service, second, store = two_connections
    second.whatsapp = "open"
    store.set_read_fallbacks(ReadFallbacks(profiles=["second"]))
    primary = protocol[1]
    primary.source_failures = {
        "/chat/findChats/Default": 503,
        "/chat/findContacts/Default": 503,
        "/group/fetchAllGroups/Default?getParticipants=false": 503,
    }
    result = service.invoke("chats_search", {"query": "Alex"})
    assert result.ok and result.data["connection"]["profile"] == "second"
    second.requests.clear()
    del primary.source_failures["/chat/findContacts/Default"]
    partial = service.invoke("chats_search", {"query": "Alex"})
    assert not partial.ok and partial.error["code"] == "SEARCH_PARTIAL"
    assert partial.data["chats"] and partial.data["connection"]["profile"] == "test" and not second.requests


def test_search_continuation_stays_on_the_backend_that_returned_it(protocol, two_connections):
    """Default recovery cannot move a cursor from its original backend; pinning permits the next page."""
    service, second, store = two_connections
    second.whatsapp = "open"
    protocol[1].whatsapp = "close"
    store.set_read_fallbacks(ReadFallbacks(profiles=["second"]))
    first = service.invoke("chats_search", {"limit": 1})
    assert first.ok and first.data["next_cursor"]
    protocol[1].whatsapp = "open"
    following = {"limit": 1, "cursor": first.data["next_cursor"]}
    unsafe = service.invoke("chats_search", following)
    assert not unsafe.ok and unsafe.error["code"] == "PROFILE_REQUIRED"
    safe = service.invoke("chats_search", {**following, "profile": first.data["connection"]["profile"]})
    assert safe.ok and safe.data["chats"] != first.data["chats"]


def test_local_cache_contention_does_not_change_backends(protocol, two_connections):
    """Retryable local storage contention must not be treated as a backend outage."""
    service, second, store = two_connections
    second.whatsapp = "open"
    store.set_read_fallbacks(ReadFallbacks(profiles=["second"]))
    assert service.invoke("chats_search", {"query": "Alex"}).ok
    with sqlite3.connect(service.state / "searches.sqlite3") as database:
        database.execute("BEGIN IMMEDIATE")
        blocked = service.invoke("chats_search", {"query": "Sam"})
    assert not blocked.ok and blocked.error["code"] == "SEARCH_CACHE_BUSY"
    assert blocked.data["connection"]["profile"] == "test" and not second.requests


@pytest.mark.parametrize(
    ("operation", "arguments"),
    [
        ("contacts_search", {"page": 2}),
        ("chats_list", {"offset": 1}),
        ("messages_read", {"chat": "15550000001", "page": 2}),
    ],
)
def test_unpinned_pages_require_the_original_connection(protocol, two_connections, operation, arguments):
    """Page numbers and offsets cannot silently select another history after an outage or recovery."""
    service, second, store = two_connections
    store.set_read_fallbacks(ReadFallbacks(profiles=["second"]))
    result = service.invoke(operation, arguments)
    assert not result.ok and result.error["code"] == "PROFILE_REQUIRED"
    assert not protocol[1].requests and not second.requests


async def test_mcp_read_fallback_matches_cli_and_observes_policy_changes(protocol, two_connections):
    """A running MCP session shares fallback configuration and source attribution with the CLI."""
    service, second, store = two_connections
    second.whatsapp = "open"
    protocol[1].whatsapp = "close"
    parameters = StdioServerParameters(
        command=str(Path(sys.executable).with_name("evoctl")),
        args=["mcp", "serve", "--mode", "write"],
        env={
            "EVOCTL_CONFIG_DIR": str(store.directory),
            "EVOCTL_STATE_DIR": str(service.state),
            "EVOCTL_TEST_KEY": os.environ["EVOCTL_TEST_KEY"],
        },
    )
    async with Client(parameters, read_timeout_seconds=15) as client:
        configured = await client.call_tool(
            "evoctl_write", {"action": "read_fallbacks_set", "arguments": {"profiles": ["second"]}}
        )
        assert not configured.is_error
        read = await client.call_tool("evoctl_read", {"action": "chats_list"})
        assert not read.is_error and read.structured_content == json.loads(command(protocol, "chats", "list").stdout)
        assert command(protocol, "remote", "fallback", "--clear").returncode == 0
        unchanged = await client.call_tool("evoctl_read", {"action": "chats_list"})
        assert "connection" not in unchanged.structured_content["data"]
    denied = Service(store, service.state, mode="read").invoke("read_fallbacks_set", {"profiles": ["second"]})
    assert not denied.ok and not store.read_fallbacks().profiles


def test_ssh_profiles_keep_connections_and_identity_separate(tmp_path):
    """Two deployments on one SSH host cannot disconnect or reuse each other's authenticated master."""
    settings = Profile(transport="ssh", ssh_host="example", identity_file="/keys/first")
    first = Transport("first", settings, tmp_path)
    second = Transport("second", settings, tmp_path)
    changed_identity = Transport("first", settings.model_copy(update={"identity_file": "/keys/second"}), tmp_path)
    assert len({first.control_path(), second.control_path(), changed_identity.control_path()}) == 3
    assert first.control_path() == Transport("first", settings, tmp_path).control_path()
    assert f"ControlPath={first.control_path()}" in first.ssh_arguments()
    assert f"ControlPath={first.control_path()}" not in second.ssh_arguments()
