"""Bounded recipient discovery with private, replayable CLI/MCP continuations."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import unicodedata
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from evoctl.config import private_directory
from evoctl.models import ChatsSearch
from evoctl.worker import EvoError

PAGE_SIZE = 100
MAX_PAGES = 100
MAX_STATE_BYTES = 8_388_608
SESSION_SECONDS = 1800
MAX_SESSIONS = 32


def fold(value: str) -> str:
    """Compare display names without case or combining accents."""
    return "".join(part for part in unicodedata.normalize("NFKD", value.casefold()) if not unicodedata.combining(part))


def recipient(record: dict[str, Any], source: str, query: str, contact_names: dict[str, str]) -> dict[str, Any] | None:
    """Reduce upstream records to recipient metadata; never retain last messages or group descriptions."""
    raw_jid = record["id" if source == "groups" else "remoteJid"]
    if not isinstance(raw_jid, str):
        raise ValueError("Missing recipient identifier.")
    jid = raw_jid.strip().lower().replace("@c.us", "@s.whatsapp.net")
    # Device suffixes address the same person; LIDs remain distinct from phone JIDs.
    jid = re.sub(r":\d+(?=@(?:s\.whatsapp\.net|lid)$)", "", jid)
    if jid == "0@s.whatsapp.net":
        return None  # Evolution excludes this system notification identity from personal imports too.
    if not re.fullmatch(r"[0-9][0-9._-]{3,180}@(s\.whatsapp\.net|g\.us|lid)", jid):
        if jid.endswith(("@broadcast", "@newsletter", "@bot")):
            return None
        raise ValueError("Invalid recipient identifier.")
    kind = "group" if jid.endswith("@g.us") else "person"
    fields = ("subject", "name", "pushName", "displayName", "verifiedName", "notify")
    names = [record[field] for field in fields if record.get(field)]
    if any(not isinstance(name, str) for name in names):
        raise ValueError("Invalid display name.")
    local_name = contact_names.get(jid, "")
    if local_name:
        names.insert(0, local_name)
    name = names[0] if names else ""
    rank = {"groups": 3, "contacts": 2, "chats": 1}[source] if name else 0
    matched = any(fold(query) in fold(value) for value in [*names, jid, raw_jid])
    if kind == "person" and re.fullmatch(r"[+\d\s().-]+", query):
        digits = re.sub(r"\D", "", query)
        matched |= bool(digits) and digits in jid.split("@")[0]
    return {
        "jid": jid,
        "name": name,
        "kind": kind,
        "matched": matched,
        "rank": 4 if local_name else rank,
    }


class ConversationSearch:
    """Combine group subjects, stored contacts and message-backed chat metadata within explicit budgets."""

    def __init__(self, request: Callable[[str, Any, Any], Any], contact_names: dict[str, str]) -> None:
        """Bind the service's bounded requests and one snapshot of locally confirmed names."""
        self.request = request
        self.contact_names = contact_names

    def scan(self, state: dict[str, Any], arguments: ChatsSearch) -> int:
        """Advance each available source, retaining earlier matches when another source fails."""
        scanned = 0
        for source, progress in state["sources"].items():
            if progress["status"] != "pending":
                continue
            for _ in range(1 if source == "groups" else arguments.scan_pages):
                try:
                    if source == "groups":
                        records = self.request("group.fetch_all_groups", None, {"getParticipants": "false"})
                    elif source == "contacts":
                        records = self.request(
                            "chat.find_contacts", {"where": {}, "offset": PAGE_SIZE, "page": progress["page"]}, None
                        )
                    else:
                        records = self.request(
                            "chat.find_chats",
                            {"where": {}, "take": PAGE_SIZE, "skip": (progress["page"] - 1) * PAGE_SIZE},
                            None,
                        )
                    if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
                        raise ValueError("Expected a recipient array.")
                    if source != "groups" and len(records) > PAGE_SIZE:
                        raise EvoError("PAGINATION_UNSUPPORTED", "The source ignored the requested page size.")
                    rows = [
                        row
                        for record in records
                        if (row := recipient(record, source, arguments.query, self.contact_names))
                    ]
                    # A broken proxy repeating a full page must not cause an endless continuation.
                    digest = hashlib.sha256(json.dumps(sorted(row["jid"] for row in rows)).encode()).hexdigest()
                    if source != "groups" and len(records) == PAGE_SIZE and digest in progress["pages_seen"]:
                        raise EvoError("PAGINATION_STALLED", "The source repeated a full page; coverage is incomplete.")
                    candidates = dict(state["recipients"])
                    for row in rows:
                        if arguments.kind != "all" and row["kind"] != arguments.kind:
                            continue
                        previous = candidates.get(row["jid"])
                        if previous:
                            preferred = row if row["rank"] > previous["rank"] else previous
                            row = {**preferred, "matched": row["matched"] or previous["matched"]}
                        candidates[row["jid"]] = row
                    if len(json.dumps(candidates).encode()) > MAX_STATE_BYTES:
                        raise EvoError("SEARCH_STATE_LIMIT", "The search reached its private metadata storage bound.")
                    state["recipients"] = candidates
                    progress["scanned"] += len(records)
                    scanned += len(records)
                    if source == "groups" or len(records) < PAGE_SIZE:
                        progress["status"] = "complete"
                        break
                    progress["pages_seen"].append(digest)
                    progress["page"] += 1
                    if progress["page"] > MAX_PAGES:
                        raise EvoError("SEARCH_SCAN_LIMIT", "The source reached the 10,000-row search bound.")
                except (ValueError, KeyError, TypeError):
                    progress["status"] = "failed"
                    progress["error"] = EvoError(
                        "INVALID_RESPONSE", "The source returned invalid recipient metadata."
                    ).as_dict()
                    break
                except EvoError as error:
                    progress["status"] = "failed"
                    progress["error"] = error.as_dict()
                    break
        return scanned

    def advance(self, state: dict[str, Any], arguments: ChatsSearch) -> dict[str, Any]:
        """Drain saved matches before scanning more; never emit a canonical JID twice in a continuation chain."""
        if not state:
            state.update(
                recipients={},
                emitted=[],
                sources={
                    source: {
                        "status": "excluded" if source == "groups" and arguments.kind == "person" else "pending",
                        "page": 1,
                        "scanned": 0,
                        "pages_seen": [],
                    }
                    for source in ("groups", "contacts", "chats")
                },
            )
        emitted = set(state["emitted"])
        pending = [row for jid, row in state["recipients"].items() if row["matched"] and jid not in emitted]
        scanned = 0
        if not pending:
            scanned = self.scan(state, arguments)
            pending = [row for jid, row in state["recipients"].items() if row["matched"] and jid not in emitted]
        pending.sort(key=lambda row: row["jid"])
        chats = [{key: row[key] for key in ("jid", "name", "kind")} for row in pending[: arguments.limit]]
        state["emitted"].extend(chat["jid"] for chat in chats)
        sources = {
            name: {key: value for key, value in progress.items() if key not in {"page", "pages_seen"}}
            for name, progress in state["sources"].items()
        }
        has_more = len(pending) > len(chats) or any(source["status"] == "pending" for source in sources.values())
        failed = any(source["status"] == "failed" for source in sources.values())
        return {
            "chats": chats,
            "scanned": scanned,
            "matches_in_scan": len(pending),
            "complete": not has_more and not failed,
            "partial": failed,
            "sources": sources,
            "has_more": has_more,
            "next_cursor": None,
            "consistency": "observed",
        }


class SearchCache:
    """Keep bounded, short-lived metadata and cached response pages shared across CLI/MCP processes."""

    def __init__(self, directory: Path) -> None:
        """Create private continuation storage; message bodies, credentials and participants never enter it."""
        self.path = private_directory(directory) / "searches.sqlite3"

    def run(
        self,
        scope: str,
        arguments: ChatsSearch,
        advance: Callable[[dict[str, Any], ChatsSearch], dict[str, Any]],
    ) -> dict[str, Any]:
        """Serialize cursor advancement and replay identical responses without revisiting upstream pages."""
        binding = hashlib.sha256(
            json.dumps([scope, arguments.model_dump(exclude={"cursor", "profile"})], sort_keys=True).encode()
        ).hexdigest()
        session, step = arguments.cursor.split(":") if arguments.cursor else (uuid.uuid4().hex, "0")
        database = None
        try:
            database = sqlite3.connect(self.path, timeout=0)
            self.path.chmod(0o600)
            database.execute("PRAGMA foreign_keys=ON")
            database.execute("PRAGMA secure_delete=ON")
            database.execute("BEGIN IMMEDIATE")
            database.execute(
                "CREATE TABLE IF NOT EXISTS sessions "
                "(id TEXT PRIMARY KEY, binding TEXT, expires REAL, state TEXT, step INTEGER)"
            )
            database.execute(
                "CREATE TABLE IF NOT EXISTS pages (session TEXT REFERENCES sessions(id) ON DELETE CASCADE, "
                "step INTEGER, result TEXT, PRIMARY KEY(session, step))"
            )
            database.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
            database.commit()
            database.execute("BEGIN IMMEDIATE")
            row = database.execute("SELECT binding, state, step FROM sessions WHERE id=?", (session,)).fetchone()
            if arguments.cursor:
                if not row or row[0] != binding or int(step) > row[2]:
                    raise EvoError(
                        "INVALID_CURSOR",
                        "This search cursor expired, is unavailable, or belongs to different arguments.",
                        "Reuse all search arguments, target and state directory, or start a new search.",
                    )
                cached = database.execute(
                    "SELECT result FROM pages WHERE session=? AND step=?", (session, int(step))
                ).fetchone()
                if cached:
                    database.commit()
                    return json.loads(cached[0])
                state = json.loads(row[1])
            else:
                if database.execute("SELECT count(*) FROM sessions").fetchone()[0] >= MAX_SESSIONS:
                    raise EvoError(
                        "SEARCH_CACHE_FULL",
                        "There are 32 active searches.",
                        "Continue an existing search or wait for its 30-minute expiry.",
                    )
                state = {}
                database.execute(
                    "INSERT INTO sessions VALUES (?, ?, ?, '{}', 0)", (session, binding, time.time() + SESSION_SECONDS)
                )
            result = advance(state, arguments)
            if step == "0" and not result["has_more"]:
                database.execute("DELETE FROM sessions WHERE id=?", (session,))
                database.commit()
                return result
            if result["has_more"]:
                result["next_cursor"] = f"{session}:{int(step) + 1}"
            database.execute(
                "UPDATE sessions SET state=?, step=? WHERE id=?",
                (json.dumps(state), int(step) + int(result["has_more"]), session),
            )
            database.execute("INSERT INTO pages VALUES (?, ?, ?)", (session, int(step), json.dumps(result)))
            database.commit()
            return result
        except sqlite3.Error as error:
            busy = error.sqlite_errorcode in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
            raise EvoError(
                "SEARCH_CACHE_BUSY" if busy else "SEARCH_CACHE_UNAVAILABLE",
                "Search continuation storage is busy." if busy else "Search continuation storage is unavailable.",
                "Retry this same read-only cursor." if busy else "Check local storage and permissions.",
                retryable=busy,
            ) from None
        finally:
            if database is not None:
                database.close()
