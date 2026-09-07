"""Read-only live smoke checks; print counts and readiness, never contact or message content."""

from __future__ import annotations

import argparse
import json
import subprocess
import urllib.request


def call(profile: str, *arguments: str) -> dict:
    """Exercise the installed command contract without exposing private response payloads."""
    response = subprocess.run(
        ["uv", "run", "evoctl", "--profile", profile, *arguments],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    result = json.loads(response.stdout)
    if response.returncode or not result["ok"]:
        raise RuntimeError(result["error"] or "Deployment is not ready.")
    return result["data"]


def main() -> None:
    """Verify an existing deployment through status, paged reads, and a loopback GUI request."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--contact", default="")
    parser.add_argument("--chat", default="")
    parser.add_argument("--message-id", default="")
    arguments = parser.parse_args()
    status = call(arguments.profile, "status")
    contacts = call(arguments.profile, "contacts", "search", arguments.contact, "--limit", "2")
    chats = call(arguments.profile, "chats", "list", "--limit", "2")
    gui = call(arguments.profile, "ui")
    with urllib.request.urlopen(gui["url"], timeout=10) as response:
        gui_status = response.status
    report = {
        "ready": status["ready"],
        "version": status["version"],
        "contacts_returned": len(contacts["contacts"]),
        "chats_returned": len(chats["chats"]),
        "gui_http_status": gui_status,
    }
    if arguments.chat:
        messages = call(arguments.profile, "messages", "read", arguments.chat, "--limit", "2")
        report["messages_returned"] = len(messages["messages"])
    if arguments.message_id:
        receipt = call(arguments.profile, "messages", "status", arguments.message_id)
        report["receipt_status"] = receipt["status"]
    print(json.dumps(report))


if __name__ == "__main__":
    main()
