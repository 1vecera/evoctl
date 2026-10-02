"""Independent deployments remain selectable through real CLI and MCP sessions."""

from __future__ import annotations

import json
import os
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from conftest import ProtocolState, handler_for
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters
from test_cli import command

from evoctl.config import ConfigStore, Profile
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
    result = service.invoke("message_send", {"to": "15550000001", "text": "Fixture only", "request_id": "no-fallback"})
    assert not result.ok
    assert any(path == "/message/sendText/Default" for _, path, _ in second.requests)
    assert not protocol[1].requests and not protocol[1].messages


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
