"""Confirmed local names must survive incomplete upstream names without guessing recipients."""

from __future__ import annotations

import json

import pytest
from test_cli import command

from evoctl.mcp_surface import Surface
from evoctl.service import Service


@pytest.mark.parametrize("operation", ["contacts_search", "chats_search"])
@pytest.mark.parametrize("query", ["Jiri Dvorak", "JIŘÍ DVOŘÁK", "Dvořák", "Dvor\u030ca\u0301k"])
def test_czech_names_match_without_diacritics(protocol, operation, query):
    """Full names, surnames, capitals and decomposed Czech accents identify the same upstream contact."""
    service, state, _ = protocol
    state.contacts[0]["pushName"] = "Jiří Dvořák"
    result = service.invoke(operation, {"query": query})
    assert result.ok and result.data["complete"]
    rows = result.data["contacts" if operation == "contacts_search" else "chats"]
    assert rows == [{"jid": "15550000001@s.whatsapp.net", "name": "Jiří Dvořák", "kind": "person"}]


def test_confirmed_name_survives_missing_and_replaced_upstream_names(protocol):
    """A confirmed full name stays searchable while an upstream profile only provides a first name or null."""
    service, state, store = protocol
    state.contacts[0]["pushName"] = "Jiří"
    state.contacts[1]["pushName"] = "Jiří"
    state.chats[0]["pushName"] = None
    saved = service.invoke("contacts_name", {"jid": "15550000001:4@s.whatsapp.net", "name": "Jiří Dvořák"})
    assert saved.ok and saved.data["jid"] == "15550000001@s.whatsapp.net"
    assert not state.requests
    assert (service.state / "contact-names.sqlite3").stat().st_mode & 0o777 == 0o600
    assert "Jiří" not in store.path.read_text()
    fresh = Service(store, service.state, mode="read")
    expected = {"jid": "15550000001@s.whatsapp.net", "name": "Jiří Dvořák", "kind": "person"}
    for operation, field in (("contacts_search", "contacts"), ("chats_search", "chats")):
        result = fresh.invoke(operation, {"query": "jiri dvorak"})
        assert result.ok and result.data[field] == [expected]
        assert not fresh.invoke(operation, {"query": "Jiří Novotny"}).data[field]
    listed = fresh.invoke("chats_list", {})
    assert listed.ok and listed.data["chats"][0]["name"] == "Jiří Dvořák"
    state.contacts[0]["pushName"] = "Another profile name"
    for query in ("dvorak", "another profile"):
        result = fresh.invoke("chats_search", {"query": query})
        assert result.ok and result.data["chats"] == [expected]
    assert not state.messages


def test_names_stay_with_their_profile_and_require_an_observed_recipient(protocol):
    """A name saved elsewhere or for a vanished contact cannot manufacture a match in this account."""
    service, _, store = protocol
    assert service.invoke("contacts_name", {"jid": "15550000001", "name": "Private name"}).ok
    _, profile = store.resolve()
    store.add("other", profile)
    result = service.invoke("chats_search", {"profile": "other", "query": "private"})
    assert result.ok and result.data["complete"] and not result.data["chats"]
    assert service.invoke("contacts_name", {"jid": "15550000999", "name": "Missing person"}).ok
    absent = service.invoke("chats_search", {"query": "missing person"})
    assert absent.ok and absent.data["complete"] and not absent.data["chats"]


def test_editing_names_requires_a_fresh_search_cursor(protocol):
    """A cursor cannot mix cached names with a subsequently edited local directory."""
    service, state, _ = protocol
    first = service.invoke("chats_search", {"limit": 1})
    assert first.ok and first.data["next_cursor"]
    assert service.invoke("contacts_name", {"jid": "15550000001", "name": "New name"}).ok
    before = len(state.requests)
    old = service.invoke("chats_search", {"limit": 1, "cursor": first.data["next_cursor"]})
    assert not old.ok and old.error["code"] == "INVALID_CURSOR" and len(state.requests) == before
    assert service.invoke("chats_search", {"query": "new name"}).data["chats"][0]["name"] == "New name"


@pytest.mark.parametrize(
    "arguments",
    [
        {"jid": "A person's name", "name": "Example"},
        {"jid": "12345@bot", "name": "Example"},
        {"jid": "12345@newsletter", "name": "Example"},
        {"jid": "15550000001", "name": "   "},
        {"jid": "15550000001"},
    ],
)
def test_invalid_names_never_touch_network_or_storage(protocol, arguments):
    """Only an explicit person/group address and a deliberate name or removal can change local names."""
    service, state, _ = protocol
    result = service.invoke("contacts_name", arguments)
    assert not result.ok and not state.requests
    assert not (service.state / "contact-names.sqlite3").exists()


def test_name_writes_obey_mcp_capabilities(protocol):
    """Local changes remain write actions even though they never send data to WhatsApp."""
    service, state, store = protocol
    request = {"action": "contacts_name", "arguments": {"jid": "15550000001", "name": "Example"}}
    read = Surface(Service(store, service.state, mode="read"))
    assert not read.invoke("evoctl_discover", {"operation": "contacts_name"}).ok
    assert not read.invoke("evoctl_read", request).ok
    assert not read.invoke("evoctl_write", request).ok
    write = Surface(Service(store, service.state, mode="write"))
    assert write.invoke("evoctl_read", request).error["code"] == "WRONG_MCP_TOOL"
    assert write.invoke("evoctl_write", request).ok
    assert not state.requests


def test_cli_names_are_explicit_and_reversible(protocol):
    """CLI name changes persist between processes, and --clear restores the upstream display name."""
    service, state, _ = protocol
    for arguments in (("15550000001",), ("15550000001", "Alex", "--clear")):
        invalid = command(protocol, "contacts", "name", *arguments)
        assert invalid.returncode == 2 and json.loads(invalid.stdout)["error"]["code"] == "INVALID_INPUT"
    saved = command(protocol, "contacts", "name", "+15550000001", "Jiří Dvořák")
    assert saved.returncode == 0 and json.loads(saved.stdout)["data"]["saved"]
    assert not state.requests
    found = command(protocol, "contacts", "search", "dvorak")
    assert found.returncode == 0 and json.loads(found.stdout)["data"]["contacts"][0]["name"] == "Jiří Dvořák"
    cleared = command(protocol, "contacts", "name", "15550000001", "--clear")
    assert cleared.returncode == 0 and not json.loads(cleared.stdout)["data"]["saved"]
    assert not service.invoke("chats_search", {"query": "dvorak"}).data["chats"]
    assert service.invoke("contacts_search", {"query": "novak"}).data["contacts"][0]["name"] == "Alex Novák"
    assert not state.messages


def test_broken_name_storage_does_not_silently_hide_matches(protocol):
    """A corrupt local directory fails explicitly before any remote scan, instead of returning false negatives."""
    service, state, _ = protocol
    assert service.invoke("contacts_name", {"jid": "15550000001", "name": "Example"}).ok
    (service.state / "contact-names.sqlite3").write_bytes(b"not a database")
    result = service.invoke("chats_search", {"query": "example"})
    assert not result.ok and result.error["code"] == "CONTACT_NAMES_UNAVAILABLE" and not state.requests
