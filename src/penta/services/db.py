from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import aiosqlite

from penta.services.db_schema import CREATE_TABLES_SQL, db_path_for, default_storage_root

log = logging.getLogger(__name__)


class PentaDB:
    MAX_MESSAGES = 2000

    def __init__(
        self, directory: Path, storage_root: Path | None = None,
    ) -> None:
        self._directory = directory.resolve()
        self._storage_root = storage_root or default_storage_root()
        self._db_path = db_path_for(self._directory, self._storage_root)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: aiosqlite.Connection | None = None
        self._last_data_version: int = 0
        self._last_seen_id: int = 0
        self._on_external_message: Callable[[str, str], None] | None = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.executescript(CREATE_TABLES_SQL)
        self._last_data_version = await self._get_data_version()
        self._last_seen_id = await self._get_max_id()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()

    # -- Messages --

    async def append_message(self, sender: str, text: str) -> int:
        cur = await self._conn.execute(
            "INSERT INTO messages (sender, text, timestamp) VALUES (?, ?, ?)",
            (sender, text, datetime.now(timezone.utc).isoformat()),
        )
        await self._conn.commit()
        self._last_seen_id = cur.lastrowid
        self._last_data_version = await self._get_data_version()
        return cur.lastrowid

    async def get_messages(self, limit: int = 2000) -> list[tuple[int, str, str, str]]:
        """Returns (id, sender, text, timestamp) tuples, oldest first."""
        cur = await self._conn.execute(
            "SELECT id, sender, text, timestamp FROM messages "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        return rows[::-1]

    async def check_external_changes(self) -> list[tuple[int, str, str, str]]:
        """Returns new rows written by other connections since last check."""
        dv = await self._get_data_version()
        if dv == self._last_data_version:
            return []
        self._last_data_version = dv
        cur = await self._conn.execute(
            "SELECT id, sender, text, timestamp FROM messages WHERE id > ? ORDER BY id",
            (self._last_seen_id,),
        )
        rows = await cur.fetchall()
        if rows:
            self._last_seen_id = rows[-1][0]
        return rows

    async def compact(self, max_messages: int | None = None) -> None:
        limit = max_messages or self.MAX_MESSAGES
        await self._conn.execute(
            "DELETE FROM messages WHERE id NOT IN "
            "(SELECT id FROM messages ORDER BY id DESC LIMIT ?)",
            (limit,),
        )
        await self._conn.commit()

    # -- Sessions --

    async def save_session(self, agent_name: str, session_id: str) -> None:
        await self._conn.execute(
            "INSERT OR REPLACE INTO sessions (agent_name, session_id) VALUES (?, ?)",
            (agent_name, session_id),
        )
        await self._conn.commit()

    async def load_session(self, agent_name: str) -> str | None:
        cur = await self._conn.execute(
            "SELECT session_id FROM sessions WHERE agent_name = ?",
            (agent_name,),
        )
        row = await cur.fetchone()
        return row[0] if row else None

    # -- External change polling --

    def set_external_message_callback(
        self, callback: Callable[[str, str], None]
    ) -> None:
        self._on_external_message = callback

    async def poll_external_messages(self) -> None:
        """Background task: check for external writes every 500ms."""
        while True:
            await asyncio.sleep(0.5)
            try:
                rows = await self.check_external_changes()
            except Exception:
                log.exception(
                    "poll_external_messages: check failed, will retry"
                )
                continue
            if self._on_external_message:
                for _, sender, text, _ in rows:
                    self._on_external_message(sender, text)

    # -- Internal --

    async def _get_data_version(self) -> int:
        cur = await self._conn.execute("PRAGMA data_version")
        row = await cur.fetchone()
        return row[0]

    async def _get_max_id(self) -> int:
        cur = await self._conn.execute("SELECT MAX(id) FROM messages")
        row = await cur.fetchone()
        return row[0] or 0
