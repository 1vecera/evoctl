"""Recipient discovery regressions against the source-verified Evolution HTTP contract."""

from __future__ import annotations

import json
import sqlite3

import pytest

from evoctl import search
from evoctl.service import Service


def test_people_and_groups_are_searched_together(protocol):
    """A group absent from contacts and chats is found alongside a matching person."""
    service, state, _ = protocol
    result = service.invoke("chats_search", {"query": "NOVAK"})
    assert result.ok and result.data["complete"] and not result.data["partial"]
    assert result.data["chats"] == [
        {"jid": "120363000000000002@g.us", "name": "Alex Novák study group", "kind": "group"},
        {"jid": "15550000001@s.whatsapp.net", "name": "Alex Novák", "kind": "person"},
    ]
    assert state.requests == [
        ("GET", "/group/fetchAllGroups/Default?getParticipants=false", None),
        ("POST", "/chat/findContacts/Default", {"where": {}, "offset": 100, "page": 1}),
        ("POST", "/chat/findChats/Default", {"where": {}, "take": 100, "skip": 0}),
    ]
    assert not state.messages


@pytest.mark.parametrize("query", ["ŽLUŤOUČKÝ", "zlutoucky", "Outdated label", "000000000001@g.us"])
def test_group_subject_and_old_chat_name_are_searchable(protocol, query):
    """Group metadata wins for display while names from other sources remain search aliases."""
    service, _, _ = protocol
    result = service.invoke("chats_search", {"query": query})
    assert result.ok
    assert result.data["chats"] == [{"jid": "120363000000000001@g.us", "name": "Žluťoučký tým", "kind": "group"}]


def test_deduplication_and_exact_usable_jids(protocol):
    """Canonical phone aliases collapse, groups retain hyphens, and LIDs are never guessed as phone numbers."""
    service, state, _ = protocol
    state.contacts.extend(
        [
            {"remoteJid": "15550000001:4@s.whatsapp.net", "pushName": "Work alias"},
            {"remoteJid": "15550000001@c.us", "name": "Address book alias"},
            {"remoteJid": "12345@lid", "pushName": "Alex"},
            {"remoteJid": "12345-67890@g.us", "pushName": "Older group"},
            {"remoteJid": "status@broadcast", "pushName": "Status"},
        ]
    )
    result = service.invoke("chats_search", {})
    assert result.ok
    identifiers = [chat["jid"] for chat in result.data["chats"]]
    assert len(identifiers) == len(set(identifiers)) == 7
    assert "15550000001@s.whatsapp.net" in identifiers and "12345@lid" in identifiers
    assert "12345-67890@g.us" in identifiers
    alias = service.invoke("chats_search", {"query": "address book"})
    assert alias.data["chats"] == [{"jid": "15550000001@s.whatsapp.net", "name": "Alex Novák", "kind": "person"}]


@pytest.mark.parametrize("query", ["15550000001@s.whatsapp.net", "50000001", "+1 (555) 000-0001"])
def test_person_phone_and_jid_queries(protocol, query):
    """Exact identifiers and common formatted phone substrings resolve to the usable JID."""
    result = protocol[0].invoke("chats_search", {"query": query})
    assert result.ok
    assert [row["jid"] for row in result.data["chats"]] == ["15550000001@s.whatsapp.net"]


def test_chat_without_contact_is_found_and_message_text_is_not_searched(protocol):
    """Chat metadata fills contact gaps without searching or retaining the last message."""
    service, state, _ = protocol
    state.chats.append(
        {
            "remoteJid": "15550000009@s.whatsapp.net",
            "pushName": "Čeněk",
            "lastMessage": {"message": {"conversation": "DO_NOT_STORE_THIS_BODY"}},
        }
    )
    result = service.invoke("chats_search", {"query": "cenek", "limit": 1})
    assert result.ok and result.data["chats"][0]["jid"] == "15550000009@s.whatsapp.net"
    no_text = service.invoke("chats_search", {"query": "DO_NOT_STORE_THIS_BODY"})
    assert no_text.ok and no_text.data["complete"] and not no_text.data["chats"]
    service.invoke("chats_search", {"limit": 1})
    assert b"DO_NOT_STORE_THIS_BODY" not in (service.state / "searches.sqlite3").read_bytes()
    assert (service.state / "searches.sqlite3").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("kind", ["person", "group"])
def test_optional_kind_filters(protocol, kind):
    """Filtering narrows results and person-only search avoids the unnecessary group request."""
    service, state, _ = protocol
    result = service.invoke("chats_search", {"kind": kind})
    assert result.ok and result.data["chats"]
    assert {row["kind"] for row in result.data["chats"]} == {kind}
    if kind == "person":
        assert result.data["sources"]["groups"]["status"] == "excluded"
        assert all(not path.startswith("/group/") for _, path, _ in state.requests)


def test_empty_sources_and_no_matches(protocol):
    """Only exhausted, successful sources can establish an empty result."""
    service, state, _ = protocol
    empty = service.invoke("chats_search", {"query": "not-a-fixture-name"})
    assert empty.ok and empty.data["complete"] and empty.data["chats"] == []
    state.contacts = state.chats = state.groups = []
    empty = service.invoke("chats_search", {})
    assert empty.ok and empty.data["complete"] and empty.data["scanned"] == 0


def test_scan_budget_continues_to_late_match_and_deduplicates(protocol):
    """An empty bounded scan has a cursor; a later group and person survive cross-source duplicates."""
    service, state, store = protocol
    state.groups = []
    state.contacts = [{"remoteJid": f"1555000{index:04d}@s.whatsapp.net", "pushName": "Other"} for index in range(100)]
    state.contacts.append({"remoteJid": "15550000999@s.whatsapp.net", "pushName": "Target"})
    state.chats = [
        *state.contacts[:100],
        {"remoteJid": "120363000000009999@g.us", "pushName": "Target group"},
        state.contacts[-1],
    ]
    first = service.invoke("chats_search", {"query": "target", "scan_pages": 1, "limit": 1})
    assert first.ok and not first.data["chats"] and not first.data["complete"]
    assert not first.data["partial"] and first.data["scanned"] == 200 and len(state.requests) == 3
    arguments = {"query": "target", "scan_pages": 1, "limit": 1, "cursor": first.data["next_cursor"]}
    second = Service(store, service.state).invoke("chats_search", arguments)
    assert second.ok and len(state.requests) == 5
    assert second.data["chats"][0]["kind"] == "group" and not second.data["complete"]
    third = service.invoke("chats_search", {**arguments, "cursor": second.data["next_cursor"]})
    assert third.ok and third.data["complete"] and third.data["chats"][0]["kind"] == "person"
    assert len(state.requests) == 5  # Buffered output does not rescan upstream.
    state.contacts = state.chats = []
    assert service.invoke("chats_search", arguments).model_dump() == second.model_dump()
    assert len(state.requests) == 5  # Replaying a cursor returns the identical cached page.


def test_small_result_pages_never_drop_or_repeat_recipients(protocol):
    """A limit smaller than an upstream page retains every unreturned match between CLI-sized calls."""
    service, state, _ = protocol
    state.groups = []
    state.contacts = [{"remoteJid": f"1555000{index:04d}@s.whatsapp.net", "pushName": "Alex"} for index in range(125)]
    state.chats = list(reversed(state.contacts))
    cursor, found = "", []
    for _ in range(30):
        result = service.invoke("chats_search", {"query": "alex", "limit": 7, "scan_pages": 1, "cursor": cursor})
        assert result.ok
        found.extend(chat["jid"] for chat in result.data["chats"])
        if not result.data["next_cursor"]:
            assert result.data["complete"]
            break
        cursor = result.data["next_cursor"]
    assert len(found) == len(set(found)) == 125
    assert len(state.requests) == 5


@pytest.mark.parametrize(
    "source,path",
    [
        ("groups", "/group/fetchAllGroups/Default?getParticipants=false"),
        ("contacts", "/chat/findContacts/Default"),
        ("chats", "/chat/findChats/Default"),
    ],
)
def test_partial_upstream_failure_keeps_available_matches(protocol, source, path):
    """A failed source is visible as a top-level failure with useful partial data and the original source code."""
    service, state, _ = protocol
    state.source_failures[path] = 503
    result = service.invoke("chats_search", {"query": "alex"})
    assert not result.ok and result.error["code"] == "SEARCH_PARTIAL"
    assert result.data["chats"] and result.data["partial"] and not result.data["complete"]
    assert result.data["sources"][source]["error"]["code"] == "API_REJECTED"
    assert result.data["next_cursor"] is None
    assert len(state.requests) == 3 and not state.messages


def test_total_failure_never_claims_no_matches(protocol):
    """Authentication failure is not an empty successful directory."""
    service, state, _ = protocol
    state.source_failures = {
        path: 401
        for path in [
            "/group/fetchAllGroups/Default?getParticipants=false",
            "/chat/findContacts/Default",
            "/chat/findChats/Default",
        ]
    }
    result = service.invoke("chats_search", {})
    assert not result.ok and not result.data["complete"] and result.data["partial"]
    assert all(source["error"]["code"] == "API_UNAUTHORIZED" for source in result.data["sources"].values())


def test_failed_bulk_groups_respect_response_byte_limit(protocol):
    """Oversized unpaginated group data fails honestly and triggers no per-group fallback."""
    service, state, _ = protocol
    state.groups = [{"id": "120363000000000001@g.us", "subject": "x" * 1_048_576}]
    result = service.invoke("chats_search", {})
    assert not result.ok and result.data["sources"]["groups"]["error"]["code"] == "RESPONSE_TOO_LARGE"
    assert len(state.requests) == 3 and result.data["chats"]


def test_scan_ceiling_and_malformed_metadata_are_explicit(protocol, monkeypatch):
    """Hard bounds and invalid source records cannot turn a prefix into a complete search."""
    service, state, _ = protocol
    monkeypatch.setattr(search, "MAX_PAGES", 1)
    state.contacts = [{"remoteJid": f"1555000{index:04d}@s.whatsapp.net", "pushName": "Alex"} for index in range(100)]
    state.groups = [{"unexpected": "shape"}]
    result = service.invoke("chats_search", {"query": "not-a-fixture-name"})
    assert not result.ok and not result.data["complete"] and not result.data["has_more"]
    assert result.data["sources"]["contacts"]["error"]["code"] == "SEARCH_SCAN_LIMIT"
    assert result.data["sources"]["groups"]["error"]["code"] == "INVALID_RESPONSE"


@pytest.mark.parametrize("changes", [{"query": "sam"}, {"kind": "person"}, {"limit": 2}, {"scan_pages": 1}])
def test_cursor_binding_rejects_changed_arguments(protocol, changes):
    """Continuation cannot silently switch its query, filter or page contract."""
    service, state, _ = protocol
    first = service.invoke("chats_search", {"limit": 1})
    before = len(state.requests)
    wrong = service.invoke("chats_search", {"limit": 1, "cursor": first.data["next_cursor"], **changes})
    assert not wrong.ok and wrong.error["code"] == "INVALID_CURSOR" and len(state.requests) == before


def test_cursor_target_expiry_and_unknown_step(protocol):
    """A cursor belongs to one deployment and cannot outlive its stored metadata or skip ahead."""
    service, state, store = protocol
    first = service.invoke("chats_search", {"limit": 1})
    cursor = first.data["next_cursor"]
    before = len(state.requests)
    forged = service.invoke("chats_search", {"limit": 1, "cursor": cursor.split(":")[0] + ":999"})
    assert not forged.ok and forged.error["code"] == "INVALID_CURSOR"
    _, profile = store.resolve()
    store.add("other", profile.model_copy(update={"instance": "Other"}))
    wrong = service.invoke("chats_search", {"profile": "other", "limit": 1, "cursor": cursor})
    assert not wrong.ok and wrong.error["code"] == "INVALID_CURSOR"
    with sqlite3.connect(service.state / "searches.sqlite3") as database:
        database.execute("UPDATE sessions SET expires=0")
    expired = service.invoke("chats_search", {"limit": 1, "cursor": cursor})
    assert not expired.ok and expired.error["code"] == "INVALID_CURSOR" and len(state.requests) == before
    with sqlite3.connect(service.state / "searches.sqlite3") as database:
        assert database.execute("SELECT count(*) FROM pages").fetchone()[0] == 0


@pytest.mark.parametrize(
    "arguments", [{"kind": "people"}, {"scan_pages": 21}, {"cursor": "../private"}, {"limit": 0}, {"qurey": "x"}]
)
def test_shared_search_validation_precedes_network(protocol, arguments):
    """Malformed filters, budgets and cursor paths fail in the same shared input model."""
    service, state, _ = protocol
    result = service.invoke("chats_search", arguments)
    assert not result.ok and result.error["code"] == "INVALID_INPUT" and not state.requests


def test_repeated_pages_stop_and_metadata_storage_is_bounded(protocol, monkeypatch):
    """Broken upstream pagination and excessive metadata terminate with actionable partial results."""
    service, state, _ = protocol
    page = [{"remoteJid": f"1555000{index:04d}@s.whatsapp.net", "pushName": "Other"} for index in range(100)]
    state.contacts = page + page
    result = service.invoke("chats_search", {"query": "no-match"})
    assert not result.ok and result.data["sources"]["contacts"]["error"]["code"] == "PAGINATION_STALLED"
    monkeypatch.setattr(search, "MAX_STATE_BYTES", 1)
    limited = service.invoke("chats_search", {})
    assert not limited.ok and not limited.data["complete"]
    assert limited.data["sources"]["groups"]["error"]["code"] == "SEARCH_STATE_LIMIT"


def test_cache_busy_and_active_session_limit_are_structured(protocol, monkeypatch):
    """Concurrent readers and too many unfinished scans fail explicitly instead of corrupting a cursor."""
    service, _, _ = protocol
    first = service.invoke("chats_search", {"limit": 1})
    with sqlite3.connect(service.state / "searches.sqlite3") as database:
        database.execute("BEGIN IMMEDIATE")
        busy = service.invoke("chats_search", {"limit": 1, "cursor": first.data["next_cursor"]})
        assert not busy.ok and busy.error["code"] == "SEARCH_CACHE_BUSY"
    monkeypatch.setattr(search, "MAX_SESSIONS", 1)
    full = service.invoke("chats_search", {"query": "other"})
    assert not full.ok and full.error["code"] == "SEARCH_CACHE_FULL"
    with sqlite3.connect(service.state / "searches.sqlite3") as database:
        state = json.loads(database.execute("SELECT state FROM sessions").fetchone()[0])
    assert len(state["emitted"]) == 1


def test_system_notifications_do_not_invalidate_personal_and_group_chats(protocol):
    """The deployed chat response includes WhatsApp's system identity alongside usable recipients."""
    service, state, _ = protocol
    state.chats.extend(
        [
            {"remoteJid": "0@s.whatsapp.net", "pushName": None},
            {"remoteJid": "status@broadcast", "pushName": None},
            {"remoteJid": "12345@newsletter", "pushName": "Announcement"},
            {"remoteJid": "12345@bot", "pushName": "Meta AI"},
        ]
    )
    result = service.invoke("chats_search", {})
    assert result.ok and result.data["complete"]
    assert {row["kind"] for row in result.data["chats"]} == {"person", "group"}
    assert result.data["sources"]["chats"]["status"] == "complete"
    assert all(row["jid"] != "0@s.whatsapp.net" for row in result.data["chats"])


@pytest.mark.parametrize("operation", ["contacts_search", "chats_search"])
def test_bot_contacts_do_not_abort_later_pages(protocol, operation):
    """An unrelated Meta AI contact must not hide a person beyond the current scan budget."""
    service, state, _ = protocol
    state.contacts = [{"remoteJid": f"1555000{index:04d}@s.whatsapp.net"} for index in range(99)]
    state.contacts.extend(
        [
            {"remoteJid": "12345@bot", "pushName": "Meta AI"},
            {"remoteJid": "15550000999@s.whatsapp.net", "pushName": "Čeněk"},
        ]
    )
    arguments = {"query": "cenek", "scan_pages": 1}
    first = service.invoke(operation, arguments)
    assert first.ok and not first.data["complete"] and first.data["next_cursor"]
    second = service.invoke(operation, {**arguments, "cursor": first.data["next_cursor"]})
    assert second.ok and second.data["complete"]
    assert second.data["contacts" if operation == "contacts_search" else "chats"] == [
        {"jid": "15550000999@s.whatsapp.net", "name": "Čeněk", "kind": "person"}
    ]


def test_chat_listing_continues_past_a_filtered_system_page(protocol):
    """Filtering bots cannot turn a full upstream page into a false end of the chat list."""
    service, state, _ = protocol
    state.chats.insert(0, {"remoteJid": "12345@bot", "pushName": "Meta AI"})
    first = service.invoke("chats_list", {"limit": 1})
    assert first.ok and not first.data["chats"] and first.data["next_offset"] == 1
    second = service.invoke("chats_list", {"limit": 1, "offset": first.data["next_offset"]})
    assert second.ok and second.data["chats"][0]["jid"] == "15550000001@s.whatsapp.net"


def test_corrupt_search_cache_returns_structured_error(protocol):
    """Storage corruption cannot escape the shared JSON error envelope."""
    service, state, _ = protocol
    service.state.mkdir(parents=True, exist_ok=True)
    (service.state / "searches.sqlite3").write_bytes(b"not a database")
    result = service.invoke("chats_search", {})
    assert not result.ok and result.error["code"] == "SEARCH_CACHE_UNAVAILABLE" and not state.requests


def test_bulk_group_timeout_is_bounded_without_changing_the_profile(protocol):
    """Slow upstream picture lookups get an explicit bulk budget while ordinary reads retain the profile timeout."""
    service, state, store = protocol
    _, profile = store.resolve()
    store.add("test", profile.model_copy(update={"timeout": 0.02}), replace=True)
    state.group_delay = 0.06
    success = service.invoke("chats_search", {"query": "zlutoucky", "group_timeout": 0.2})
    assert success.ok and success.data["chats"][0]["kind"] == "group"
    assert store.resolve()[1].timeout == 0.02
    timeout = service.invoke("chats_search", {"group_timeout": 0.01})
    assert not timeout.ok and timeout.data["sources"]["groups"]["error"]["code"] == "API_UNREACHABLE"
    assert timeout.data["chats"] and not timeout.data["complete"]


@pytest.mark.parametrize("timeout", [0, 121])
def test_group_timeout_bounds_are_validated_before_network(protocol, timeout):
    """The bulk request cannot silently become unbounded."""
    service, state, _ = protocol
    result = service.invoke("chats_search", {"group_timeout": timeout})
    assert not result.ok and result.error["code"] == "INVALID_INPUT" and not state.requests


def test_terminal_cursor_cannot_be_advanced_or_rebound_to_another_timeout(protocol):
    """A guessed post-completion step and a changed timeout cannot produce a fresh scan."""
    service, state, _ = protocol
    first = service.invoke("chats_search", {"limit": 4})
    cursor = first.data["next_cursor"]
    mismatch = service.invoke("chats_search", {"limit": 4, "cursor": cursor, "group_timeout": 1})
    assert not mismatch.ok and mismatch.error["code"] == "INVALID_CURSOR"
    last = service.invoke("chats_search", {"limit": 4, "cursor": cursor})
    assert last.ok and last.data["complete"]
    before = len(state.requests)
    session, step = cursor.split(":")
    forged = service.invoke("chats_search", {"limit": 4, "cursor": f"{session}:{int(step) + 1}"})
    assert not forged.ok and forged.error["code"] == "INVALID_CURSOR" and len(state.requests) == before
