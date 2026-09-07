"""Official MCP SDK transport with the same schemas and results as the CLI."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

from mcp.server import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ImageContent,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
)

from evoctl import __version__
from evoctl.mcp_surface import Surface
from evoctl.service import Service


def create_server(service: Service) -> Server[Any]:
    """Expose bounded task tools and capability-filtered catalog calls over MCP."""

    surface = Surface(service)

    async def list_tools(
        _context: ServerRequestContext[Any], _params: PaginatedRequestParams | None
    ) -> ListToolsResult:
        """Publish exact Pydantic input schemas and a stable structured result schema."""
        return ListToolsResult(tools=surface.tools())

    async def call_tool(_context: ServerRequestContext[Any], params: CallToolRequestParams) -> CallToolResult:
        """Return execution failures as MCP errors while keeping their structured error envelope."""
        result = await asyncio.to_thread(surface.invoke, params.name, params.arguments or {})
        content: list[Any] = [TextContent(type="text", text=result.model_dump_json())]
        if (
            result.ok
            and params.name == "evoctl_write"
            and (params.arguments or {}).get("action") == "instance_pair"
            and result.data["path"]
        ):
            content.append(
                ImageContent(
                    type="image",
                    mime_type="image/png",
                    data=base64.b64encode(Path(result.data["path"]).read_bytes()).decode(),
                )
            )
        return CallToolResult(
            content=content, structured_content=json.loads(result.model_dump_json()), is_error=not result.ok
        )

    return Server(
        "evoctl",
        version=__version__,
        title="Evolution API control",
        description="Structured Evolution API messaging and remote operations.",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


async def serve(service: Service) -> None:
    """Run stdio only; hosting an unauthenticated HTTP MCP listener is not part of this command."""
    server = create_server(service)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())
