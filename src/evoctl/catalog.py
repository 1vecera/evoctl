"""Offline API discovery and semantic access control from one versioned catalog."""

from __future__ import annotations

import json
import re
from importlib.resources import files
from typing import Any, Literal
from urllib.parse import quote

from jsonschema import Draft7Validator

from evoctl.worker import EvoError

Mode = Literal["read", "write", "admin"]
ACCESS = {"read": 0, "write": 1, "admin": 2}


class Catalog:
    """Discover and validate API operations without loading hundreds of MCP tools."""

    def __init__(self) -> None:
        """Load the bundled route catalog without network access."""
        self.document = json.loads(files("evoctl").joinpath("api_catalog.json").read_text())
        self.operations = {item["id"]: item for item in self.document["operations"]}

    def operation(self, operation_id: str) -> dict[str, Any]:
        """Require an exact catalog ID rather than an arbitrary API path."""
        if operation_id not in self.operations:
            raise EvoError(
                "OPERATION_NOT_FOUND", "Unknown API operation.", "Use evoctl api list --query to find its ID."
            )
        return self.operations[operation_id]

    def search(self, query: str = "", limit: int = 20, offset: int = 0, mode: Mode = "admin") -> dict[str, Any]:
        """Return a bounded page of operation names, access levels, methods, and paths."""
        matches = [
            item
            for item in self.operations.values()
            if ACCESS[item["access"]] <= ACCESS[mode]
            and all(word in (item["id"] + " " + item["path"]).lower() for word in query.lower().split())
        ]
        return {
            "version": self.document["version"],
            "total": len(matches),
            "items": [
                {key: item[key] for key in ("id", "method", "path", "access")}
                for item in matches[offset : offset + limit]
            ],
            "next_offset": offset + limit if offset + limit < len(matches) else None,
        }

    def prepare(
        self,
        operation_id: str,
        instance: str,
        body: dict[str, Any] | None,
        path_parameters: dict[str, str],
        query: dict[str, Any],
        mode: Mode,
        max_bytes: int = 1_048_576,
    ) -> dict[str, Any]:
        """Validate access and input before any network operation or idempotency reservation."""
        operation = self.operation(operation_id)
        if ACCESS[operation["access"]] > ACCESS[mode]:
            raise EvoError("CAPABILITY_DENIED", f"This operation requires {operation['access']} access.")
        parameters = {"instanceName": instance, **path_parameters}
        required = set(operation["path_parameters"])
        missing = required - parameters.keys()
        extra = path_parameters.keys() - required
        if missing or extra:
            raise EvoError(
                "INVALID_PARAMETERS",
                "Path parameter names do not match this operation.",
                "Inspect the operation with evoctl api schema.",
            )
        path = operation["path"]
        for parameter in required:
            value = parameters[parameter]
            if not value or ".." in value or re.search(r"[/\\\x00-\x1f]", value):
                raise EvoError("INVALID_PARAMETERS", "Path parameter values cannot contain traversal or separators.")
            path = path.replace("{" + parameter + "}", quote(value, safe=""))
        if operation["schema_source"] == "runtime_verified":
            errors = list(Draft7Validator(operation["body_schema"]).iter_errors(body or {}))
            if errors:
                locations = [".".join(map(str, error.path)) or "body" for error in errors[:5]]
                raise EvoError(
                    "INVALID_BODY",
                    "Request body does not match the operation schema.",
                    "Check fields: " + ", ".join(locations),
                )
        return {
            "method": operation["method"],
            "path": path,
            "body": body,
            "query": query,
            "authenticated": operation_id not in {"server.info", "server.metrics"},
            "read_only": operation["access"] == "read",
            "max_bytes": max_bytes,
            "sensitive_response": operation["sensitive_response"],
        }
