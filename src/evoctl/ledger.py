"""Transactional send deduplication without storing message text or credentials."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from evoctl.config import private_directory
from evoctl.worker import EvoError


class SendLedger:
    """Reserve an idempotency key before a network mutation can leave the process."""

    def __init__(self, directory: Path) -> None:
        """Create a private SQLite database shared by CLI and MCP processes."""
        self.path = private_directory(directory) / "sends.sqlite3"
        with self.connection() as database:
            database.execute(
                "CREATE TABLE IF NOT EXISTS sends (scope TEXT NOT NULL, request_id TEXT NOT NULL, "
                "digest TEXT NOT NULL, state TEXT NOT NULL, result TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, "
                "PRIMARY KEY (scope, request_id))"
            )
        self.path.chmod(0o600)

    def connection(self) -> sqlite3.Connection:
        """Use SQLite locking for cross-process reservation, with a bounded busy timeout."""
        return sqlite3.connect(self.path, timeout=10)

    def reserve(self, scope: str, request_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Reserve once or return the first receipt; uncertainty can never become a retry."""
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self.connection() as database:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute(
                "SELECT digest, state, result FROM sends WHERE scope=? AND request_id=?", (scope, request_id)
            ).fetchone()
            if row:
                if row[0] != digest:
                    raise EvoError("IDEMPOTENCY_CONFLICT", "This request ID belongs to a different payload.")
                if row[1] == "accepted":
                    return json.loads(row[2])
                raise EvoError(
                    "SEND_OUTCOME_UNKNOWN",
                    "This request was already attempted and has no confirmed API receipt.",
                    "Inspect message history or delivery status; do not use a new request ID to retry blindly.",
                )
            database.execute(
                "INSERT INTO sends (scope, request_id, digest, state) VALUES (?, ?, ?, 'attempted')",
                (scope, request_id, digest),
            )
        return None

    def accepted(self, scope: str, request_id: str, result: dict[str, Any]) -> None:
        """Store only the sanitized receipt after Evolution accepts the mutation."""
        with self.connection() as database:
            database.execute(
                "UPDATE sends SET state='accepted', result=? WHERE scope=? AND request_id=?",
                (json.dumps(result), scope, request_id),
            )

    def inspect(self, scope: str, request_id: str) -> dict[str, Any]:
        """Recover a prior receipt without sending again."""
        with self.connection() as database:
            row = database.execute(
                "SELECT state, result, created_at FROM sends WHERE scope=? AND request_id=?", (scope, request_id)
            ).fetchone()
        if not row:
            raise EvoError("REQUEST_NOT_FOUND", "No local send receipt matches this request ID.")
        return {
            "request_id": request_id,
            "state": row[0],
            "receipt": json.loads(row[1]) if row[1] else None,
            "created_at": row[2],
        }
