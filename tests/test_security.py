"""Credential, transport, and persisted-state boundaries exercised without real secrets."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from evoctl import worker
from evoctl.config import Profile
from evoctl.worker import EvoError, api_request, redact, resolve_body


def request(path):
    """Construct a test worker request at a fixed loopback origin."""
    return {
        "method": "GET",
        "path": path,
        "authenticated": True,
        "body": None,
        "query": {},
        "max_bytes": 1024,
        "sensitive_response": False,
    }


def test_redirects_and_response_budget(protocol):
    """A server redirect never receives a second request; large responses stop at the byte budget."""
    _, state, store = protocol
    _, profile = store.resolve()
    with pytest.raises(EvoError, match="API_REJECTED"):
        api_request(profile.model_dump(), request("/redirect"))
    assert len(state.requests) == 1
    with pytest.raises(EvoError, match="RESPONSE_TOO_LARGE"):
        api_request(profile.model_dump(), request("/large"))


@pytest.mark.parametrize(
    "settings",
    [
        {"api_url": "https://user:password@example.com"},
        {"api_url": "http://example.com"},
        {"ssh_host": "-oProxyCommand=evil"},
        {"api_container": "--privileged"},
        {"key_env": "literal secret value"},
    ],
)
def test_unsafe_profile_inputs_are_rejected(settings):
    """Configuration cannot embed API credentials or executable options."""
    with pytest.raises(ValidationError):
        Profile(**settings)


def test_worker_subprocess_and_redaction(protocol):
    """The standalone worker runs without package dependencies and never exports the API credential."""
    _, _, store = protocol
    _, profile = store.resolve()
    response = subprocess.run(
        ["uv", "run", "--no-project", str(Path(worker.__file__))],
        input=json.dumps(
            {
                "protocol": 1,
                "action": "request",
                "profile": profile.model_dump(),
                "request": request("/instance/fetchInstances"),
            }
        ),
        capture_output=True,
        text=True,
        env=os.environ,
        check=False,
        timeout=15,
    )
    assert response.returncode == 0 and not response.stderr
    result = json.loads(response.stdout)
    assert result["ok"] and result["data"]["data"][0]["token"] == "[REDACTED]"
    assert "test-only" not in response.stdout
    assert redact({"nested": [{"api_key": "secret"}], "echo": "contains secret"}, ("secret",)) == {
        "nested": [{"api_key": "[REDACTED]"}],
        "echo": "contains [REDACTED]",
    }


def test_dedicated_send_stores_receipt_without_text_or_key(protocol):
    """Durable duplicate suppression does not require storing the dedicated send's message body."""
    service, _, store = protocol
    result = service.invoke(
        "message_send", {"to": "15550000001", "text": "unique-private-content", "request_id": "safe"}
    )
    assert result.ok
    assert "test-only" not in store.path.read_text()
    payload = (service.state / "sends.sqlite3").read_bytes()
    assert b"unique-private-content" not in payload and b"test-only" not in payload
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert (service.state / "sends.sqlite3").stat().st_mode & 0o777 == 0o600


def test_secret_body_references_remain_out_of_output(protocol):
    """Explicit secret references resolve on the worker and redact echoed values."""
    _, _, store = protocol
    _, profile = store.resolve()
    message = {
        "method": "POST",
        "path": "/message/sendText/Default",
        "authenticated": True,
        "body": {"number": "15550000001", "text": {"$env": "EVOCTL_TEST_KEY"}},
        "query": {},
        "max_bytes": 4096,
        "sensitive_response": False,
    }
    result = api_request(profile.model_dump(), message)
    assert result["data"]["message"]["conversation"] == "[REDACTED]"
    assert "test-only" not in json.dumps(result)
    with pytest.raises(EvoError):
        resolve_body({"$env": "not a variable"}, [])


def test_metrics_uses_separate_authorization_reference(protocol):
    """Metrics can authenticate independently without exporting an echoed Authorization header."""
    service, _, store = protocol
    _, profile = store.resolve()
    store.add("test", profile.model_copy(update={"key_env": "EVOCTL_NONEXISTENT_KEY"}), replace=True)
    result = service.invoke("api_read", {"operation": "server.metrics", "authorization_env": "EVOCTL_TEST_KEY"})
    assert result.ok and result.data["data"]["received"] == "[REDACTED]"
