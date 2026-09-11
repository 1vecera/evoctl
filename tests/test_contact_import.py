"""Bulk address-book imports must be private, explicit about ambiguity, and usable offline."""

from __future__ import annotations

import json
import sqlite3

import pytest
from test_cli import command

from evoctl.mcp_surface import Surface
from evoctl.service import Service

OUTLOOK = (
    "\ufeffFirst Name,Middle Name,Last Name,Mobile Phone,Home Phone,E-mail Address\r\n"
    "Jiří,,Dvořák,+1 (555) 000-0001,0015550000002,jiri@example.com\r\n"
    "Only,,Email,,,email@example.com\r\n"
)


def test_outlook_preview_import_and_offline_listing(protocol):
    """Preview has no storage side effects; imported full names match offline and in observed WhatsApp chats."""
    service, state, store = protocol
    preview = service.invoke("contacts_import", {"csv_text": OUTLOOK, "dry_run": True})
    assert preview.ok and preview.data["inserted"] == 2 and preview.data["dry_run"]
    assert preview.data["rows"] == 2 and preview.data["skipped_rows"] == 1
    assert preview.data["issues"] == [{"row": 3, "reason": "missing_phone"}]
    assert not (service.state / "contact-names.sqlite3").exists() and not state.requests
    saved = service.invoke("contacts_import", {"csv_text": OUTLOOK})
    assert saved.ok and saved.data == {**preview.data, "dry_run": False}
    fresh = Service(store, service.state, mode="read")
    listed = fresh.invoke("contacts_list", {"query": "JIRI DVORAK", "limit": 1})
    assert listed.ok and listed.data["total"] == 2 and listed.data["next_offset"] == 1
    assert listed.data["source"] == "local" and listed.data["whatsapp_verified"] is False
    assert listed.data["contacts"] == [{"jid": "15550000001@s.whatsapp.net", "name": "Jiří Dvořák", "kind": "person"}]
    assert fresh.invoke("contacts_list", {"offset": 1}).data["next_offset"] is None
    assert fresh.invoke("contacts_list", {"query": "+1 (555) 000-0002"}).data["total"] == 1
    assert not state.requests
    state.contacts[0]["pushName"] = "Jiří"
    state.contacts[1]["pushName"] = "Unrelated profile"
    found = fresh.invoke("chats_search", {"query": "dvorak"})
    assert found.ok and {row["name"] for row in found.data["chats"]} == {"Jiří Dvořák"}
    assert (service.state / "contact-names.sqlite3").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "csv_text",
    [
        'Name,Phone 1 - Type,Phone 1 - Value,Phone 2 - Value\n"Dvořák, Jiří",Mobile,'
        '"+1 555 000 0001 ::: +1 555 000 0002",+1 555 000 0003\n',
        'Given Name,Additional Name,Family Name,Phone 1 - Value\nJiří,,Dvořák,"+1 555 000 0001 ::: '
        '+1 555 000 0002 ::: +1 555 000 0003"\n',
        "First Name;Middle Name;Last Name;Mobile Phone;Business Phone;Business Phone 2\n"
        "Jiří;;Dvořák;+1 555 000 0001;+1 555 000 0002;+1 555 000 0003\n",
    ],
)
def test_google_and_outlook_csv_variants(protocol, csv_text):
    """Export layouts preserve quoted names, Unicode, multiple phone columns and Google value separators."""
    service, state, _ = protocol
    result = service.invoke("contacts_import", {"csv_text": csv_text})
    assert result.ok and result.data["inserted"] == 3 and result.data["skipped_rows"] == 0
    assert service.invoke("contacts_list", {"query": "dvorak"}).data["total"] == 3
    assert not state.requests


def test_national_numbers_require_an_explicit_region(protocol):
    """No country is inferred from the machine locale; extensions, short codes and malformed values stay out."""
    service, state, _ = protocol
    csv_text = (
        "Name,Mobile Phone\nLocal,212 345 678\nShort,112\nExtension,+1 555 000 0001 ext 2\n"
        "Malformed,+1 555 000 0002 / +1 555 000 0003\n"
    )
    strict = service.invoke("contacts_import", {"csv_text": csv_text})
    assert strict.ok and strict.data["inserted"] == 0
    assert strict.data["skipped_rows"] == 4
    assert strict.data["issues"][0]["reason"] == "country_required"
    region = service.invoke("contacts_import", {"csv_text": csv_text, "region": "cz"})
    assert region.ok and region.data["inserted"] == 1 and region.data["skipped_rows"] == 3
    assert service.invoke("contacts_list", {}).data["contacts"][0]["jid"] == "420212345678@s.whatsapp.net"
    invalid = service.invoke("contacts_import", {"csv_text": csv_text, "region": "XX"})
    assert not invalid.ok and invalid.error["code"] == "INVALID_INPUT" and not state.requests


def test_reimports_preserve_existing_names_unless_replaced(protocol):
    """Repeated import is idempotent; a new export cannot silently overwrite a confirmed local name."""
    service, state, _ = protocol
    assert service.invoke("contacts_name", {"jid": "15550000001", "name": "Confirmed name"}).ok
    first = service.invoke("contacts_import", {"csv_text": OUTLOOK})
    assert first.ok and first.data["inserted"] == 1 and first.data["conflicts"] == 1
    again = service.invoke("contacts_import", {"csv_text": OUTLOOK})
    assert again.ok and again.data["unchanged"] == 1 and again.data["conflicts"] == 1
    preview = service.invoke("contacts_import", {"csv_text": OUTLOOK, "replace": True, "dry_run": True})
    assert preview.ok and preview.data["updated"] == 1
    assert service.invoke("contacts_list", {"query": "confirmed"}).data["total"] == 1
    replace = service.invoke("contacts_import", {"csv_text": OUTLOOK, "replace": True})
    assert replace.ok and replace.data["updated"] == 1 and replace.data["unchanged"] == 1
    assert service.invoke("contacts_list", {"query": "confirmed"}).data["total"] == 0
    assert not state.requests


def test_shared_number_is_ambiguous_even_with_replace(protocol):
    """Two different people sharing a number are excluded, independent of CSV order or replacement policy."""
    service, _, _ = protocol
    text = "Name,Mobile Phone\nPerson A,+15550000001\nPerson B,+15550000001\nPerson A,+15550000001\n"
    result = service.invoke("contacts_import", {"csv_text": text, "replace": True})
    assert result.ok and result.data["inserted"] == 0 and result.data["ambiguous_phones"] == 1
    assert result.data["skipped_rows"] == 3
    assert service.invoke("contacts_list", {}).data["total"] == 0


def test_identical_csv_duplicates_collapse_and_other_profiles_stay_separate(protocol):
    """The same mapping is imported once, and local lookup uses the exact selected deployment scope."""
    service, _, store = protocol
    result = service.invoke("contacts_import", {"csv_text": OUTLOOK + OUTLOOK.splitlines()[1] + "\n"})
    assert result.ok and result.data["inserted"] == 2 and result.data["ambiguous_phones"] == 0
    _, profile = store.resolve()
    store.add("other", profile)
    assert service.invoke("contacts_list", {"profile": "other"}).data["total"] == 0


@pytest.mark.parametrize(
    "csv_text",
    [
        "",
        "Name,Email\nA,a@example.com\n",
        "Name,Mobile Phone,Mobile Phone\nA,+15550000001,+15550000002\n",
        "Name,Mobile Phone\nA,+15550000001,extra\n",
        'Name,Mobile Phone\nA,"unterminated\n',
        "Name,Mobile Phone\nA,+15550000001\nB\n",
    ],
)
def test_malformed_files_fail_atomically(protocol, csv_text):
    """A malformed export never commits a valid prefix or echoes private row contents into an error."""
    service, state, _ = protocol
    result = service.invoke("contacts_import", {"csv_text": csv_text})
    assert not result.ok and result.error["code"] == "INVALID_INPUT"
    assert not (service.state / "contact-names.sqlite3").exists() and not state.requests
    assert "+15550000001" not in json.dumps(result.error)


def test_custom_columns_and_cli_decoding(protocol, tmp_path):
    """Localized exports use explicit column mappings; CLI handles UTF-16 and configurable legacy encodings."""
    csv_text = "Jméno;Příjmení;Telefon\nJiří;Dvořák;+15550000001\n"
    path = tmp_path / "contacts.csv"
    for encoding, flags in (("utf-16", ()), ("cp1250", ("--encoding", "cp1250"))):
        path.write_bytes(csv_text.encode(encoding))
        result = command(
            protocol,
            "contacts",
            "import",
            str(path),
            "--name-column",
            "Jméno",
            "--name-column",
            "Příjmení",
            "--phone-column",
            "Telefon",
            "--dry-run",
            *flags,
        )
        assert result.returncode == 0, result.stdout
        assert json.loads(result.stdout)["data"]["inserted"] == 1
    invalid = command(protocol, "contacts", "import", str(path))
    assert invalid.returncode == 2 and json.loads(invalid.stdout)["error"]["code"] == "INVALID_INPUT"
    stdin = command(protocol, "contacts", "import", "-", input_text=OUTLOOK)
    assert stdin.returncode == 0 and json.loads(stdin.stdout)["data"]["inserted"] == 2
    listed = command(protocol, "contacts", "list", "--query", "dvorak")
    assert listed.returncode == 0 and json.loads(listed.stdout)["data"]["total"] == 2


def test_import_capabilities_and_bounded_diagnostics(protocol):
    """Import is a local write in MCP; large incomplete books return explicit counts and bounded row diagnostics."""
    service, state, store = protocol
    arguments = {"csv_text": "Name,Mobile Phone\n" + "Nobody,112\n" * 60, "dry_run": True}
    request = {"action": "contacts_import", "arguments": arguments}
    read = Surface(Service(store, service.state, mode="read"))
    assert not read.invoke("evoctl_read", request).ok
    assert not read.invoke("evoctl_write", request).ok
    write = Surface(Service(store, service.state, mode="write"))
    result = write.invoke("evoctl_write", request)
    assert result.ok and result.data["skipped_rows"] == 60
    assert len(result.data["issues"]) == 20 and result.data["issues_omitted"] == 40
    assert result.data["issue_counts"] == {"country_required": 60}
    assert not state.requests


def test_storage_failure_rolls_back_the_entire_import(protocol):
    """A mid-import SQLite failure restores earlier names and inserts instead of persisting a partial book."""
    service, state, _ = protocol
    assert service.invoke("contacts_name", {"jid": "15550000001", "name": "Original"}).ok
    with sqlite3.connect(service.state / "contact-names.sqlite3") as database:
        database.execute(
            "CREATE TRIGGER reject_second BEFORE INSERT ON names WHEN new.jid='15550000002@s.whatsapp.net' "
            "BEGIN SELECT RAISE(ABORT, 'fixture storage error'); END"
        )
    result = service.invoke("contacts_import", {"csv_text": OUTLOOK, "replace": True})
    assert not result.ok and result.error["code"] == "CONTACT_NAMES_UNAVAILABLE"
    assert service.invoke("contacts_list", {}).data["contacts"] == [
        {"jid": "15550000001@s.whatsapp.net", "name": "Original", "kind": "person"}
    ]
    assert not state.requests


@pytest.mark.parametrize("csv_text", ["Name,Mobile Phone\n" + "A,+15550000001\n" * 20_001, "é" * 2_097_153])
def test_oversized_imports_do_not_persist_a_prefix(protocol, csv_text):
    """Both row and UTF-8 byte bounds apply before committing any names, including multibyte CSV text."""
    service, state, _ = protocol
    result = service.invoke("contacts_import", {"csv_text": csv_text})
    assert not result.ok and result.error["code"] == "INVALID_INPUT"
    assert not (service.state / "contact-names.sqlite3").exists() and not state.requests
