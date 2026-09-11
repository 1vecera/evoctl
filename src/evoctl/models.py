"""Shared tool input and result schemas for CLI and MCP."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from evoctl.config import Profile


class Input(BaseModel):
    """Reject misspelled fields instead of silently dropping agent input."""

    model_config = ConfigDict(extra="forbid")


class EmptyInput(Input):
    """No arguments are needed for profile discovery."""


class ProfileInput(Input):
    """Select a named deployment or its configured default."""

    profile: str = Field(default="", description="Saved profile name; empty selects the configured default.")


class ProfileAdd(Input):
    """Persist connection settings without accepting literal API keys."""

    name: str = Field(description="New profile name.")
    settings: Profile = Field(description="Connection settings; credentials are environment/container references.")
    replace: bool = Field(default=False, description="Replace an existing profile with these settings.")


class ContactsSearch(ProfileInput):
    """Bound contact search by both returned rows and remote pages scanned."""

    query: str = Field(default="", max_length=200, description="Case- and accent-insensitive name or JID substring.")
    limit: int = Field(default=20, ge=1, le=100, description="Maximum returned contacts.")
    page: int = Field(default=1, ge=1, description="First upstream page to scan.")
    cursor: str = Field(
        default="", pattern=r"^$|^[1-9][0-9]*:[0-9]{1,2}$", description="Continuation from next_cursor."
    )
    scan_pages: int = Field(
        default=5, ge=1, le=20, description="Bounded search budget; each upstream page has 100 contacts."
    )


class ContactName(ProfileInput):
    """Save an operator-confirmed name for an exact recipient without changing WhatsApp."""

    jid: str = Field(min_length=5, max_length=200, description="Exact person/group JID or international digits.")
    name: str = Field(
        max_length=200,
        description="Confirmed local display name. An explicit empty string removes the saved name.",
    )

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        """Reject accidental blank names while reserving the explicit empty string for removal."""
        if value and not value.strip():
            raise ValueError("Use a nonblank name, or an empty string to remove the saved name.")
        return value.strip()


class ContactsImport(ProfileInput):
    """Import an exported address book into local phone-to-name storage without cloud access."""

    csv_text: str = Field(min_length=1, max_length=4_194_304, description="Outlook or Google Contacts CSV text.")
    region: str = Field(
        default="",
        pattern=r"^$|^[A-Za-z]{2}$",
        description="Explicit two-letter country for national phone numbers, e.g. CZ. Empty requires + or 00.",
    )
    name_columns: list[str] = Field(
        default_factory=list, max_length=10, description="Optional exact CSV headers to join into a full name."
    )
    phone_columns: list[str] = Field(
        default_factory=list, max_length=50, description="Optional exact CSV headers containing phone numbers."
    )
    replace: bool = Field(default=False, description="Allow replacement of conflicting existing local names.")
    dry_run: bool = Field(default=False, description="Preview counts and issues without changing local storage.")


class ContactsList(ProfileInput):
    """Search the saved local address book offline without claiming WhatsApp registration."""

    query: str = Field(default="", max_length=200, description="Case/accent-insensitive local name, phone or JID.")
    limit: int = Field(default=20, ge=1, le=100, description="Maximum returned local phone/name entries.")
    offset: int = Field(default=0, ge=0, description="Number of matching local entries to skip.")


class ChatsSearch(ProfileInput):
    """Find people and groups with bounded scans and a shared, replayable continuation."""

    query: str = Field(
        default="", max_length=200, description="Case/accent-insensitive name, subject, JID or phone substring."
    )
    kind: Literal["all", "person", "group"] = Field(
        default="all", description="Search both people and groups by default."
    )
    limit: int = Field(default=20, ge=1, le=100, description="Maximum returned conversations per call.")
    scan_pages: int = Field(
        default=5, ge=1, le=20, description="At most this many 100-row pages per paginated source per call."
    )
    group_timeout: float = Field(
        default=120.0,
        gt=0,
        le=120,
        description="Seconds allowed for the single bulk group request (upstream also fetches pictures). "
        "Contact/chat requests retain the profile timeout.",
    )
    cursor: str = Field(
        default="",
        pattern=r"^$|^[a-f0-9]{32}:[1-9][0-9]{0,5}$",
        description="Opaque next_cursor; reuse all search arguments, plus the same profile and state directory. "
        "Expires after 30 minutes.",
    )


class ChatsList(ProfileInput):
    """Read a page of conversations without changing their read state."""

    limit: int = Field(default=20, ge=1, le=100, description="Maximum returned chats.")
    offset: int = Field(default=0, ge=0, description="Number of chats to skip.")


class MessagesRead(ProfileInput):
    """Read one exact conversation with explicit bounds."""

    chat: str = Field(
        min_length=5,
        max_length=200,
        description="Exact JID or international digits; resolve names via chats_search.",
    )
    limit: int = Field(default=20, ge=1, le=100, description="Maximum returned messages.")
    page: int = Field(default=1, ge=1, description="History page, newest messages first.")
    since: str = Field(default="", description="Optional ISO 8601 lower time bound including a timezone.")


class MessageSend(ProfileInput):
    """Send exact reviewed text with a durable request identifier."""

    to: str = Field(
        min_length=5, max_length=200, description="Exact recipient JID or international digits, never a display name."
    )
    text: str = Field(min_length=1, max_length=65536, description="Exact message text to send.")
    request_id: str = Field(
        min_length=1, max_length=120, description="Stable idempotency key, reused for the same logical send."
    )
    dry_run: bool = Field(default=False, description="Return a preview without reserving a request ID or sending.")


class MessageStatus(ProfileInput):
    """Look up receipt evidence using a WhatsApp message ID."""

    message_id: str = Field(min_length=1, max_length=200, description="Message ID returned by message_send.")


class RequestStatus(ProfileInput):
    """Recover a local idempotency receipt without contacting WhatsApp."""

    request_id: str = Field(min_length=1, max_length=120, description="Idempotency key used for the original send.")


class CatalogSearch(Input):
    """Discover API operations within a bounded page."""

    query: str = Field(default="", max_length=200, description="Words from an operation ID or API route.")
    limit: int = Field(default=20, ge=1, le=100, description="Maximum returned operation summaries.")
    offset: int = Field(default=0, ge=0, description="Offset from the previous page's next_offset.")


class ApiSchema(Input):
    """Inspect one operation's method, path parameters, request body, and access level."""

    operation: str = Field(description="Exact operation ID returned by api_search.")


class ApiCall(ProfileInput):
    """Call a catalog operation with structured JSON rather than shell fragments."""

    operation: str = Field(description="Exact catalog operation ID; see api_schema for its contract.")
    body: dict[str, Any] | None = Field(
        default=None, description='JSON body; secret values can use {"$env": "VARIABLE_NAME"} on the API host.'
    )
    path_parameters: dict[str, str] = Field(
        default_factory=dict, description="Named path values; instanceName defaults to the profile."
    )
    query: dict[str, Any] = Field(default_factory=dict, description="URL query parameters, encoded by the client.")
    authorization_env: str = Field(
        default="",
        pattern=r"^$|^[A-Za-z_][A-Za-z0-9_]*$",
        description="API-host environment variable containing an Authorization header, such as metrics Basic auth.",
    )
    request_id: str = Field(default="", max_length=120, description="Required stable idempotency key for mutations.")
    dry_run: bool = Field(default=False, description="Preview a validated request without executing it.")
    max_bytes: int = Field(default=1_048_576, ge=1024, le=16_777_216, description="Hard upstream response byte limit.")


class ServicesOperation(ProfileInput):
    """Manage only the existing containers selected by a profile."""

    operation: Literal["start", "restart"] = Field(
        default="start", description="Start or restart existing deployment containers."
    )


class Envelope(BaseModel):
    """Stable JSON result shared by CLI stdout and MCP structuredContent."""

    ok: bool
    data: Any = None
    error: dict[str, Any] | None = None


class PairResult(BaseModel):
    """Reference an ephemeral local QR image without embedding base64 in text output."""

    profile: str
    instance: str
    path: str
    state: str
