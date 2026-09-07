"""Three MCP entry points with operation schemas loaded only when needed."""

from __future__ import annotations

from typing import Any

from mcp.types import Tool, ToolAnnotations
from pydantic import Field, ValidationError

from evoctl.models import ApiCall, Envelope, Input
from evoctl.service import Service
from evoctl.worker import EvoError


class DiscoverInput(Input):
    """Find an operation or obtain the exact schema for its arguments."""

    query: str = Field(default="", max_length=200, description="Search workflow names and REST routes.")
    operation: str = Field(
        default="", max_length=200, description="Exact workflow or REST ID to inspect instead of search."
    )
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


class ExecuteInput(Input):
    """Route validated operation arguments through an explicitly read or write tool."""

    action: str = Field(description="Workflow name, or api for a REST call. Use evoctl_discover for argument schemas.")
    arguments: dict[str, Any] = Field(default_factory=dict, description="Exact operation arguments, including profile.")


class Surface:
    """Keep the initial tool manifest small without weakening operation-level validation."""

    def __init__(self, service: Service) -> None:
        """Reuse the shared operation registry and configured capability mode."""
        self.service = service
        self.workflows = {
            definition.name: definition
            for definition in service.available_tools()
            if not definition.name.startswith("api_")
        }

    def tools(self) -> list[Tool]:
        """Advertise discovery and separate read/write execution with capability-filtered action enums."""
        descriptions = {
            "discover": "Search workflows and REST routes, or inspect one operation's exact argument schema.",
            "read": "Read status, contacts, chats, messages or receipts. action=api calls a read REST operation.",
            "write": "Send, pair or administer a deployment. Discover arguments first; writes can affect real users.",
        }
        tools = []
        for kind in ("discover", "read", "write"):
            if kind == "write" and self.service.mode == "read":
                continue
            schema = DiscoverInput.model_json_schema() if kind == "discover" else ExecuteInput.model_json_schema()
            if kind != "discover":
                schema["properties"]["action"]["enum"] = [
                    name
                    for name, definition in self.workflows.items()
                    if (definition.access == "read") == (kind == "read")
                ] + ["api"]
            tools.append(
                Tool(
                    name=f"evoctl_{kind}",
                    title=f"Evolution {kind}",
                    description=descriptions[kind],
                    input_schema=schema,
                    output_schema=Envelope.model_json_schema(),
                    annotations=ToolAnnotations(
                        read_only_hint=kind != "write",
                        destructive_hint=kind == "write",
                        idempotent_hint=kind != "write",
                        open_world_hint=kind != "discover",
                    ),
                )
            )
        return tools

    def discover(self, arguments: DiscoverInput) -> Envelope:
        """Return concise search results or one full schema, restricted to the server's allowed operations."""
        if arguments.operation:
            if arguments.operation in self.workflows:
                definition = self.workflows[arguments.operation]
                return Envelope(
                    ok=True,
                    data={
                        "operation": definition.name,
                        "tool": "evoctl_read" if definition.access == "read" else "evoctl_write",
                        "action": definition.name,
                        "description": definition.description,
                        "arguments_schema": definition.input_model.model_json_schema(),
                    },
                )
            response = self.service.invoke("api_schema", {"operation": arguments.operation})
            if not response.ok:
                return response
            return Envelope(
                ok=True,
                data={
                    "operation": arguments.operation,
                    "tool": "evoctl_read" if response.data["access"] == "read" else "evoctl_write",
                    "action": "api",
                    "arguments_schema": ApiCall.model_json_schema(),
                    "api": response.data,
                },
            )
        words = arguments.query.casefold().split()
        workflows = [
            {
                "operation": definition.name,
                "kind": "workflow",
                "title": definition.title,
                "access": definition.access,
                "tool": "evoctl_read" if definition.access == "read" else "evoctl_write",
            }
            for definition in self.workflows.values()
            if all(word in (definition.name + " " + definition.description).casefold() for word in words)
        ]
        routes = self.service.catalog.search(
            arguments.query, len(self.service.catalog.operations), 0, self.service.mode
        )
        matches = workflows + [
            {
                "operation": item["id"],
                "kind": "api",
                "method": item["method"],
                "path": item["path"],
                "access": item["access"],
                "tool": "evoctl_read" if item["access"] == "read" else "evoctl_write",
            }
            for item in routes["items"]
        ]
        end = arguments.offset + arguments.limit
        return Envelope(
            ok=True,
            data={
                "items": matches[arguments.offset : end],
                "total": len(matches),
                "next_offset": end if end < len(matches) else None,
                "next_step": "Call evoctl_discover with operation=<exact ID> for the argument schema.",
            },
        )

    def invoke(self, name: str, arguments: dict[str, Any]) -> Envelope:
        """Enforce read/write boundaries before delegating exact argument validation to the service."""
        try:
            if name == "evoctl_discover":
                return self.discover(DiscoverInput.model_validate(arguments))
            if name not in {"evoctl_read", "evoctl_write"}:
                raise EvoError("TOOL_NOT_FOUND", "Use evoctl_discover, evoctl_read, or evoctl_write.")
            if name == "evoctl_write" and self.service.mode == "read":
                raise EvoError("CAPABILITY_DENIED", "This server exposes read operations only.")
            request = ExecuteInput.model_validate(arguments)
            if request.action == "api":
                call = ApiCall.model_validate(request.arguments)
                access = self.service.catalog.operation(call.operation)["access"]
                operation = f"api_{access}"
            else:
                if request.action not in self.workflows:
                    raise EvoError("CAPABILITY_DENIED", "This workflow is unavailable in the server's capability mode.")
                operation = request.action
                access = self.workflows[operation].access
            if (access == "read") != (name == "evoctl_read"):
                target = "evoctl_read" if access == "read" else "evoctl_write"
                raise EvoError("WRONG_MCP_TOOL", f"This operation must use {target}.")
            return self.service.invoke(operation, request.arguments)
        except ValidationError:
            return Envelope(
                ok=False, error=EvoError("INVALID_INPUT", "Inspect the operation's argument schema.").as_dict()
            )
        except EvoError as error:
            return Envelope(ok=False, error=error.as_dict())
