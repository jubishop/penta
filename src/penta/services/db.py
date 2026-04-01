from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Callable

import aiosqlite

from penta.services.db_schema import (
    CREATE_TABLES_SQL,
    SCHEMA_VERSION,
    db_path_for,
    default_storage_root,
    ensure_default_conversation,
    run_migrations,
)
from penta.utils import utc_iso_now

log = logging.getLogger(__name__)


class PentaDB:
    MAX_MESSAGES: int = 2000

    def __init__(
        self,
        directory: Path,
        storage_root: Path | None = None,
        *,
        in_memory: bool = False,
    ) -> None:
        self._directory = directory.resolve()
        self._in_memory = in_memory
        if in_memory:
            self._db_path: Path | None = None
        else:
            self._storage_root = storage_root or default_storage_root()
            self._db_path = db_path_for(self._directory, self._storage_root)
        self._conn: aiosqlite.Connection | None = None
        self._conversation_id: int = 1
        self._last_data_version: int = 0
        self._last_seen_id: int = 0
        self._local_ids: set[int] = set()
        self._on_external_message: Callable[[str, str], None] | None = None
        self._polling_paused: bool = False

    @property
    def _db(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("database not connected — call connect() first")
        return self._conn

    @property
    def conversation_id(self) -> int:
        return self._conversation_id

    async def connect(self) -> None:
        if self._conn is not None:
            return
        if self._in_memory:
            self._conn = await aiosqlite.connect(":memory:")
        else:
            assert self._db_path is not None
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = await aiosqlite.connect(self._db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.execute("PRAGMA foreign_keys=ON")

        cur = await self._db.execute("PRAGMA user_version")
        row = await cur.fetchone()
        assert row is not None
        version = row[0]

        if version == 0:
            # Check if this is a pre-migration DB (has old messages table)
            cur = await self._db.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='table' AND name='messages'"
            )
            count_row = await cur.fetchone()
            assert count_row is not None
            is_existing_db = count_row[0] > 0

            if is_existing_db:
                # Pre-migration DB — run migrations to upgrade schema
                await run_migrations(self._db)
            else:
                # Fresh DB — create current schema directly
                await self._db.executescript(CREATE_TABLES_SQL)
                await ensure_default_conversation(self._db)
                await self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                await self._db.commit()
        else:
            # Already versioned — run any pending migrations
            await run_migrations(self._db)

        # Set active conversation to most recently updated
        cur = await self._db.execute(
            "SELECT id FROM conversations ORDER BY updated_at DESC LIMIT 1"
        )
        row = await cur.fetchone()
        if row:
            self._conversation_id = row[0]

        self._last_data_version = await self._get_data_version()
        self._last_seen_id = await self._get_max_id()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    # -- Conversations --

    async def create_conversation(self, title: str) -> int:
        now = utc_iso_now()
        cur = await self._db.execute(
            "INSERT INTO conversations (title, created_at, updated_at) VALUES (?, ?, ?)",
            (title, now, now),
        )
        await self._db.commit()
        rowid = cur.lastrowid
        assert rowid is not None
        return rowid

    async def list_conversations(self) -> list:
        """Returns (id, title, created_at, updated_at) rows, most recently updated first."""
        cur = await self._db.execute(
            "SELECT id, title, created_at, updated_at FROM conversations "
            "ORDER BY updated_at DESC"
        )
        return list(await cur.fetchall())

    async def _conversation_exists(self, conversation_id: int) -> bool:
        cur = await self._db.execute(
            "SELECT COUNT(*) FROM conversations WHERE id = ?", (conversation_id,)
        )
        row = await cur.fetchone()
        assert row is not None
        return row[0] > 0

    async def delete_conversation(self, conversation_id: int) -> None:
        if not await self._conversation_exists(conversation_id):
            raise ValueError(f"Conversation {conversation_id} does not exist")
        await self._db.execute(
            "DELETE FROM messages WHERE conversation_id = ?", (conversation_id,)
        )
        await self._db.execute(
            "DELETE FROM sessions WHERE conversation_id = ?", (conversation_id,)
        )
        await self._db.execute(
            "DELETE FROM conversations WHERE id = ?", (conversation_id,)
        )
        await self._db.commit()

    async def rename_conversation(self, conversation_id: int, title: str) -> None:
        if not await self._conversation_exists(conversation_id):
            raise ValueError(f"Conversation {conversation_id} does not exist")
        await self._db.execute(
            "UPDATE conversations SET title = ? WHERE id = ?",
            (title, conversation_id),
        )
        await self._db.commit()

    async def set_conversation(self, conversation_id: int) -> None:
        """Switch the active conversation. Resets external-change tracking state."""
        cur = await self._db.execute(
            "SELECT COUNT(*) FROM conversations WHERE id = ?", (conversation_id,)
        )
        row = await cur.fetchone()
        assert row is not None
        if row[0] == 0:
            raise ValueError(f"Conversation {conversation_id} does not exist")
        self._conversation_id = conversation_id
        self._local_ids.clear()
        self._last_seen_id = await self._get_max_id()
        self._last_data_version = await self._get_data_version()

    # -- Messages --

    async def append_message(self, sender: str, text: str) -> int:
        cur = await self._db.execute(
            "INSERT INTO messages (conversation_id, sender, text, timestamp) VALUES (?, ?, ?, ?)",
            (self._conversation_id, sender, text, utc_iso_now()),
        )
        now = utc_iso_now()
        await self._db.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (now, self._conversation_id),
        )
        await self._db.commit()
        rowid = cur.lastrowid
        assert rowid is not None
        self._local_ids.add(rowid)
        return rowid

    async def get_messages(self, limit: int = 2000) -> list:
        """Returns (id, sender, text, timestamp) rows, oldest first."""
        cur = await self._db.execute(
            "SELECT id, sender, text, timestamp FROM messages "
            "WHERE conversation_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (self._conversation_id, limit),
        )
        rows = list(await cur.fetchall())
        return rows[::-1]

    async def check_external_changes(self) -> list:
        """Returns new rows written by other connections since last check."""
        dv = await self._get_data_version()
        if dv == self._last_data_version:
            return []
        self._last_data_version = dv
        cur = await self._db.execute(
            "SELECT id, sender, text, timestamp FROM messages "
            "WHERE conversation_id = ? AND id > ? ORDER BY id",
            (self._conversation_id, self._last_seen_id),
        )
        rows = list(await cur.fetchall())
        if rows:
            self._last_seen_id = rows[-1][0]
        # Filter out rows we wrote locally; clean up stale tracked IDs
        external = [r for r in rows if r[0] not in self._local_ids]
        self._local_ids = {lid for lid in self._local_ids if lid > self._last_seen_id}
        return external

    async def compact(self, max_messages: int | None = None) -> None:
        limit = max_messages or self.MAX_MESSAGES
        await self._db.execute(
            "DELETE FROM messages WHERE conversation_id = ? AND id NOT IN "
            "(SELECT id FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT ?)",
            (self._conversation_id, self._conversation_id, limit),
        )
        await self._db.commit()

    # -- Sessions --

    async def save_session(self, agent_name: str, session_id: str) -> None:
        await self._db.execute(
            "INSERT OR REPLACE INTO sessions (agent_name, conversation_id, session_id) "
            "VALUES (?, ?, ?)",
            (agent_name, self._conversation_id, session_id),
        )
        await self._db.commit()

    async def load_session(self, agent_name: str) -> str | None:
        cur = await self._db.execute(
            "SELECT session_id FROM sessions WHERE agent_name = ? AND conversation_id = ?",
            (agent_name, self._conversation_id),
        )
        row = await cur.fetchone()
        return row[0] if row else None

    # -- External change polling --

    def set_external_message_callback(
        self, callback: Callable[[str, str], None],
    ) -> None:
        self._on_external_message = callback

    def pause_polling(self) -> None:
        """Pause external change polling. The poll loop skips checks while paused."""
        self._polling_paused = True

    def resume_polling(self) -> None:
        """Resume external change polling."""
        self._polling_paused = False

    async def poll_external_messages(self) -> None:
        """Background task: check for external writes every 500ms."""
        while True:
            await asyncio.sleep(0.5)
            if self._conn is None:
                log.info("poll_external_messages: DB closed, stopping")
                return
            if self._polling_paused:
                continue
            try:
                rows = await self.check_external_changes()
            except Exception:
                if self._conn is None:
                    log.info("poll_external_messages: DB closed, stopping")
                    return
                log.exception(
                    "poll_external_messages: check failed, will retry"
                )
                continue
            if self._on_external_message and not self._polling_paused:
                for _, sender, text, _ in rows:
                    try:
                        self._on_external_message(sender, text)
                    except Exception:
                        log.exception(
                            "poll_external_messages: callback failed for message from %s",
                            sender,
                        )

    # -- Internal --

    async def _get_data_version(self) -> int:
        cur = await self._db.execute("PRAGMA data_version")
        row = await cur.fetchone()
        assert row is not None
        return row[0]

    async def _get_max_id(self) -> int:
        cur = await self._db.execute(
            "SELECT MAX(id) FROM messages WHERE conversation_id = ?",
            (self._conversation_id,),
        )
        row = await cur.fetchone()
        assert row is not None
        return row[0] or 0
