"""Portable remote worker: stdlib only, one JSON request, no credential export."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

PROTOCOL_VERSION = 1


@dataclass
class EvoError(Exception):
    """A public error whose fields are safe to return to a CLI or MCP client."""

    code: str
    message: str
    hint: str = ""
    retryable: bool = False

    def as_dict(self) -> dict[str, Any]:
        """Return the stable error contract without a traceback or request payload."""
        return {"code": self.code, "message": self.message, "hint": self.hint, "retryable": self.retryable}


def redact(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    """Remove credential fields and known secret values from arbitrary API JSON."""
    match value:
        case dict():
            result = {}
            for key, item in value.items():
                normalized = re.sub(r"[^a-z0-9]", "", key.lower())
                sensitive = any(
                    part in normalized for part in ("token", "password", "secret", "credential", "apikey")
                ) or normalized in {"hash", "authorization", "creds", "privatekey", "authstate"}
                result[key] = "[REDACTED]" if sensitive else redact(item, secrets)
            return result
        case list():
            return [redact(item, secrets) for item in value]
        case str():
            for secret in secrets:
                if secret:
                    value = value.replace(secret, "[REDACTED]")
            return value
        case _:
            return value


def run_command(arguments: list[str], timeout: float = 10) -> subprocess.CompletedProcess[str]:
    """Capture a fixed executable invocation so command output cannot leak into the protocol."""
    try:
        return subprocess.run(arguments, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        raise EvoError("EXECUTABLE_MISSING", f"Required executable is unavailable: {arguments[0]}.") from None
    except subprocess.TimeoutExpired:
        raise EvoError("COMMAND_TIMEOUT", f"The {arguments[0]} operation timed out.", retryable=True) from None


def docker_inventory() -> list[dict[str, Any]]:
    """Read container metadata without inspecting environment variables or logs."""
    result = run_command(["docker", "ps", "-a", "--format", "{{json .}}"])
    if result.returncode:
        raise EvoError("DOCKER_UNAVAILABLE", "Docker is not reachable.", "Start the configured container runtime.")
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def api_container(profile: dict[str, Any], inventory: list[dict[str, Any]]) -> dict[str, Any]:
    """Resolve exactly one Evolution container; ambiguous deployments require a profile setting."""
    name = profile["api_container"]
    if name == "auto":
        matches = [item for item in inventory if "evolution-api" in item["Image"]]
    else:
        matches = [item for item in inventory if item["Names"] == name]
    if len(matches) != 1:
        raise EvoError(
            "CONTAINER_NOT_FOUND" if not matches else "CONTAINER_AMBIGUOUS",
            "The Evolution API container could not be uniquely identified.",
            "Set api_container to an existing container name; missing deployments must be restored explicitly.",
        )
    return matches[0]


def deployment(profile: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Find the API and its Compose siblings without selecting unrelated services."""
    inventory = docker_inventory()
    api = api_container(profile, inventory)
    labels = dict(part.split("=", 1) for part in api["Labels"].split(",") if "=" in part)
    project = labels.get("com.docker.compose.project", "")
    if project:
        siblings = []
        for item in inventory:
            item_labels = dict(part.split("=", 1) for part in item["Labels"].split(",") if "=" in part)
            if item_labels.get("com.docker.compose.project") == project:
                siblings.append(item)
    else:
        names = {api["Names"], *profile["service_containers"]}
        siblings = [item for item in inventory if item["Names"] in names]
    return api, siblings


def resolve_key(profile: dict[str, Any]) -> str:
    """Resolve a credential on the API host and retain it only in process memory."""
    if profile["api_container"]:
        api = api_container(profile, docker_inventory())
        result = run_command(["docker", "exec", api["Names"], "sh", "-c", 'printf %s "$AUTHENTICATION_API_KEY"'])
        key = result.stdout.strip() if result.returncode == 0 else ""
    else:
        key = os.environ.get(profile["key_env"], "")
    if not key:
        raise EvoError(
            "CREDENTIAL_MISSING",
            "The API credential is unavailable.",
            "Set the profile's environment variable on the API host, or select its API container.",
        )
    return key


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Prevent authenticated API calls from forwarding credentials to another URL."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        """Refuse redirects so configured origin is the only credential destination."""
        return None


def resolve_body(value: Any, secrets: list[str]) -> Any:
    """Expand explicit environment references on the API host without exporting their values."""
    if isinstance(value, dict):
        if set(value) == {"$env"}:
            name = value["$env"]
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise EvoError("INVALID_SECRET_REFERENCE", "Use an environment variable name in $env.")
            resolved = os.environ.get(name, "")
            if not resolved:
                raise EvoError(
                    "CREDENTIAL_MISSING", "A request body environment reference is unavailable on the API host."
                )
            secrets.append(resolved)
            return resolved
        return {key: resolve_body(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_body(item, secrets) for item in value]
    return value


def api_request(profile: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    """Perform one bounded API call; never retry a mutation or follow a redirect."""
    path = request["path"]
    if not path.startswith("/") or path.startswith("//") or ".." in path or "?" in path or "#" in path:
        raise EvoError("INVALID_PATH", "API paths must be relative to the configured origin.")
    key = resolve_key(profile) if request["authenticated"] else ""
    headers = {"Accept": "application/json"}
    if key:
        headers["apikey"] = key
    body = None
    secrets = [key]
    if request.get("authorization_env"):
        headers["Authorization"] = resolve_body({"$env": request["authorization_env"]}, secrets)
    if request["body"] is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(resolve_body(request["body"], secrets), ensure_ascii=False).encode()
    url = profile["api_url"].rstrip("/") + path
    if request["query"]:
        url += "?" + urllib.parse.urlencode(request["query"], doseq=True)
    call = urllib.request.Request(url, data=body, headers=headers, method=request["method"])
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({}))
    try:
        with opener.open(call, timeout=profile["timeout"]) as response:
            raw = response.read(request["max_bytes"] + 1)
            status = response.status
            content_type = response.headers.get_content_type()
    except urllib.error.HTTPError as error:
        codes = {401: "API_UNAUTHORIZED", 403: "API_FORBIDDEN", 404: "API_NOT_FOUND", 429: "RATE_LIMITED"}
        code = codes.get(error.code, "API_REJECTED")
        raise EvoError(
            code,
            f"Evolution API returned HTTP {error.code}.",
            "Check the instance, operation schema, and API credential.",
            error.code == 429,
        ) from None
    except (TimeoutError, urllib.error.URLError, ConnectionError):
        raise EvoError(
            "API_UNREACHABLE",
            "The API request could not be completed.",
            "Inspect status. A mutation may have been accepted; do not resend with a new idempotency key.",
            request.get("read_only", False),
        ) from None
    if len(raw) > request["max_bytes"]:
        raise EvoError("RESPONSE_TOO_LARGE", "The API response exceeds the configured byte limit.", "Narrow the query.")
    if request["sensitive_response"]:
        data = {"redacted": True, "reason": "This endpoint returns authentication material."}
    elif not raw:
        data = None
    elif "json" in content_type:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise EvoError("INVALID_API_RESPONSE", "Evolution returned invalid JSON.") from None
    else:
        data = raw.decode("utf-8", errors="replace")
    return {"http_status": status, "data": redact(data, tuple(secrets))}


def health(profile: dict[str, Any]) -> dict[str, Any]:
    """Report independent readiness stages so one missing layer does not hide the others."""
    report: dict[str, Any] = {"host": platform.system(), "runtime": "unmanaged", "containers": []}
    if profile["api_container"]:
        try:
            _, containers = deployment(profile)
            report["runtime"] = "running"
            report["containers"] = [{"name": item["Names"], "state": item["State"]} for item in containers]
        except EvoError as error:
            report["runtime"] = "unavailable"
            report["runtime_error"] = error.as_dict()
    request = {
        "method": "GET",
        "path": "/",
        "body": None,
        "query": {},
        "authenticated": False,
        "read_only": True,
        "max_bytes": 65536,
        "sensitive_response": False,
    }
    try:
        response = api_request(profile, request)
        report["api"] = "reachable"
        report["version"] = response["data"]["version"]
    except EvoError as error:
        report["api"] = "unreachable"
        report["api_error"] = error.as_dict()
    request.update(
        {
            "path": "/instance/connectionState/" + urllib.parse.quote(profile["instance"], safe=""),
            "authenticated": True,
        }
    )
    try:
        response = api_request(profile, request)
        report["authentication"] = "accepted"
        report["whatsapp"] = response["data"]["instance"]["state"]
    except EvoError as error:
        report["authentication"] = "failed" if error.code in {"CREDENTIAL_MISSING", "API_UNAUTHORIZED"} else "unknown"
        report["whatsapp"] = "unknown"
        report["connection_error"] = error.as_dict()
    report["ready"] = report["whatsapp"] == "open"
    return report


def services(profile: dict[str, Any], operation: str) -> dict[str, Any]:
    """Start or restart the existing deployment without creating containers or volumes."""
    if operation not in {"start", "restart"}:
        raise EvoError("INVALID_OPERATION", "Supported service operations are start and restart.")
    if not profile["api_container"]:
        raise EvoError("UNMANAGED_SERVICE", "This profile does not manage a Docker deployment.")
    if profile["runtime"] == "colima":
        probe = run_command(["colima", "status"])
        if probe.returncode:
            started = run_command(["colima", "start"], timeout=180)
            if started.returncode:
                raise EvoError("RUNTIME_START_FAILED", "Colima could not start.", "Inspect Colima on the remote host.")
    api, containers = deployment(profile)
    ordered = sorted(containers, key=lambda item: item["Names"] == api["Names"])
    for container in ordered:
        result = run_command(["docker", operation, container["Names"]], timeout=60)
        if result.returncode:
            raise EvoError("SERVICE_START_FAILED", f"Could not {operation} {container['Names']}.")
    return {"operation": operation, "containers": [item["Names"] for item in ordered]}


def execute(message: dict[str, Any]) -> dict[str, Any]:
    """Execute the fixed worker protocol and return only sanitized JSON."""
    try:
        if message["protocol"] != PROTOCOL_VERSION:
            raise EvoError("PROTOCOL_MISMATCH", "Reconnect the remote to update its worker.")
        action = message["action"]
        profile = message["profile"]
        if action == "ping":
            data = {"protocol": PROTOCOL_VERSION, "host": platform.system(), "uv": bool(shutil.which("uv"))}
        elif action == "request":
            data = api_request(profile, message["request"])
        elif action == "health":
            data = health(profile)
        elif action == "services":
            data = services(profile, message["operation"])
        else:
            raise EvoError("INVALID_OPERATION", "Unknown worker operation.")
        return {"ok": True, "data": data}
    except EvoError as error:
        return {"ok": False, "error": error.as_dict()}


def main() -> None:
    """Serve a single request over SSH stdin/stdout without importing third-party code."""
    try:
        message = json.load(sys.stdin)
        result = execute(message)
    except (ValueError, KeyError, TypeError, OSError):
        result = {
            "ok": False,
            "error": EvoError("INVALID_WORKER_REQUEST", "The worker request could not be processed.").as_dict(),
        }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
