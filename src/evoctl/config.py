"""Validated, credential-free named connection profiles."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from evoctl.worker import EvoError


class Profile(BaseModel):
    """All settings required to address one deployment; secrets are references only."""

    model_config = ConfigDict(extra="forbid")
    transport: Literal["http", "ssh"] = "http"
    api_url: str = "http://127.0.0.1:8080"
    instance: str = Field(default="Default", min_length=1, max_length=200)
    key_env: str = "EVOLUTION_API_KEY"
    api_container: str = ""
    service_containers: list[str] = Field(default_factory=list)
    runtime: Literal["external", "colima"] = "external"
    ssh_host: str = ""
    identity_file: str = ""
    timeout: float = Field(default=20, gt=0, le=120)
    allow_http: bool = False

    @field_validator("api_url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        """Limit credentials to an explicit HTTP origin without userinfo or query strings."""
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            raise ValueError("Use an http(s) origin without embedded credentials.")
        if parts.query or parts.fragment or parts.path not in {"", "/"}:
            raise ValueError("Use an API origin without a path, query, or fragment.")
        return value.rstrip("/")

    @field_validator("key_env")
    @classmethod
    def valid_env(cls, value: str) -> str:
        """Accept a variable name, never a literal credential."""
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise ValueError("key_env must be an environment variable name.")
        return value

    @field_validator("ssh_host")
    @classmethod
    def valid_host(cls, value: str) -> str:
        """Prevent SSH option injection while accepting aliases and user@host targets."""
        if value and (value.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.@:\[\]-]+", value)):
            raise ValueError("Use an SSH alias or user@hostname, without options or whitespace.")
        return value

    @field_validator("api_container")
    @classmethod
    def valid_container(cls, value: str) -> str:
        """Reject executable options in container names."""
        if value and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
            raise ValueError("Invalid Docker container name.")
        return value

    @model_validator(mode="after")
    def valid_transport(self) -> Profile:
        """Require SSH identity and encrypted or explicitly local API transport."""
        if self.transport == "ssh" and not self.ssh_host:
            raise ValueError("SSH profiles need ssh_host.")
        parts = urlsplit(self.api_url)
        if parts.scheme == "http" and parts.hostname not in {"localhost", "127.0.0.1", "::1"} and not self.allow_http:
            raise ValueError("Use HTTPS, loopback HTTP, or explicitly set allow_http for a trusted network.")
        return self


class Configuration(BaseModel):
    """Portable configuration with one optional default profile."""

    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    default: str = ""
    profiles: dict[str, Profile] = Field(default_factory=dict)


def config_directory() -> Path:
    """Locate user configuration without relying on a repository working directory."""
    if "EVOCTL_CONFIG_DIR" in os.environ:
        return Path(os.environ["EVOCTL_CONFIG_DIR"]).expanduser()
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "evoctl"


def state_directory() -> Path:
    """Keep private operational state outside project files."""
    if "EVOCTL_STATE_DIR" in os.environ:
        return Path(os.environ["EVOCTL_STATE_DIR"]).expanduser()
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "evoctl"


def private_directory(path: Path) -> Path:
    """Create owner-only directories for configuration, sockets, keys, and send receipts."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def atomic_write(path: Path, content: str) -> None:
    """Replace a configuration file atomically with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(prefix=".evoctl-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class ConfigStore:
    """Read and update named profiles without storing secret values."""

    def __init__(self, directory: Path | None = None) -> None:
        """Bind one configuration directory for both CLI and MCP use."""
        self.directory = directory if directory is not None else config_directory()
        self.path = self.directory / "config.json"

    def load(self) -> Configuration:
        """Return an empty configuration on first use."""
        if not self.path.exists():
            return Configuration()
        return Configuration.model_validate_json(self.path.read_text())

    def save(self, configuration: Configuration) -> None:
        """Persist only validated connection settings."""
        private_directory(self.directory)
        atomic_write(self.path, configuration.model_dump_json(indent=2) + "\n")

    def add(self, name: str, profile: Profile, replace: bool = False) -> dict[str, object]:
        """Create a profile and select the first one as default."""
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}", name):
            raise EvoError("INVALID_PROFILE_NAME", "Use 1–48 letters, numbers, hyphens, or underscores.")
        configuration = self.load()
        if name in configuration.profiles and not replace:
            raise EvoError("PROFILE_EXISTS", "This profile already exists.", "Use --replace to update it.")
        configuration.profiles[name] = profile
        if not configuration.default:
            configuration.default = name
        self.save(configuration)
        return {"profile": name, "default": configuration.default == name, "settings": profile.model_dump()}

    def resolve(self, name: str = "") -> tuple[str, Profile]:
        """Resolve an explicit name or the saved default without implicit connection state."""
        configuration = self.load()
        name = name or configuration.default
        if name not in configuration.profiles:
            raise EvoError("PROFILE_NOT_FOUND", "No matching profile is configured.", "Run evoctl remote add --help.")
        return name, configuration.profiles[name]

    def listing(self) -> dict[str, object]:
        """Return all credential-free profile settings for discovery."""
        return json.loads(self.load().model_dump_json())
