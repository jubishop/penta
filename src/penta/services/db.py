from __future__ import annotations

import hashlib
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


def _default_storage_root() -> Path:
    """Respect PENTA_DATA_DIR, fall back to ~/.local/share."""
    override = os.environ.get("PENTA_DATA_DIR")
    if override:
        return Path(override)
    return Path.home() / ".local" / "share"


class PentaDB:
    MAX_MESSAGES = 2000

    def __init__(
        self, directory: Path, storage_root: Path | None = None,
    ) -> None:
        self._directory = directory.resolve()
        self._storage_root = storage_root or _default_storage_root()
        self._db_path = self._resolve_path(self._directory, self._storage_root)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._create_tables()
        self._last_data_version: int = self._get_data_version()
        self._last_seen_id: int = self._get_max_id()
        self._on_external_message: Callable[[str, str], None] | None = None

    def close(self) -> None:
        self._conn.close()

    # -- Messages --

    def append_message(self, sender: str, text: str) -> int:
        cur = self._conn.execute(
            "INSERT INTO messages (sender, text, timestamp) VALUES (?, ?, ?)",
            (sender, text, datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()
        self._last_seen_id = cur.lastrowid
        self._last_data_version = self._get_data_version()
        return cur.lastrowid

    def get_messages(self, limit: int = 2000) -> list[tuple[int, str, str, str]]:
        """Returns (id, sender, text, timestamp) tuples, oldest first."""
        rows = self._conn.execute(
            "SELECT id, sender, text, timestamp FROM messages "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return rows[::-1]

    def check_external_changes(self) -> list[tuple[int, str, str, str]]:
        """Returns new rows written by other connections since last check."""
        dv = self._get_data_version()
        if dv == self._last_data_version:
            return []
        self._last_data_version = dv
        rows = self._conn.execute(
            "SELECT id, sender, text, timestamp FROM messages WHERE id > ? ORDER BY id",
            (self._last_seen_id,),
        ).fetchall()
        if rows:
            self._last_seen_id = rows[-1][0]
        return rows

    def compact(self, max_messages: int | None = None) -> None:
        limit = max_messages or self.MAX_MESSAGES
        self._conn.execute(
            "DELETE FROM messages WHERE id NOT IN "
            "(SELECT id FROM messages ORDER BY id DESC LIMIT ?)",
            (limit,),
        )
        self._conn.commit()

    # -- Sessions --

    def save_session(self, agent_name: str, session_id: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO sessions (agent_name, session_id) VALUES (?, ?)",
            (agent_name, session_id),
        )
        self._conn.commit()

    def load_session(self, agent_name: str) -> str | None:
        row = self._conn.execute(
            "SELECT session_id FROM sessions WHERE agent_name = ?",
            (agent_name,),
        ).fetchone()
        return row[0] if row else None

    # -- External change polling --

    def set_external_message_callback(
        self, callback: Callable[[str, str], None]
    ) -> None:
        self._on_external_message = callback

    async def poll_external_messages(self) -> None:
        """Background task: check for external writes every 500ms."""
        import asyncio

        while True:
            await asyncio.sleep(0.5)
            rows = self.check_external_changes()
            if self._on_external_message:
                for _, sender, text, _ in rows:
                    self._on_external_message(sender, text)

    # -- Internal --

    @staticmethod
    def _resolve_path(directory: Path, storage_root: Path) -> Path:
        dir_hash = hashlib.sha256(str(directory).encode()).hexdigest()
        return storage_root / "penta" / "chats" / dir_hash / "penta.db"

    @staticmethod
    def db_path_for(directory: Path, storage_root: Path | None = None) -> Path:
        """Public helper for MCP server to find the DB path."""
        resolved = directory.resolve()
        root = storage_root or _default_storage_root()
        return PentaDB._resolve_path(resolved, root)

    def _create_tables(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender TEXT NOT NULL,
                text TEXT NOT NULL,
                timestamp TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                agent_name TEXT PRIMARY KEY,
                session_id TEXT NOT NULL
            );
        """)

    def _get_data_version(self) -> int:
        return self._conn.execute("PRAGMA data_version").fetchone()[0]

    def _get_max_id(self) -> int:
        row = self._conn.execute("SELECT MAX(id) FROM messages").fetchone()
        return row[0] or 0
