"""Task operations shared by CLI, MCP, and library callers."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from evoctl.catalog import ACCESS, Catalog, Mode
from evoctl.config import ConfigStore, private_directory, state_directory
from evoctl.contact_names import ContactNames
from evoctl.ledger import SendLedger
from evoctl.models import (
    ApiCall,
    ApiSchema,
    CatalogSearch,
    ChatsList,
    ChatsSearch,
    ContactName,
    ContactsSearch,
    EmptyInput,
    Envelope,
    MessageSend,
    MessagesRead,
    MessageStatus,
    ProfileAdd,
    ProfileInput,
    RequestStatus,
    ServicesOperation,
)
from evoctl.search import ConversationSearch, SearchCache, recipient
from evoctl.transport import Transport
from evoctl.worker import EvoError, redact


@dataclass(frozen=True)
class ToolDefinition:
    """One task contract used for discovery, validation, permissions, and dispatch."""

    name: str
    title: str
    description: str
    input_model: type[BaseModel]
    access: Mode


TOOLS = (
    ToolDefinition(
        "remotes_list",
        "List connection profiles",
        "List configured deployments and their non-secret settings.",
        EmptyInput,
        "read",
    ),
    ToolDefinition(
        "remote_add",
        "Save a connection profile",
        "Save a named local or SSH deployment. Existing names require replace=true.",
        ProfileAdd,
        "admin",
    ),
    ToolDefinition(
        "remote_connect",
        "Connect a remote host",
        "Verify SSH and install the worker. Use the interactive CLI login command for trust or password prompts.",
        ProfileInput,
        "admin",
    ),
    ToolDefinition(
        "remote_key_setup",
        "Set up SSH key access",
        "Install a public key through existing SSH access, then verify a fresh connection using that key.",
        ProfileInput,
        "admin",
    ),
    ToolDefinition(
        "remote_disconnect",
        "Close owned SSH connection",
        "Close evoctl's SSH connection and GUI forwards for this profile.",
        ProfileInput,
        "admin",
    ),
    ToolDefinition(
        "status",
        "Inspect deployment health",
        "Report SSH, runtime, containers, API authentication, and WhatsApp connection state independently.",
        ProfileInput,
        "read",
    ),
    ToolDefinition(
        "services_manage",
        "Start existing services",
        "Start or restart existing deployment containers. Missing containers or data volumes are never recreated.",
        ServicesOperation,
        "admin",
    ),
    ToolDefinition(
        "contacts_search",
        "Find WhatsApp contacts",
        "Legacy contacts-only name/JID search with a page budget. Use chats_search to find people and groups together.",
        ContactsSearch,
        "read",
    ),
    ToolDefinition(
        "contacts_name",
        "Save a contact name",
        "Save or clear an operator-confirmed local name for an exact JID. "
        "Used by contact/chat search and chat listing; never renames a WhatsApp contact or sends a message.",
        ContactName,
        "write",
    ),
    ToolDefinition(
        "chats_search",
        "Find WhatsApp people and groups",
        "Search personal contacts and group conversations together by name, subject, phone or JID. "
        "Returns exact recipient JIDs, display names and person/group kind; bounded scans expose partial failures "
        "and replayable continuation. Resolve ambiguous matches explicitly before sending.",
        ChatsSearch,
        "read",
    ),
    ToolDefinition(
        "chats_list",
        "List WhatsApp conversations",
        "Read a bounded page of chats without marking them read.",
        ChatsList,
        "read",
    ),
    ToolDefinition(
        "messages_read",
        "Read a conversation",
        "Read a page from one exact JID, including IDs for receipt lookup. Does not send read receipts.",
        MessagesRead,
        "read",
    ),
    ToolDefinition(
        "message_send",
        "Send a WhatsApp message",
        "Send exact text to an exact JID once per request_id. Pending and server acknowledgment are not delivery.",
        MessageSend,
        "write",
    ),
    ToolDefinition(
        "message_status",
        "Check message delivery",
        "Inspect receipts to distinguish pending, server acknowledgment, delivered, read, played, error, or unknown.",
        MessageStatus,
        "read",
    ),
    ToolDefinition(
        "request_status",
        "Recover a send receipt",
        "Look up the local idempotency ledger without sending another message.",
        RequestStatus,
        "read",
    ),
    ToolDefinition(
        "instance_pair",
        "Prepare WhatsApp pairing",
        "Get an ephemeral pairing QR image. An open session stays connected; phone scanning remains manual.",
        ProfileInput,
        "write",
    ),
    ToolDefinition(
        "manager_url",
        "Open the management GUI",
        "Return the Evolution Manager URL, creating a local-only SSH forward for a remote deployment.",
        ProfileInput,
        "admin",
    ),
    ToolDefinition(
        "api_search",
        "Search all API operations",
        "Discover versioned Evolution REST operations by name or route, within the server's allowed capabilities.",
        CatalogSearch,
        "read",
    ),
    ToolDefinition(
        "api_schema",
        "Inspect an API operation",
        "Return method, path parameters, body schema, and access level for an exact operation ID.",
        ApiSchema,
        "read",
    ),
    ToolDefinition(
        "api_read",
        "Call a read API operation",
        "Execute a read-only catalog operation. Discover its input contract with api_schema.",
        ApiCall,
        "read",
    ),
    ToolDefinition(
        "api_write",
        "Call a messaging API operation",
        "Execute a messaging operation with an idempotency key. Discover its input contract with api_schema.",
        ApiCall,
        "write",
    ),
    ToolDefinition(
        "api_admin",
        "Call an administrative API operation",
        "Execute an administrative operation with an idempotency key, including destructive actions. See api_schema.",
        ApiCall,
        "admin",
    ),
)


def normalize_jid(value: str) -> str:
    """Accept explicit international numbers and WhatsApp JIDs; never infer a recipient from a name."""
    if re.fullmatch(r"\+?\d{5,20}", value):
        return value.lstrip("+") + "@s.whatsapp.net"
    if re.fullmatch(r"[0-9][0-9:._-]{3,180}@(s\.whatsapp\.net|g\.us|lid|broadcast|newsletter)", value):
        return value
    raise EvoError("INVALID_RECIPIENT", "Use an exact JID or international digits.", "Resolve names with chats search.")


def normalized_status(status: Any) -> str:
    """Translate upstream receipt names and protobuf enum values without claiming delivery from acceptance."""
    values = {0: "error", 1: "pending", 2: "server_ack", 3: "delivered", 4: "read", 5: "played"}
    names = {
        "ERROR": "error",
        "PENDING": "pending",
        "SERVER_ACK": "server_ack",
        "DELIVERY_ACK": "delivered",
        "READ": "read",
        "PLAYED": "played",
    }
    return values.get(status, "unknown") if isinstance(status, int) else names.get(str(status).upper(), "unknown")


class Service:
    """Execute validated workflow tools against explicit connection profiles."""

    def __init__(
        self,
        store: ConfigStore | None = None,
        state: Path | None = None,
        mode: Mode = "admin",
        definitions: tuple[ToolDefinition, ...] = TOOLS,
        default_profile: str = "",
    ) -> None:
        """Bind configuration and capabilities for a CLI invocation or MCP server lifetime."""
        self.store = store if store is not None else ConfigStore()
        self.state = state if state is not None else state_directory()
        self.mode: Mode = mode
        self.default_profile = default_profile
        self.definitions = {definition.name: definition for definition in definitions}
        self.catalog = Catalog()

    def available_tools(self) -> list[ToolDefinition]:
        """Filter discovery by the same capabilities enforced during execution."""
        return [
            definition for definition in self.definitions.values() if ACCESS[definition.access] <= ACCESS[self.mode]
        ]

    def invoke(self, name: str, arguments: dict[str, Any], max_output_bytes: int = 65_536) -> Envelope:
        """Validate once and return one predictable envelope, including actionable execution errors."""
        try:
            if name not in self.definitions:
                raise EvoError("TOOL_NOT_FOUND", "Unknown operation.")
            definition = self.definitions[name]
            if ACCESS[definition.access] > ACCESS[self.mode]:
                raise EvoError("CAPABILITY_DENIED", f"This tool requires {definition.access} access.")
            if "profile" in definition.input_model.model_fields and not arguments.get("profile"):
                arguments = {**arguments, "profile": self.default_profile}
            parameters = definition.input_model.model_validate(arguments)
            data = getattr(self, name)(parameters)
            result = Envelope(ok=True, data=data)
            if name == "chats_search" and data["partial"]:
                result = Envelope(
                    ok=False,
                    data=data,
                    error=EvoError(
                        "SEARCH_PARTIAL",
                        "One or more recipient sources failed; available matches are in data.chats.",
                        "Inspect data.sources. Follow next_cursor for remaining scans; "
                        "start a new search to retry failures.",
                    ).as_dict(),
                )
            if len(result.model_dump_json().encode()) > max_output_bytes:
                raise EvoError(
                    "OUTPUT_LIMIT",
                    "The operation completed but its result exceeds the output budget.",
                    "Narrow read queries or use CLI --output-file. Do not retry mutations with a new request ID.",
                )
            return result
        except EvoError as error:
            receipt = {"request_id": arguments["request_id"]} if arguments.get("request_id") else None
            return Envelope(ok=False, data=receipt, error=error.as_dict())
        except ValidationError as error:
            fields = sorted({".".join(str(part) for part in item["loc"]) for item in error.errors(include_input=False)})
            return Envelope(
                ok=False, error=EvoError("INVALID_INPUT", "Invalid input fields: " + ", ".join(fields)).as_dict()
            )
        except (OSError, ValueError, KeyError, TypeError):
            return Envelope(
                ok=False,
                error=EvoError(
                    "INVALID_RESPONSE",
                    "The operation returned an unexpected shape or local state could not be accessed.",
                    "Check the API version, local permissions, and profile configuration.",
                ).as_dict(),
            )

    def transport(self, profile: str) -> Transport:
        """Resolve the named deployment separately for every operation."""
        name, settings = self.store.resolve(profile)
        return Transport(name, settings, self.state)

    def scope(self, transport: Transport) -> str:
        """Bind idempotency to the actual configured target, not an ephemeral SSH socket."""
        target = [
            transport.name,
            transport.profile.transport,
            transport.profile.ssh_host,
            transport.profile.api_url,
            transport.profile.instance,
        ]
        return hashlib.sha256(json.dumps(target).encode()).hexdigest()

    def request(
        self,
        transport: Transport,
        operation: str,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> Any:
        """Run an internal workflow step through the same catalog and transport."""
        request = self.catalog.prepare(operation, transport.profile.instance, body, {}, query or {}, self.mode)
        return transport.call("request", request=request)["data"]

    def remotes_list(self, _: EmptyInput) -> dict[str, object]:
        """List configured profiles without contacting their hosts."""
        return self.store.listing()

    def remote_add(self, arguments: ProfileAdd) -> dict[str, object]:
        """Save a validated connection profile."""
        return self.store.add(arguments.name, arguments.settings, arguments.replace)

    def remote_connect(self, arguments: ProfileInput) -> dict[str, Any]:
        """Prepare remote execution over existing SSH access."""
        return self.transport(arguments.profile).connect()

    def remote_key_setup(self, arguments: ProfileInput) -> dict[str, Any]:
        """Install and verify permanent key access through the saved SSH profile."""
        return self.transport(arguments.profile).key_setup(self.store)

    def remote_disconnect(self, arguments: ProfileInput) -> dict[str, Any]:
        """Close only the application's owned SSH master."""
        return self.transport(arguments.profile).disconnect()

    def status(self, arguments: ProfileInput) -> dict[str, Any]:
        """Keep SSH failure separate from unreachable API or unpaired WhatsApp."""
        transport = self.transport(arguments.profile)
        try:
            report = transport.call("health")
            return {
                "profile": transport.name,
                "ssh": "connected" if transport.profile.transport == "ssh" else "not_used",
                **report,
            }
        except EvoError as error:
            return {
                "profile": transport.name,
                "ssh": "unavailable" if transport.profile.transport == "ssh" else "not_used",
                "ready": False,
                "connection_error": error.as_dict(),
            }

    def services_manage(self, arguments: ServicesOperation) -> dict[str, Any]:
        """Start or restart the explicitly configured deployment."""
        return self.transport(arguments.profile).call("services", operation=arguments.operation)

    def contacts_search(self, arguments: ContactsSearch) -> dict[str, Any]:
        """Scan bounded upstream pages and expose an honest continuation when incomplete."""
        transport = self.transport(arguments.profile)
        matches = []
        scanned = 0
        exhausted = False
        contact_names = ContactNames(self.state).read(self.scope(transport))
        start_page, start_row = map(int, arguments.cursor.split(":")) if arguments.cursor else (arguments.page, 0)
        next_cursor = None
        for page in range(start_page, start_page + arguments.scan_pages):
            records = self.request(transport, "chat.find_contacts", {"where": {}, "offset": 100, "page": page})
            offset = start_row if page == start_page else 0
            for row, record in enumerate(records[offset:], start=offset):
                scanned += 1
                match = recipient(record, "contacts", arguments.query, contact_names)
                if match and match["matched"]:
                    matches.append({key: match[key] for key in ("jid", "name", "kind")})
                if len(matches) == arguments.limit:
                    next_cursor = f"{page}:{row + 1}" if row + 1 < len(records) else f"{page + 1}:0"
                    exhausted = row + 1 == len(records) and len(records) < 100
                    break
            if len(matches) == arguments.limit:
                break
            if len(records) < 100:
                exhausted = True
                break
            next_cursor = f"{page + 1}:0"
        return {
            "contacts": matches,
            "scanned": scanned,
            "matches_in_scan": len(matches),
            "complete": exhausted,
            "next_cursor": None if exhausted else next_cursor,
        }

    def contacts_name(self, arguments: ContactName) -> dict[str, Any]:
        """Store the operator's exact mapping locally; never infer a name-to-number association."""
        transport = self.transport(arguments.profile)
        row = recipient({"remoteJid": normalize_jid(arguments.jid)}, "contacts", "", {})
        if row is None:
            raise EvoError("INVALID_RECIPIENT", "Saved names require an exact person or group JID.")
        ContactNames(self.state).set(self.scope(transport), row["jid"], arguments.name)
        return {"jid": row["jid"], "name": arguments.name, "saved": bool(arguments.name)}

    def chats_search(self, arguments: ChatsSearch) -> dict[str, Any]:
        """Search all recipient sources without sending messages or read receipts."""
        transport = self.transport(arguments.profile)
        scope = self.scope(transport)
        contact_names = ContactNames(self.state).read(scope)
        group_transport = Transport(
            transport.name, transport.profile.model_copy(update={"timeout": arguments.group_timeout}), self.state
        )
        search = ConversationSearch(
            lambda operation, body, query: self.request(
                group_transport if operation == "group.fetch_all_groups" else transport, operation, body, query
            ),
            contact_names,
        )
        # A changed local name must not silently mix with a cursor's previously cached names.
        binding_scope = scope + json.dumps(contact_names, sort_keys=True) if contact_names else scope
        return SearchCache(self.state).run(binding_scope, arguments, search.advance)

    def chats_list(self, arguments: ChatsList) -> dict[str, Any]:
        """Use the chat endpoint's take/skip pagination and return concise identifiers."""
        transport = self.transport(arguments.profile)
        contact_names = ContactNames(self.state).read(self.scope(transport))
        records = self.request(
            transport, "chat.find_chats", {"where": {}, "take": arguments.limit, "skip": arguments.offset}
        )
        chats = []
        for record in records:
            row = recipient(record, "chats", "", contact_names)
            if row is None:
                continue
            chats.append(
                {
                    "jid": row["jid"],
                    "name": row["name"],
                    "unread": record.get("unreadMessages", 0),
                }
            )
        return {
            "chats": chats,
            "next_offset": arguments.offset + arguments.limit if len(records) == arguments.limit else None,
        }

    def messages_read(self, arguments: MessagesRead) -> dict[str, Any]:
        """Read bounded history with exact message and chat IDs for subsequent actions."""
        transport = self.transport(arguments.profile)
        where: dict[str, Any] = {"key": {"remoteJid": normalize_jid(arguments.chat)}}
        if arguments.since:
            since = datetime.fromisoformat(arguments.since.replace("Z", "+00:00"))
            if since.tzinfo is None:
                raise EvoError("INVALID_TIMESTAMP", "The since timestamp needs a timezone.")
            where["messageTimestamp"] = {"gte": since.isoformat(), "lte": datetime.now(UTC).isoformat()}
        response = self.request(
            transport, "chat.find_messages", {"where": where, "offset": arguments.limit, "page": arguments.page}
        )
        page = response["messages"]
        messages = []
        for record in page["records"]:
            content = record["message"]
            text = content.get("conversation", "")
            if not text:
                for field in ("extendedTextMessage", "imageMessage", "videoMessage", "documentMessage"):
                    if field in content:
                        text = content[field].get("text") or content[field].get("caption") or ""
                        break
            messages.append(
                {
                    "id": record["key"]["id"],
                    "chat": record["key"]["remoteJid"],
                    "from_me": record["key"]["fromMe"],
                    "timestamp": record["messageTimestamp"],
                    "type": record["messageType"],
                    "text": text,
                    "receipts": [item["status"] for item in record.get("MessageUpdate", [])],
                }
            )
        return {
            "messages": messages,
            "total": page["total"],
            "page": page["currentPage"],
            "next_page": arguments.page + 1 if arguments.page < page["pages"] else None,
        }

    def message_send(self, arguments: MessageSend) -> dict[str, Any]:
        """Reserve before sending and return a receipt that cannot be mistaken for delivery."""
        transport = self.transport(arguments.profile)
        recipient = normalize_jid(arguments.to)
        if not arguments.text.strip():
            raise EvoError("EMPTY_MESSAGE", "Message text cannot be blank.")
        payload = {"number": recipient, "text": arguments.text}
        request = self.catalog.prepare("message.send_text", transport.profile.instance, payload, {}, {}, self.mode)
        if arguments.dry_run:
            return {
                "dry_run": True,
                "profile": transport.name,
                "to": recipient,
                "text": arguments.text,
                "request_id": arguments.request_id,
            }
        ledger = SendLedger(self.state)
        scope = self.scope(transport)
        previous = ledger.reserve(scope, arguments.request_id, {"operation": "message.send_text", "body": payload})
        if previous is not None:
            return {**previous, "duplicate_suppressed": True}
        response = transport.call("request", request=request)["data"]
        status = normalized_status(response["status"])
        receipt = {
            "message_id": response["key"]["id"],
            "to": recipient,
            "request_id": arguments.request_id,
            "accepted": True,
            "status": status,
            "upstream_status": response["status"],
            "delivery_confirmed": status in {"delivered", "read", "played"},
            "duplicate_suppressed": False,
        }
        ledger.accepted(scope, arguments.request_id, receipt)
        return receipt

    def message_status(self, arguments: MessageStatus) -> dict[str, Any]:
        """Combine independent message and receipt evidence without inferring delivery from storage."""
        transport = self.transport(arguments.profile)
        updates = self.request(
            transport, "chat.find_status_message", {"where": {"id": arguments.message_id}, "offset": 100, "page": 1}
        )
        history = self.request(
            transport, "chat.find_messages", {"where": {"key": {"id": arguments.message_id}}, "offset": 1, "page": 1}
        )
        records = history["messages"]["records"]
        statuses = [item["status"] for item in updates]
        for record in records:
            statuses.extend(item["status"] for item in record.get("MessageUpdate", []))
        normalized = {normalized_status(status) for status in statuses}
        rank = {"unknown": -1, "pending": 0, "error": 1, "server_ack": 2, "delivered": 3, "read": 4, "played": 5}
        state = max(normalized, key=lambda value: rank[value]) if normalized else "pending" if records else "unknown"
        return {
            "message_id": arguments.message_id,
            "status": state,
            "message_present": bool(records),
            "observed_statuses": sorted({str(status) for status in statuses}),
            "delivery_confirmed": state in {"delivered", "read", "played"},
            "evidence": "receipt_records"
            if statuses
            else "stored_message_without_receipt"
            if records
            else "no_records",
        }

    def request_status(self, arguments: RequestStatus) -> dict[str, Any]:
        """Return a durable local receipt without making a second network mutation."""
        transport = self.transport(arguments.profile)
        return SendLedger(self.state).inspect(self.scope(transport), arguments.request_id)

    def instance_pair(self, arguments: ProfileInput) -> dict[str, Any]:
        """Save a fresh QR in private local state, leaving phone authentication to the user."""
        transport = self.transport(arguments.profile)
        connection = self.request(transport, "instance.connection_state")
        if connection["instance"]["state"] == "open":
            return {"profile": transport.name, "instance": transport.profile.instance, "state": "open", "path": ""}
        response = self.request(transport, "instance.connect")
        if "base64" not in response:
            raise EvoError(
                "QR_NOT_READY", "Evolution has not produced a QR yet.", "Check status, then retry pairing.", True
            )
        encoded = response["base64"].split(",", 1)[-1]
        image = base64.b64decode(encoded, validate=True)
        if not image.startswith(b"\x89PNG\r\n\x1a\n"):
            raise EvoError("INVALID_QR", "Evolution returned an invalid QR image.")
        directory = private_directory(self.state / "pairing")
        descriptor, name = tempfile.mkstemp(prefix=transport.name + "-", suffix=".png", dir=directory)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(image)
        return {
            "profile": transport.name,
            "instance": transport.profile.instance,
            "state": "pairing_required",
            "path": name,
        }

    def manager_url(self, arguments: ProfileInput) -> dict[str, Any]:
        """Return a usable GUI address without exposing the remote API publicly."""
        return self.transport(arguments.profile).manager_url()

    def api_search(self, arguments: CatalogSearch) -> dict[str, Any]:
        """Discover a bounded set of operations allowed by this server's capability mode."""
        return self.catalog.search(arguments.query, arguments.limit, arguments.offset, self.mode)

    def api_schema(self, arguments: ApiSchema) -> dict[str, Any]:
        """Return the exact compiled contract and source location for an operation."""
        operation = self.catalog.operation(arguments.operation)
        if ACCESS[operation["access"]] > ACCESS[self.mode]:
            raise EvoError("CAPABILITY_DENIED", "The operation is outside this server's capability mode.")
        return operation

    def api_read(self, arguments: ApiCall) -> dict[str, Any]:
        """Execute only semantically read-only operations, including lookup POSTs."""
        return self.api_execute(arguments, "read")

    def api_write(self, arguments: ApiCall) -> dict[str, Any]:
        """Execute a write-level catalog operation with duplicate suppression."""
        return self.api_execute(arguments, "write")

    def api_admin(self, arguments: ApiCall) -> dict[str, Any]:
        """Execute administrative operations through the same validation and ledger."""
        return self.api_execute(arguments, "admin")

    def api_execute(self, arguments: ApiCall, required_access: Mode) -> dict[str, Any]:
        """Keep read and mutation surfaces distinct even when the HTTP verb is misleading."""
        operation = self.catalog.operation(arguments.operation)
        if operation["access"] != required_access:
            raise EvoError("WRONG_API_TOOL", f"Use api_{operation['access']} for this operation.")
        transport = self.transport(arguments.profile)
        request = self.catalog.prepare(
            arguments.operation,
            transport.profile.instance,
            arguments.body,
            arguments.path_parameters,
            arguments.query,
            self.mode,
            arguments.max_bytes,
        )
        request["authorization_env"] = arguments.authorization_env
        if arguments.dry_run:
            return {
                "dry_run": True,
                "profile": transport.name,
                "operation": arguments.operation,
                "request": redact(request),
            }
        if required_access == "read":
            return transport.call("request", request=request)
        if not arguments.request_id:
            raise EvoError("REQUEST_ID_REQUIRED", "Mutations require a stable request_id.")
        ledger = SendLedger(self.state)
        scope = self.scope(transport)
        previous = ledger.reserve(scope, arguments.request_id, {"operation": arguments.operation, "request": request})
        if previous is not None:
            return {**previous, "duplicate_suppressed": True}
        response = transport.call("request", request=request)
        result = {"operation": arguments.operation, "request_id": arguments.request_id, **response}
        ledger.accepted(scope, arguments.request_id, result)
        return result
