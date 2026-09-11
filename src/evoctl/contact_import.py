"""Parse exported Google/Outlook address books without inferring a person's country or identity."""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from typing import Any

import phonenumbers

from evoctl.models import ContactsImport
from evoctl.search import fold
from evoctl.worker import EvoError

MAX_CSV_BYTES = 4_194_304
MAX_ROWS = 20_000
MAX_ISSUES = 20


@dataclass
class ContactImport:
    """Hold a validated import and bounded diagnostics before any persistent changes."""

    names: dict[str, str] = field(default_factory=dict)
    rows: int = 0
    skipped_rows: int = 0
    ambiguous_phones: int = 0
    issues: list[dict[str, Any]] = field(default_factory=list)
    issue_counts: dict[str, int] = field(default_factory=dict)

    def issue(self, reason: str, row: int) -> None:
        """Keep counts for every problem without exposing raw address-book fields in diagnostics."""
        self.issue_counts[reason] = self.issue_counts.get(reason, 0) + 1
        if len(self.issues) < MAX_ISSUES:
            self.issues.append({"row": row, "reason": reason})

    def summary(self) -> dict[str, Any]:
        """Describe parse coverage separately from insert/update decisions made by the local store."""
        return {
            "rows": self.rows,
            "skipped_rows": self.skipped_rows,
            "usable_phones": len(self.names),
            "ambiguous_phones": self.ambiguous_phones,
            "issues": self.issues,
            "issue_counts": self.issue_counts,
            "issues_omitted": sum(self.issue_counts.values()) - len(self.issues),
        }


def phone_jid(value: str, region: str) -> tuple[str, str]:
    """Require international notation or an explicit region; never discard extensions or combine numbers."""
    value = value.strip()
    if not re.fullmatch(r"\+?[0-9\s().-]+", value):
        return "", "invalid_phone"
    if value.startswith("00"):
        value = "+" + value[2:]
    if not value.startswith("+") and not region:
        return "", "country_required"
    try:
        number = phonenumbers.parse(value, region or None)
    except phonenumbers.NumberParseException:
        return "", "invalid_phone"
    if number.extension or not phonenumbers.is_possible_number(number):
        return "", "invalid_phone"
    digits = phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)[1:]
    return f"{digits}@s.whatsapp.net", ""


def column_groups(headers: list[str], arguments: ContactsImport) -> tuple[list[list[str]], list[str]]:
    """Recognize export headers, or honor explicit mappings for localized/custom CSV files."""
    normalized = {fold(header): header for header in headers}
    if len(normalized) != len(headers):
        raise EvoError("INVALID_INPUT", "CSV column names must be unique.")
    if arguments.name_columns:
        groups = [arguments.name_columns]
    else:
        layouts = (
            ("name",),
            ("display name",),
            ("first name", "middle name", "last name"),
            ("given name", "additional name", "family name"),
        )
        groups = [[normalized[key] for key in layout if key in normalized] for layout in layouts]
        groups = [group for group in groups if group]
    phones = arguments.phone_columns or [
        original
        for key, original in normalized.items()
        if re.fullmatch(
            r"phone \d+ - value|(?:mobile|home|business|other|primary|car|radio|company main) phone(?: \d+)?",
            key,
        )
    ]
    selected_columns = [*phones, *(column for group in groups for column in group)]
    if not groups or not phones or any(column not in headers for column in selected_columns):
        raise EvoError(
            "INVALID_INPUT",
            "CSV needs full-name and phone columns.",
            "Export Outlook CSV or Google CSV; use name_columns/phone_columns (CLI --name-column/--phone-column) "
            "for localized or custom headers.",
        )
    return groups, phones


def parse_contacts(arguments: ContactsImport) -> ContactImport:
    """Validate the complete CSV before writing; remove shared-number ambiguities regardless of row order."""
    region = arguments.region.upper()
    if region and region not in phonenumbers.SUPPORTED_REGIONS:
        raise EvoError("INVALID_INPUT", "Use a supported two-letter phone region, such as CZ or US.")
    raw = arguments.csv_text.lstrip("\ufeff")
    if len(raw.encode("utf-8")) > MAX_CSV_BYTES or "\x00" in raw:
        raise EvoError("INVALID_INPUT", "CSV must be text without NUL characters, at most 4 MiB in UTF-8.")
    result = ContactImport()
    owners: dict[str, list[int]] = {}
    row_numbers: dict[int, set[str]] = {}
    ambiguous: set[str] = set()
    try:
        # Both formats use comma; regional Outlook settings can instead produce semicolon/tab delimiters.
        header_line = raw.splitlines()[0] if raw else ""
        delimiter = csv.Sniffer().sniff(header_line, delimiters=",;\t").delimiter
        reader = csv.reader(io.StringIO(raw, newline=""), delimiter=delimiter, strict=True)
        headers = [header.strip() for header in next(reader)]
        groups, phones = column_groups(headers, arguments)
        for row_number, fields in enumerate(reader, start=2):
            if not fields or all(not value.strip() for value in fields):
                continue
            result.rows += 1
            if result.rows > MAX_ROWS or len(fields) != len(headers):
                raise EvoError("INVALID_INPUT", "CSV exceeds 20,000 rows or has an inconsistent number of columns.")
            row = dict(zip(headers, fields, strict=True))
            names = [" ".join(row[column].strip() for column in group if row[column].strip()) for group in groups]
            name = next((name for name in names if name), "")
            row_numbers[row_number] = set()
            if not name or len(name) > 200 or any(ord(char) < 32 for char in name):
                result.issue("invalid_name" if name else "missing_name", row_number)
                continue
            values = [value.strip() for column in phones for value in row[column].split(":::") if value.strip()]
            if not values:
                result.issue("missing_phone", row_number)
            for value in values:
                jid, error = phone_jid(value, region)
                if error:
                    result.issue(error, row_number)
                    continue
                row_numbers[row_number].add(jid)
                owners.setdefault(jid, []).append(row_number)
                if jid in result.names and fold(result.names[jid]) != fold(name):
                    ambiguous.add(jid)
                else:
                    result.names.setdefault(jid, name)
    except (csv.Error, StopIteration) as error:
        raise EvoError("INVALID_INPUT", "CSV could not be parsed; check its export format and quoting.") from error
    for jid in sorted(ambiguous):
        del result.names[jid]
        for row_number in sorted(set(owners[jid])):
            result.issue("ambiguous_phone", row_number)
    result.ambiguous_phones = len(ambiguous)
    result.skipped_rows = sum(not numbers.difference(ambiguous) for numbers in row_numbers.values())
    return result
