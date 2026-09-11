"""Operator-supplied contact names, private and scoped to an exact deployment."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from evoctl.config import private_directory
from evoctl.worker import EvoError


class ContactNames:
    """Keep confirmed names independently of Evolution's replaceable profile names."""

    def __init__(self, directory: Path) -> None:
        """Share private name storage across CLI and MCP without changing remote contacts."""
        self.path = directory.resolve() / "contact-names.sqlite3"

    @contextmanager
    def connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        """Close every connection and report storage failures without dropping saved names silently."""
        try:
            if write:
                private_directory(self.path.parent)
            uri = self.path.as_uri() + ("?mode=rwc" if write else "?mode=ro")
            with closing(sqlite3.connect(uri, uri=True, timeout=1)) as database:
                if write:
                    self.path.chmod(0o600)
                    database.execute("PRAGMA secure_delete=ON")
                with database:
                    yield database
        except sqlite3.Error as error:
            raise EvoError(
                "CONTACT_NAMES_UNAVAILABLE",
                "Saved contact names could not be accessed.",
                "Check local storage and permissions; retry if another process is updating names.",
            ) from error

    def read(self, scope: str) -> dict[str, str]:
        """Return only names belonging to the current target; first-use searches create no name database."""
        if not self.path.exists():
            return {}
        with self.connection() as database:
            return dict(database.execute("SELECT jid, name FROM names WHERE scope=?", (scope,)))

    def set(self, scope: str, jid: str, name: str) -> None:
        """Atomically replace one confirmed name, or remove it when explicitly given an empty name."""
        with self.connection(write=True) as database:
            database.execute(
                "CREATE TABLE IF NOT EXISTS names (scope TEXT NOT NULL, jid TEXT NOT NULL, "
                "name TEXT NOT NULL, PRIMARY KEY (scope, jid))"
            )
            if name:
                database.execute(
                    "INSERT INTO names VALUES (?, ?, ?) ON CONFLICT (scope, jid) DO UPDATE SET name=excluded.name",
                    (scope, jid, name),
                )
            else:
                database.execute("DELETE FROM names WHERE scope=? AND jid=?", (scope, jid))
