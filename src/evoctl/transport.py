"""Direct execution and owned OpenSSH connections for the portable worker."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from evoctl import worker
from evoctl.config import ConfigStore, Profile, private_directory, state_directory
from evoctl.worker import EvoError


def unwrap(result: dict[str, Any]) -> Any:
    """Convert the worker's stable failure envelope into a shared service error."""
    if not result["ok"]:
        raise EvoError(**result["error"])
    return result["data"]


class Transport:
    """Dispatch the same worker protocol locally or over a verified SSH connection."""

    def __init__(self, name: str, profile: Profile, state: Path | None = None) -> None:
        """Bind a named profile without resolving its credentials on the caller."""
        self.name = name
        self.profile = profile
        self.state = state if state is not None else state_directory()
        self.source = Path(worker.__file__).read_bytes()
        self.worker_hash = hashlib.sha256(self.source).hexdigest()[:16]
        self.remote_path = f".local/share/evoctl/workers/{self.worker_hash}.py"

    def control_path(self) -> Path:
        """Use a private, predictable socket for only this application's SSH master."""
        digest = hashlib.sha256(self.profile.ssh_host.encode()).hexdigest()[:16]
        directory = private_directory(self.state / "ssh")
        path = directory / digest
        if len(str(path).encode()) > 100:
            raise EvoError(
                "SSH_PATH_TOO_LONG", "SSH socket path is too long.", "Set EVOCTL_STATE_DIR to a shorter path."
            )
        return path

    def ssh_arguments(self, interactive: bool = False, fresh: bool = False, identity: str = "") -> list[str]:
        """Construct arguments without a shell; preserve host keys and SSH config aliases."""
        if self.profile.transport != "ssh":
            raise EvoError("SSH_REQUIRED", "This operation requires an SSH profile.")
        if os.name != "posix":
            raise EvoError("UNSUPPORTED_PLATFORM", "SSH management currently requires Linux or macOS.")
        arguments = [
            "ssh",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=2",
            "-o",
            f"ControlMaster={'no' if fresh else 'auto'}",
            "-o",
            f"ControlPath={'none' if fresh else self.control_path()}",
        ]
        if not fresh:
            arguments += ["-o", "ControlPersist=10m"]
        if not interactive:
            arguments += ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes"]
        key = identity or self.profile.identity_file
        if key:
            arguments += ["-i", str(Path(key).expanduser()), "-o", "IdentitiesOnly=yes"]
        return arguments

    def run_ssh(
        self, command: str, input_text: str = "", timeout: float = 40, fresh: bool = False, identity: str = ""
    ) -> str:
        """Run a fixed remote command and translate SSH failures without exposing stderr."""
        arguments = self.ssh_arguments(fresh=fresh, identity=identity)
        arguments += ["-T", self.profile.ssh_host, command]
        try:
            result = subprocess.run(
                arguments, input=input_text, capture_output=True, text=True, timeout=timeout, check=False
            )
        except FileNotFoundError:
            raise EvoError("SSH_MISSING", "OpenSSH is not installed.", "Install the OpenSSH client.") from None
        except subprocess.TimeoutExpired:
            raise EvoError(
                "SSH_TIMEOUT",
                "The remote operation timed out.",
                "A mutation may have run; check its status before retrying.",
            ) from None
        if result.returncode:
            stderr = result.stderr.lower()
            if "permission denied" in stderr:
                code, message = "SSH_AUTH_REQUIRED", "SSH authentication failed."
                hint = f"Run evoctl remote login {self.name}, then evoctl remote key-setup {self.name}."
            elif "host key verification failed" in stderr or "host identification has changed" in stderr:
                code, message = "SSH_HOST_KEY", "SSH host-key verification failed."
                hint = f"Verify the server identity through evoctl remote login {self.name}."
            elif "could not resolve hostname" in stderr:
                code, message = "SSH_DNS", "The SSH hostname could not be resolved."
                hint = (
                    "Check the SSH alias. Local .local names need the same LAN; use a reachable VPN/DNS name remotely."
                )
            elif "uv: " in stderr or "uv: command not found" in stderr:
                code, message = "REMOTE_UV_MISSING", "uv is not available on the remote host."
                hint = "Install uv on the remote host, then reconnect."
            elif "no such file" in stderr or "failed to open file" in stderr:
                code, message = "REMOTE_WORKER_MISSING", "The remote worker is not installed."
                hint = f"Run evoctl remote connect {self.name}."
            else:
                code, message = "SSH_FAILED", f"The remote command failed with exit code {result.returncode}."
                hint = f"Run evoctl remote login {self.name} to inspect the connection."
            raise EvoError(code, message, hint)
        return result.stdout

    def call(self, action: str, **arguments: Any) -> Any:
        """Keep profile settings explicit for every worker invocation."""
        message = {
            "protocol": worker.PROTOCOL_VERSION,
            "action": action,
            "profile": self.profile.model_dump(),
            **arguments,
        }
        if self.profile.transport == "http":
            return unwrap(worker.execute(message))
        command = (
            'PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH" '
            f'uv run --no-project "$HOME/{self.remote_path}"'
        )
        raw = self.run_ssh(
            command, json.dumps(message), timeout=240 if action == "services" else self.profile.timeout + 25
        )
        try:
            return unwrap(json.loads(raw))
        except (json.JSONDecodeError, KeyError, TypeError):
            raise EvoError("REMOTE_PROTOCOL_ERROR", "The remote worker returned an invalid response.") from None

    def connect(self) -> dict[str, Any]:
        """Install this worker version atomically after successful SSH authentication."""
        command = (
            'umask 077; mkdir -p "$HOME/.local/share/evoctl/workers" && '
            f'cat > "$HOME/{self.remote_path}.tmp" && '
            f'mv "$HOME/{self.remote_path}.tmp" "$HOME/{self.remote_path}"'
        )
        self.run_ssh(command, self.source.decode())
        return {"profile": self.name, "ssh": "connected", "worker": self.call("ping")}

    def login(self) -> None:
        """Let the user handle host trust and password prompts directly in their terminal."""
        if not os.isatty(0):
            raise EvoError("TERMINAL_REQUIRED", "SSH login requires an interactive terminal.")
        arguments = [*self.ssh_arguments(interactive=True), "-t", self.profile.ssh_host]
        result = subprocess.run(arguments, check=False)
        if result.returncode:
            raise EvoError("SSH_LOGIN_FAILED", "The interactive SSH session did not complete successfully.")

    def key_setup(self, store: ConfigStore) -> dict[str, Any]:
        """Install a dedicated public key using existing access and verify a fresh key-only connection."""
        directory = private_directory(self.state / "keys")
        key = directory / f"{self.name}_ed25519"
        if not key.exists():
            result = subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"evoctl:{self.name}", "-f", str(key)],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode:
                raise EvoError("KEY_GENERATION_FAILED", "Could not create an Ed25519 key.")
        public_key = key.with_name(key.name + ".pub").read_text()
        command = (
            'umask 077; mkdir -p "$HOME/.ssh" && touch "$HOME/.ssh/authorized_keys" && '
            'chmod 700 "$HOME/.ssh" && chmod 600 "$HOME/.ssh/authorized_keys" && '
            "IFS= read -r evo_public_key; "
            'grep -qxF -- "$evo_public_key" "$HOME/.ssh/authorized_keys" || '
            'printf "%s\\n" "$evo_public_key" >> "$HOME/.ssh/authorized_keys"'
        )
        self.run_ssh(command, public_key)
        self.run_ssh("true", fresh=True, identity=str(key))
        profile = self.profile.model_copy(update={"identity_file": str(key)})
        store.add(self.name, profile, replace=True)
        return {"profile": self.name, "key_installed": True, "fresh_connection_verified": True}

    def manager_url(self) -> dict[str, Any]:
        """Expose the GUI on loopback using an owned SSH forward, without publishing a port."""
        if self.profile.transport == "http":
            return {"url": self.profile.api_url + "/manager/", "tunneled": False}
        self.call("ping")
        origin = urlsplit(self.profile.api_url)
        if origin.scheme != "http":
            raise EvoError("GUI_TUNNEL_UNSUPPORTED", "The GUI tunnel requires a remote loopback HTTP origin.")
        if origin.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise EvoError("GUI_TUNNEL_UNSUPPORTED", "The GUI tunnel only forwards the configured remote loopback API.")
        target_host = f"[{origin.hostname}]" if ":" in str(origin.hostname) else str(origin.hostname)
        for _ in range(3):
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            forward = f"127.0.0.1:{port}:{target_host}:{origin.port or 80}"
            result = subprocess.run(
                [*self.ssh_arguments(), "-O", "forward", "-L", forward, self.profile.ssh_host],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if result.returncode == 0:
                return {"url": f"http://127.0.0.1:{port}/manager/", "tunneled": True, "profile": self.name}
        raise EvoError("SSH_FORWARD_FAILED", "Could not open a loopback GUI forward.", "Reconnect the remote first.")

    def disconnect(self) -> dict[str, Any]:
        """Close only evoctl's own SSH master and its forwards."""
        result = subprocess.run(
            [*self.ssh_arguments(), "-O", "exit", self.profile.ssh_host],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if result.returncode and self.control_path().exists():
            raise EvoError("SSH_DISCONNECT_FAILED", "Could not close the owned SSH connection.")
        return {"profile": self.name, "disconnected": True}
