"""Actual command-line process behavior, including output and exit status."""

from __future__ import annotations

import json
import os
import subprocess
import sys


def command(protocol, *arguments: str, input_text: str | None = None):
    """Launch a separate CLI process against the local protocol fixture."""
    service, _, store = protocol
    environment = {
        **os.environ,
        "EVOCTL_CONFIG_DIR": str(store.directory),
        "EVOCTL_STATE_DIR": str(service.state),
        "NO_COLOR": "1",
    }
    return subprocess.run(
        [sys.executable, "-m", "evoctl.cli", *arguments],
        input=input_text,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def test_cli_json_and_stdin_send(protocol):
    """Unicode stdin is sent as data, and stdout contains one JSON envelope."""
    sent = command(
        protocol,
        "messages",
        "send",
        "15550000001",
        "--text-file",
        "-",
        "--request-id",
        "cli",
        input_text="Hi — $(literal)",
    )
    assert sent.returncode == 0, sent.stderr
    assert not sent.stderr
    result = json.loads(sent.stdout)
    assert result["ok"] and result["data"]["status"] == "pending"
    assert protocol[1].messages[0]["message"]["conversation"] == "Hi — $(literal)"


def test_cli_validation_has_json_and_nonzero_exit(protocol):
    """Invalid commands do not print tracebacks or falsely return success."""
    bad = command(protocol, "messages", "send", "15550000001")
    assert bad.returncode == 2
    assert json.loads(bad.stdout)["error"]["code"] == "INVALID_INPUT"
    assert "Traceback" not in bad.stderr
    unknown = command(protocol, "status", "--does-not-exist")
    assert unknown.returncode == 2 and json.loads(unknown.stdout)["error"]["code"] == "INVALID_INPUT"
    contact = command(protocol, "contacts", "search", "Alex")
    assert contact.returncode == 0 and json.loads(contact.stdout)["data"]["contacts"]
    remote = command(protocol, "remote", "connect", "missing")
    assert remote.returncode == 4


def test_status_exit_and_watch_json_lines(protocol):
    """Unready status is nonzero and finite monitoring produces one envelope per line."""
    protocol[1].whatsapp = "close"
    status = command(protocol, "status")
    assert status.returncode == 1
    assert not json.loads(status.stdout)["data"]["ready"]
    protocol[1].whatsapp = "open"
    watch = command(protocol, "status", "--watch", "--count", "2", "--interval", "1")
    assert watch.returncode == 0
    snapshots = [json.loads(line) for line in watch.stdout.splitlines()]
    assert len(snapshots) == 2 and all(snapshot["data"]["ready"] for snapshot in snapshots)


def test_cli_catalog_is_available_without_network(protocol):
    """API schema discovery and version are usable before configuring a server."""
    result = command(protocol, "api", "list", "--query", "message", "--limit", "5")
    assert result.returncode == 0
    listing = json.loads(result.stdout)["data"]
    assert len(listing["items"]) == 5 and listing["next_offset"] == 5
