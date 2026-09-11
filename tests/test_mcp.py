"""Real MCP client/server processes using both modern and legacy negotiation."""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters
from test_cli import command


@pytest.mark.parametrize("negotiation", ["auto", "legacy"])
async def test_stdio_discovery_results_and_denied_writes(protocol, negotiation):
    """Both protocol handshakes discover typed tools and enforce read-only execution."""
    service, state, store = protocol
    parameters = StdioServerParameters(
        command=str(Path(sys.executable).with_name("evoctl")),
        args=["mcp", "serve"],
        env={
            "EVOCTL_CONFIG_DIR": str(store.directory),
            "EVOCTL_STATE_DIR": str(service.state),
            "EVOCTL_TEST_KEY": os.environ["EVOCTL_TEST_KEY"],
        },
    )
    async with Client(parameters, mode=negotiation, read_timeout_seconds=10) as client:
        listing = await client.list_tools()
        tools = {tool.name: tool for tool in listing.tools}
        assert set(tools) == {"evoctl_discover", "evoctl_read"}
        assert tools["evoctl_read"].annotations.read_only_hint
        discovery = await client.call_tool("evoctl_discover", {"operation": "contacts_search"})
        contract = discovery.structured_content["data"]
        assert contract["tool"] == "evoctl_read" and contract["action"] == "contacts_search"
        Draft202012Validator(contract["arguments_schema"]).validate({"query": "novak"})
        result = await client.call_tool("evoctl_read", {"action": "contacts_search", "arguments": {"query": "novak"}})
        assert not result.is_error
        assert result.structured_content["data"]["contacts"][0]["jid"] == "15550000001@s.whatsapp.net"
        denied = await client.call_tool(
            "evoctl_read", {"action": "api", "arguments": {"operation": "instance.connect"}}
        )
        assert denied.is_error and not state.messages


async def test_mcp_write_shares_cli_idempotency(protocol):
    """An MCP write returns a typed receipt and cannot duplicate an earlier service send."""
    service, state, store = protocol
    arguments = {"to": "15550000001", "text": "Shared receipt", "request_id": "shared"}
    first = service.invoke("message_send", arguments)
    parameters = StdioServerParameters(
        command=str(Path(sys.executable).with_name("evoctl")),
        args=["mcp", "serve", "--mode", "write"],
        env={"EVOCTL_CONFIG_DIR": str(store.directory), "EVOCTL_STATE_DIR": str(service.state)},
    )
    async with Client(parameters, read_timeout_seconds=10) as client:
        listing = await client.list_tools()
        assert {tool.name for tool in listing.tools} == {"evoctl_discover", "evoctl_read", "evoctl_write"}
        receipt = await client.call_tool("evoctl_write", {"action": "message_send", "arguments": arguments})
        assert not receipt.is_error
        assert receipt.structured_content["data"]["message_id"] == first.data["message_id"]
        assert receipt.structured_content["data"]["duplicate_suppressed"]
        assert len(state.messages) == 1


async def test_saved_contact_names_are_shared_by_cli_and_running_mcp(protocol):
    """A running MCP process sees CLI name changes and exposes the same reversible local write."""
    service, state, store = protocol
    state.contacts[0]["pushName"] = "Jiří"
    state.chats[0]["pushName"] = None
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
        contract = await client.call_tool("evoctl_discover", {"operation": "contacts_name"})
        assert contract.structured_content["data"]["tool"] == "evoctl_write"
        cli = command(protocol, "contacts", "name", "15550000001", "Jiří Dvořák")
        assert cli.returncode == 0 and not state.requests
        request = {"action": "chats_search", "arguments": {"query": "jiri dvorak"}}
        found = await client.call_tool("evoctl_read", request)
        assert not found.is_error and found.structured_content["data"]["chats"] == [
            {"jid": "15550000001@s.whatsapp.net", "name": "Jiří Dvořák", "kind": "person"}
        ]
        cli_search = command(protocol, "chats", "search", "jiri dvorak")
        assert json.loads(cli_search.stdout) == found.structured_content
        cleared = await client.call_tool(
            "evoctl_write", {"action": "contacts_name", "arguments": {"jid": "15550000001", "name": ""}}
        )
        assert not cleared.is_error and not cleared.structured_content["data"]["saved"]
        assert not json.loads(command(protocol, "contacts", "search", "dvorak").stdout)["data"]["contacts"]
        assert not state.messages


@pytest.mark.parametrize("connection", ["open", "close"])
async def test_bundled_pairing_keeps_qr_image_content(protocol, connection):
    """Pairing through the bundled write tool returns an image only when a phone scan is needed."""
    service, state, store = protocol
    state.whatsapp = connection
    parameters = StdioServerParameters(
        command=str(Path(sys.executable).with_name("evoctl")),
        args=["mcp", "serve", "--mode", "write"],
        env={
            "EVOCTL_CONFIG_DIR": str(store.directory),
            "EVOCTL_STATE_DIR": str(service.state),
            "EVOCTL_TEST_KEY": os.environ["EVOCTL_TEST_KEY"],
        },
    )
    async with Client(parameters, read_timeout_seconds=10) as client:
        response = await client.call_tool("evoctl_write", {"action": "instance_pair"})
        assert not response.is_error
        images = [item for item in response.content if item.type == "image"]
        if connection == "open":
            assert not images
            assert not any(path == "/instance/connect/Default" for _, path, _ in state.requests)
        else:
            assert len(images) == 1 and images[0].mime_type == "image/png"
            saved = Path(response.structured_content["data"]["path"])
            assert base64.b64decode(images[0].data) == saved.read_bytes()
            assert saved.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("mode", ["read", "write", "admin"])
async def test_discovery_and_execution_preserve_capability_boundaries(protocol, mode):
    """Bundling cannot expose or execute operations outside the selected mode."""
    service, state, store = protocol
    parameters = StdioServerParameters(
        command=str(Path(sys.executable).with_name("evoctl")),
        args=["mcp", "serve", "--mode", mode],
        env={
            "EVOCTL_CONFIG_DIR": str(store.directory),
            "EVOCTL_STATE_DIR": str(service.state),
            "EVOCTL_TEST_KEY": os.environ["EVOCTL_TEST_KEY"],
        },
    )
    async with Client(parameters, read_timeout_seconds=10) as client:
        listing = await client.list_tools()
        assert len(listing.model_dump_json().encode()) < 5500
        discovery = await client.call_tool("evoctl_discover", {"query": "group", "limit": 2})
        page = discovery.structured_content["data"]
        assert len(page["items"]) == 2 and page["next_offset"] == 2
        read = await client.call_tool(
            "evoctl_read",
            {
                "action": "api",
                "arguments": {"operation": "chat.find_contacts", "body": {"where": {}, "page": 1, "offset": 1}},
            },
        )
        assert not read.is_error
        wrong = await client.call_tool("evoctl_read", {"action": "api", "arguments": {"operation": "instance.connect"}})
        assert wrong.is_error
        malformed = await client.call_tool("evoctl_read", {"action": "contacts_search", "arguments": {"limti": 2}})
        assert malformed.is_error
        legacy = await client.call_tool("contacts_search", {"query": "Alex"})
        assert legacy.is_error
        if mode != "read":
            admin = await client.call_tool("evoctl_discover", {"operation": "remote_add"})
            assert admin.is_error == (mode != "admin")
            denied = await client.call_tool(
                "evoctl_write",
                {
                    "action": "api",
                    "arguments": {
                        "operation": "instance.create",
                        "body": {"instanceName": "Other"},
                        "request_id": "admin",
                        "dry_run": True,
                    },
                },
            )
            assert denied.is_error == (mode != "admin")
        assert not state.messages


@pytest.mark.parametrize("failed_source", [False, True])
async def test_conversation_search_cli_mcp_parity(protocol, failed_source):
    """CLI and the three-tool MCP surface discover and execute identical search/error contracts."""
    service, state, store = protocol
    if failed_source:
        state.source_failures["/group/fetchAllGroups/Default?getParticipants=false"] = 503
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
        listing = await client.list_tools()
        assert {tool.name for tool in listing.tools} == {"evoctl_discover", "evoctl_read", "evoctl_write"}
        discovered = await client.call_tool("evoctl_discover", {"query": "search group"})
        assert "chats_search" in [item["operation"] for item in discovered.structured_content["data"]["items"]]
        contract = await client.call_tool("evoctl_discover", {"operation": "chats_search"})
        schema = contract.structured_content["data"]["arguments_schema"]
        assert schema["properties"]["kind"]["default"] == "all"
        Draft202012Validator(schema).validate({"query": "novak"})
        cli = command(protocol, "chats", "search", "novak")
        mcp = await client.call_tool("evoctl_read", {"action": "chats_search", "arguments": {"query": "novak"}})
        assert cli.returncode == (1 if failed_source else 0)
        assert mcp.is_error == failed_source
        assert json.loads(cli.stdout) == mcp.structured_content
        bad_cli = command(protocol, "chats", "search", "--kind", "people")
        bad_mcp = await client.call_tool("evoctl_read", {"action": "chats_search", "arguments": {"kind": "people"}})
        assert bad_cli.returncode == 2 and bad_mcp.is_error
        assert json.loads(bad_cli.stdout) == bad_mcp.structured_content
        assert not state.messages


async def test_search_continuation_crosses_cli_and_mcp(protocol):
    """Opaque cursors retain buffered results and exact replay across separate CLI and MCP processes."""
    service, state, store = protocol
    first = json.loads(command(protocol, "chats", "search", "--limit", "1").stdout)
    cursor = first["data"]["next_cursor"]
    second = json.loads(command(protocol, "chats", "search", "--limit", "1", "--cursor", cursor).stdout)
    before = len(state.requests)
    parameters = StdioServerParameters(
        command=str(Path(sys.executable).with_name("evoctl")),
        args=["mcp", "serve"],
        env={
            "EVOCTL_CONFIG_DIR": str(store.directory),
            "EVOCTL_STATE_DIR": str(service.state),
            "EVOCTL_TEST_KEY": os.environ["EVOCTL_TEST_KEY"],
        },
    )
    async with Client(parameters, read_timeout_seconds=15) as client:
        replay = await client.call_tool(
            "evoctl_read", {"action": "chats_search", "arguments": {"limit": 1, "cursor": cursor}}
        )
        assert replay.structured_content == second and len(state.requests) == before
        third = await client.call_tool(
            "evoctl_read",
            {
                "action": "chats_search",
                "arguments": {"limit": 1, "cursor": second["data"]["next_cursor"]},
            },
        )
        third_cli = json.loads(
            command(protocol, "chats", "search", "--limit", "1", "--cursor", second["data"]["next_cursor"]).stdout
        )
        assert third_cli == third.structured_content and len(state.requests) == before
