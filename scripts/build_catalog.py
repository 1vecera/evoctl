"""Compile REST route facts from the pinned Evolution source, enriched by its public OpenAPI."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import yaml

ROUTE = re.compile(
    r"\.(get|post|put|patch|delete)\(\s*(?:this\.routerPath\('([^']+)'(?:,\s*(false|true))?\)|'(/[^']*)')"
)
MOUNT = re.compile(r"\.use\('([^']*)',\s*new\s+(\w+)\(")


def snake(value: str) -> str:
    """Normalize API action names into predictable operation identifiers."""
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def resolve_refs(
    value: Any, document: dict[str, Any], seen: frozenset[str] = frozenset(), property_map: bool = False
) -> Any:
    """Resolve local OpenAPI schema references without fetching external resources."""
    if isinstance(value, dict):
        if "$ref" in value:
            reference = value["$ref"]
            if not reference.startswith("#/") or reference in seen:
                return {}
            target: Any = document
            for part in reference[2:].split("/"):
                target = target[part.replace("~1", "/").replace("~0", "~")]
            return resolve_refs(target, document, seen | {reference})
        return {
            key: resolve_refs(item, document, seen, property_map=key == "properties")
            for key, item in value.items()
            if property_map or key not in {"description", "summary", "example", "examples", "externalDocs"}
        }
    if isinstance(value, list):
        return [resolve_refs(item, document, seen) for item in value]
    return value


def documented_operations(directory: Path | None) -> dict[tuple[str, str], dict[str, Any]]:
    """Index optional local copies of the official OpenAPI files."""
    operations = {}
    if directory is None:
        return operations
    for path in sorted(directory.glob("*.yaml")):
        document = yaml.safe_load(path.read_text())
        for route, methods in document["paths"].items():
            for method, operation in methods.items():
                if method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                    operations[(method.upper(), route)] = resolve_refs(operation, document)
    return operations


def access_level(method: str, route: str, action: str) -> str:
    """Classify actual semantics, including GET endpoints that change remote state."""
    if route.startswith("/baileys/") or "/webhook/" in route or route == "/verify-creds":
        return "admin"
    if method == "DELETE" or action in {"restart", "logout", "delete", "removeProfilePicture", "leaveGroup"}:
        return "admin"
    if action in {"connect", "acceptInviteCode", "inviteCode"}:
        return "write"
    if method == "GET" or re.match(r"^(find|fetch|get|whatsappNumbers|onWhatsapp|profilePictureUrl)", action):
        return "read"
    if route.startswith(("/message/", "/chat/", "/group/", "/call/", "/business/")):
        return "write"
    return "admin"


def compile_catalog(source: Path, specifications: Path | None) -> dict[str, Any]:
    """Traverse mounted routers and fail on unrecognized REST declarations."""
    files = sorted((source / "src/api").rglob("*.router.ts"))
    classes = {}
    for file in files:
        match = re.search(r"export class (\w+)", file.read_text())
        if match:
            classes[match[1]] = file
    documented = documented_operations(specifications)
    queue = [(source / "src/api/routes/index.router.ts", "")]
    visited: set[tuple[Path, str]] = set()
    operations = []
    exclusions = []
    while queue:
        file, prefix = queue.pop(0)
        if (file, prefix) in visited:
            continue
        visited.add((file, prefix))
        content = file.read_text()
        if file.name == "view.router.ts":
            exclusions.append({"path": "/manager/*", "reason": "HTML frontend"})
            continue
        for mount, class_name in MOUNT.findall(content):
            queue.append((classes[class_name], (prefix + mount).rstrip("/")))
        matches = list(ROUTE.finditer(content))
        declarations = re.finditer(
            r"(?m)^\s*(?:(?:this\.)?router)?(?P<route>\.(get|post|put|patch|delete)\(\s*)", content
        )
        if not {match.start("route") for match in declarations} <= {match.start() for match in matches}:
            raise ValueError(f"Unrecognized REST route syntax in {file.relative_to(source)}.")
        for index, match in enumerate(matches):
            method, dynamic, param, literal = match.groups()
            suffix = "/" + dynamic + ("/:instanceName" if param != "false" else "") if dynamic else literal
            route = re.sub(r":([A-Za-z0-9_]+)", r"{\1}", prefix + suffix)
            if "*" in route:
                exclusions.append({"path": route, "reason": "Static frontend assets"})
                continue
            end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
            fragment = content[match.end() : end]
            action = (dynamic or literal.strip("/") or "info").split("/")[0]
            group = prefix.strip("/") or "server"
            segments = (dynamic or literal.strip("/") or "info").split("/")
            identifier = snake(group) + "." + ".".join(snake(part) for part in segments if not part.startswith(":"))
            metadata = documented.get((method.upper(), route), {})
            request_body = metadata.get("requestBody", {}).get("content", {}).get("application/json", {})
            schema = copy.deepcopy(request_body.get("schema", {"type": "object", "additionalProperties": True}))
            schema_source = "upstream_openapi" if request_body else "undocumented"
            if action == "sendText":
                schema = {
                    "type": "object",
                    "required": ["number", "text"],
                    "properties": {"number": {"type": "string"}, "text": {"type": "string"}},
                    "additionalProperties": True,
                }
                schema_source = "runtime_verified"
            # Runtime 2.3.7 differs from the OpenAPI pagination example.
            if action in {"findContacts", "findMessages", "findStatusMessage"}:
                schema = {
                    "type": "object",
                    "properties": {
                        "where": {"type": "object"},
                        "page": {"type": "integer", "minimum": 1},
                        "offset": {"type": "integer", "minimum": 1},
                    },
                }
                schema_source = "runtime_verified"
            operations.append(
                {
                    "id": identifier,
                    "method": method.upper(),
                    "path": route,
                    "access": access_level(method.upper(), route, action),
                    "body_schema": schema,
                    "schema_source": schema_source,
                    "query_parameters": [item for item in metadata.get("parameters", []) if item["in"] == "query"],
                    "path_parameters": re.findall(r"\{([^}]+)\}", route),
                    "source": str(file.relative_to(source)),
                    "source_line": content[: match.start()].count("\n") + 1,
                    "multipart": "upload.single" in fragment,
                    "sensitive_response": action == "getAuthState",
                }
            )
    counts: dict[str, int] = {}
    for operation in operations:
        counts[operation["id"]] = counts.get(operation["id"], 0) + 1
    for operation in operations:
        if counts[operation["id"]] > 1:
            operation["id"] += "." + operation["method"].lower()
    if len({item["id"] for item in operations}) != len(operations):
        raise ValueError("Operation identifiers collide; review the new upstream routes.")
    source_digest = hashlib.sha256()
    for file in files:
        source_digest.update(file.read_bytes())
    return {
        "api": "Evolution API",
        "version": json.loads((source / "package.json").read_text())["version"],
        "source": "https://github.com/evolution-foundation/evolution-api/tree/2.3.7",
        "route_source_sha256": source_digest.hexdigest(),
        "operations": sorted(operations, key=lambda item: item["id"]),
        "excluded": exclusions,
    }


def main() -> None:
    """Build or verify the committed catalog from a local pinned upstream checkout."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--specifications", type=Path)
    parser.add_argument("--output", type=Path, default=Path("src/evoctl/api_catalog.json"))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    catalog = compile_catalog(args.source, args.specifications)
    content = json.dumps(catalog, ensure_ascii=False, indent=2) + "\n"
    if args.check:
        existing = json.loads(args.output.read_text())
        if args.specifications is None:
            fields = ("id", "method", "path", "access", "source", "source_line", "multipart", "sensitive_response")
            actual = [{key: item[key] for key in fields} for item in catalog["operations"]]
            expected = [{key: item[key] for key in fields} for item in existing["operations"]]
            matches = actual == expected and catalog["route_source_sha256"] == existing["route_source_sha256"]
        else:
            matches = existing == catalog
        if not matches:
            raise SystemExit("Catalog differs from the pinned source.")
    else:
        args.output.write_text(content)
    print(json.dumps({"operations": len(catalog["operations"]), "version": catalog["version"]}))


if __name__ == "__main__":
    main()
