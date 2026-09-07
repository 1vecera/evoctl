"""Workflow contracts across a real HTTP boundary."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from evoctl.service import Service


def test_status_and_contact_search(protocol):
    """Readiness and accent-insensitive search expose exact recipient identifiers."""
    service, state, _ = protocol
    health = service.invoke("status", {})
    assert health.ok and health.data["ready"]
    assert health.data["authentication"] == "accepted"
    found = service.invoke("contacts_search", {"query": "novak"})
    assert found.ok
    assert found.data["contacts"] == [{"jid": "15550000001@s.whatsapp.net", "name": "Alex Novák", "kind": "person"}]
    assert found.data["complete"]
    assert len(state.requests) == 3


def test_send_history_and_delivery_are_distinct(protocol):
    """Acceptance stays pending until an actual delivery or read receipt exists."""
    service, state, _ = protocol
    sent = service.invoke(
        "message_send", {"to": "+15550000001", "text": "Ahoj 👋\nNext line", "request_id": "greeting"}
    )
    assert sent.ok and sent.data["accepted"]
    assert sent.data["status"] == "pending"
    assert not sent.data["delivery_confirmed"]
    history = service.invoke("messages_read", {"chat": "15550000001", "limit": 2})
    assert history.ok and history.data["messages"][0]["text"] == "Ahoj 👋\nNext line"
    status = service.invoke("message_status", {"message_id": sent.data["message_id"]})
    assert status.ok and status.data["status"] == "pending"
    assert status.data["evidence"] == "stored_message_without_receipt"
    state.updates.append({"keyId": sent.data["message_id"], "status": "SERVER_ACK"})
    acknowledged = service.invoke("message_status", {"message_id": sent.data["message_id"]})
    assert acknowledged.data["status"] == "server_ack" and not acknowledged.data["delivery_confirmed"]
    state.updates.append({"keyId": sent.data["message_id"], "status": "READ"})
    read = service.invoke("message_status", {"message_id": sent.data["message_id"]})
    assert read.data["status"] == "read" and read.data["delivery_confirmed"]


def test_duplicate_send_is_suppressed_across_service_instances(protocol):
    """A durable request ID survives process-equivalent service reconstruction."""
    service, state, store = protocol
    arguments = {"to": "15550000001", "text": "Exactly once", "request_id": "stable"}
    first = service.invoke("message_send", arguments)
    second = Service(store, service.state).invoke("message_send", arguments)
    assert first.ok and second.ok and second.data["duplicate_suppressed"]
    assert first.data["message_id"] == second.data["message_id"]
    assert len(state.messages) == 1
    conflict = service.invoke("message_send", {**arguments, "text": "Different"})
    assert not conflict.ok and conflict.error["code"] == "IDEMPOTENCY_CONFLICT"


def test_concurrent_sends_make_at_most_one_network_mutation(protocol):
    """SQLite reserves atomically even when independent clients race."""
    service, state, store = protocol
    state.send_delay = 0.1
    arguments = {"to": "15550000001", "text": "Only once", "request_id": "concurrent"}
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(Service(store, service.state).invoke, "message_send", arguments) for _ in range(4)]
        results = [future.result() for future in futures]
    assert sum(result.ok for result in results) >= 1
    assert len(state.messages) == 1
    assert all(result.ok or result.error["code"] == "SEND_OUTCOME_UNKNOWN" for result in results)


def test_uncertain_send_cannot_be_retried_automatically(protocol):
    """A timeout after the request leaves preserves the reservation without resending."""
    service, state, store = protocol
    _, profile = store.resolve()
    store.add("test", profile.model_copy(update={"timeout": 0.03}), replace=True)
    state.send_delay = 0.15
    arguments = {"to": "15550000001", "text": "May have sent", "request_id": "timeout"}
    first = service.invoke("message_send", arguments)
    second = service.invoke("message_send", arguments)
    assert not first.ok and not second.ok
    assert second.error["code"] == "SEND_OUTCOME_UNKNOWN"
    assert len([request for request in state.requests if request[1].startswith("/message/")]) == 1


def test_preview_does_not_send_or_reserve(protocol):
    """A preview can be followed by a real send with the same request ID."""
    service, state, _ = protocol
    arguments = {"to": "15550000001", "text": "Preview", "request_id": "preview"}
    preview = service.invoke("message_send", {**arguments, "dry_run": True})
    assert preview.ok and not state.requests
    sent = service.invoke("message_send", arguments)
    assert sent.ok and len(state.messages) == 1


@pytest.mark.parametrize("recipient", ["Alex", "Sam Example", "; echo wrong", "-oProxyCommand=x", "../../private"])
def test_recipient_must_be_an_exact_identifier(protocol, recipient):
    """Names and executable fragments cannot become accidental recipients."""
    service, state, _ = protocol
    result = service.invoke("message_send", {"to": recipient, "text": "No", "request_id": "invalid"})
    assert not result.ok and not state.requests


def test_read_mode_cannot_bypass_capabilities_with_generic_calls(protocol):
    """A GET mutation and a lookup POST retain their semantic access levels."""
    original, state, store = protocol
    service = Service(store, original.state, mode="read")
    denied = service.invoke("api_read", {"operation": "instance.connect"})
    direct = service.invoke("message_send", {"to": "15550000001", "text": "No", "request_id": "denied"})
    assert not denied.ok and not direct.ok and not state.requests
    lookup = service.invoke(
        "api_read", {"operation": "chat.find_messages", "body": {"where": {"key": {}}, "offset": 1, "page": 1}}
    )
    assert lookup.ok


def test_api_credentials_are_redacted(protocol):
    """Instance responses cannot export their API tokens into JSON output."""
    service, _, _ = protocol
    result = service.invoke("api_read", {"operation": "instance.fetch_instances"})
    assert result.ok
    assert result.data["data"][0]["token"] == "[REDACTED]"
    assert result.data["data"][0]["hash"] == "[REDACTED]"


def test_invalid_parameters_fail_before_network(protocol):
    """Path traversal and misspelled fields are rejected by the shared input contract."""
    service, state, _ = protocol
    bad_path = service.invoke(
        "api_read", {"operation": "instance.connection_state", "path_parameters": {"instanceName": "../other"}}
    )
    bad_limit = service.invoke("messages_read", {"chat": "15550000001", "limit": 1000})
    unknown = service.invoke("contacts_search", {"qurey": "Alex"})
    assert not bad_path.ok and not bad_limit.ok and not unknown.ok
    assert not state.requests


def test_contact_cursor_does_not_skip_matches_inside_a_page(protocol):
    """A small result limit can be paged across an entire upstream page without omissions."""
    service, state, _ = protocol
    state.contacts = [{"remoteJid": f"1555000{index:04d}@s.whatsapp.net", "pushName": "Alex"} for index in range(125)]
    cursor = ""
    found = []
    while True:
        result = service.invoke("contacts_search", {"query": "alex", "limit": 7, "cursor": cursor})
        assert result.ok
        found.extend(contact["jid"] for contact in result.data["contacts"])
        if result.data["complete"]:
            break
        cursor = result.data["next_cursor"]
    assert len(found) == len(set(found)) == 125


def test_invalid_api_body_does_not_reserve_a_request(protocol):
    """A rejected local schema can be corrected under the same request ID."""
    service, state, _ = protocol
    bad = service.invoke("api_write", {"operation": "message.send_text", "request_id": "schema"})
    assert not bad.ok and bad.error["code"] == "INVALID_BODY" and not state.requests
    fixed = service.invoke(
        "api_write",
        {"operation": "message.send_text", "request_id": "schema", "body": {"number": "15550000001", "text": "Valid"}},
    )
    assert fixed.ok and len(state.messages) == 1
