"""Real MCP client/server processes using both modern and legacy negotiation."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters


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
        assert "contacts_search" in tools and "message_send" not in tools
        assert tools["contacts_search"].annotations.read_only_hint
        result = await client.call_tool("contacts_search", {"query": "novak"})
        assert not result.is_error
        assert result.structured_content["data"]["contacts"][0]["jid"] == "15550000001@s.whatsapp.net"
        denied = await client.call_tool(
            "message_send", {"to": "15550000001", "text": "Do not send", "request_id": "denied"}
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
        assert "message_send" in {tool.name for tool in listing.tools}
        assert "api_admin" not in {tool.name for tool in listing.tools}
        receipt = await client.call_tool("message_send", arguments)
        assert not receipt.is_error
        assert receipt.structured_content["data"]["message_id"] == first.data["message_id"]
        assert receipt.structured_content["data"]["duplicate_suppressed"]
        assert len(state.messages) == 1
