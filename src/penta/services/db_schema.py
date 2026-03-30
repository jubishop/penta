"""Shared DB schema, path helpers, and migration framework.

Async migrations use aiosqlite; sync variants use stdlib sqlite3.
Both share the same SQL constants and migration logic.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from penta.utils import utc_iso_now

if TYPE_CHECKING:
    import aiosqlite

# ---------------------------------------------------------------------------
# Current schema (for fresh databases)
# ---------------------------------------------------------------------------

CREATE_TABLES_SQL = """
    CREATE TABLE IF NOT EXISTS conversations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id INTEGER NOT NULL DEFAULT 1 REFERENCES conversations(id),
        sender TEXT NOT NULL,
        text TEXT NOT NULL,
        timestamp TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_messages_conversation
        ON messages(conversation_id);
    CREATE TABLE IF NOT EXISTS sessions (
        agent_name TEXT NOT NULL,
        conversation_id INTEGER NOT NULL DEFAULT 1 REFERENCES conversations(id),
        session_id TEXT NOT NULL,
        PRIMARY KEY (agent_name, conversation_id)
    );
    CREATE INDEX IF NOT EXISTS idx_sessions_conversation
        ON sessions(conversation_id);
"""

# ---------------------------------------------------------------------------
# Migration 1: add conversations table, scope messages and sessions
# ---------------------------------------------------------------------------

# Each step is idempotent so a partial failure can be safely re-run.

_MIGRATE_V1_CREATE_CONVERSATIONS = """
    CREATE TABLE IF NOT EXISTS conversations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
"""

_MIGRATE_V1_INDEXES = """
    CREATE INDEX IF NOT EXISTS idx_messages_conversation
        ON messages(conversation_id);
    CREATE INDEX IF NOT EXISTS idx_sessions_conversation
        ON sessions(conversation_id);
"""


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Check if a column exists on a table (sync)."""
    cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(c[1] == column for c in cols)


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    """Check if a table exists (sync)."""
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row[0] > 0


def _rebuild_sessions_sync(conn: sqlite3.Connection) -> None:
    """Recreate sessions table with composite PK. Handles all partial-run states."""
    has_old = _has_table(conn, "sessions")
    has_new = _has_table(conn, "sessions_new")

    if has_old and not has_new:
        # Normal case: old table exists, create new, copy, drop, rename
        conn.executescript("""
            CREATE TABLE sessions_new (
                agent_name TEXT NOT NULL,
                conversation_id INTEGER NOT NULL DEFAULT 1 REFERENCES conversations(id),
                session_id TEXT NOT NULL,
                PRIMARY KEY (agent_name, conversation_id)
            );
            INSERT OR IGNORE INTO sessions_new (agent_name, conversation_id, session_id)
                SELECT agent_name, 1, session_id FROM sessions;
            DROP TABLE sessions;
            ALTER TABLE sessions_new RENAME TO sessions;
        """)
    elif has_new and not has_old:
        # Partial run: old was dropped but rename didn't happen
        conn.executescript("ALTER TABLE sessions_new RENAME TO sessions;")
    elif has_new and has_old:
        # Partial run: new was created but old wasn't dropped yet
        conn.executescript("""
            DROP TABLE sessions;
            ALTER TABLE sessions_new RENAME TO sessions;
        """)
    # else: both missing is impossible (sessions always exists in old schema)


def _migrate_v1_sync(conn: sqlite3.Connection) -> None:
    """Add conversations table, scope messages and sessions by conversation_id."""
    now = utc_iso_now()
    row = conn.execute("SELECT MIN(timestamp) FROM messages").fetchone()
    created_at = row[0] if row[0] else now

    conn.executescript(_MIGRATE_V1_CREATE_CONVERSATIONS)

    # Idempotent: only insert default conversation if not already present
    existing = conn.execute("SELECT COUNT(*) FROM conversations WHERE id = 1").fetchone()
    if existing[0] == 0:
        conn.execute(
            "INSERT INTO conversations (id, title, created_at, updated_at) VALUES (1, ?, ?, ?)",
            ("Default", created_at, now),
        )

    # Idempotent: only add column if not already present
    if not _has_column(conn, "messages", "conversation_id"):
        conn.executescript(
            "ALTER TABLE messages ADD COLUMN conversation_id INTEGER NOT NULL DEFAULT 1 "
            "REFERENCES conversations(id);"
        )

    _rebuild_sessions_sync(conn)
    conn.executescript(_MIGRATE_V1_INDEXES)


async def _migrate_v1_async(conn: aiosqlite.Connection) -> None:
    """Async version of migration 1."""
    now = utc_iso_now()
    cur = await conn.execute("SELECT MIN(timestamp) FROM messages")
    row = await cur.fetchone()
    created_at = row[0] if row[0] else now

    await conn.executescript(_MIGRATE_V1_CREATE_CONVERSATIONS)

    cur = await conn.execute("SELECT COUNT(*) FROM conversations WHERE id = 1")
    existing = await cur.fetchone()
    if existing[0] == 0:
        await conn.execute(
            "INSERT INTO conversations (id, title, created_at, updated_at) VALUES (1, ?, ?, ?)",
            ("Default", created_at, now),
        )

    # Check if column already exists (partial re-run safety)
    cur = await conn.execute("PRAGMA table_info(messages)")
    cols = await cur.fetchall()
    has_conversation_id = any(c[1] == "conversation_id" for c in cols)
    if not has_conversation_id:
        await conn.executescript(
            "ALTER TABLE messages ADD COLUMN conversation_id INTEGER NOT NULL DEFAULT 1 "
            "REFERENCES conversations(id);"
        )

    # Rebuild sessions — handle all partial-run states
    cur = await conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='sessions'"
    )
    has_old = (await cur.fetchone())[0] > 0
    cur = await conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='sessions_new'"
    )
    has_new = (await cur.fetchone())[0] > 0

    if has_old and not has_new:
        await conn.executescript("""
            CREATE TABLE sessions_new (
                agent_name TEXT NOT NULL,
                conversation_id INTEGER NOT NULL DEFAULT 1 REFERENCES conversations(id),
                session_id TEXT NOT NULL,
                PRIMARY KEY (agent_name, conversation_id)
            );
            INSERT OR IGNORE INTO sessions_new (agent_name, conversation_id, session_id)
                SELECT agent_name, 1, session_id FROM sessions;
            DROP TABLE sessions;
            ALTER TABLE sessions_new RENAME TO sessions;
        """)
    elif has_new and not has_old:
        await conn.executescript("ALTER TABLE sessions_new RENAME TO sessions;")
    elif has_new and has_old:
        await conn.executescript("""
            DROP TABLE sessions;
            ALTER TABLE sessions_new RENAME TO sessions;
        """)

    await conn.executescript(_MIGRATE_V1_INDEXES)


# ---------------------------------------------------------------------------
# Migration registry
# ---------------------------------------------------------------------------

# Index 0 = migration 1 (brings user_version from 0 → 1).
# Append new (sync_fn, async_fn) tuples for future migrations.
_MIGRATIONS: list[
    tuple[
        Callable[[sqlite3.Connection], None],
        Callable[..., Awaitable[None]],
    ]
] = [
    (_migrate_v1_sync, _migrate_v1_async),
]

SCHEMA_VERSION = len(_MIGRATIONS)


# ---------------------------------------------------------------------------
# Run migrations
# ---------------------------------------------------------------------------

def run_migrations_sync(conn: sqlite3.Connection) -> None:
    """Run pending migrations using a sync sqlite3 connection."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, (migrate_sync, _) in enumerate(_MIGRATIONS):
        version = i + 1
        if current < version:
            # Disable FK during schema changes (SQLite requirement for DDL)
            conn.execute("PRAGMA foreign_keys=OFF")
            migrate_sync(conn)
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(f"PRAGMA user_version = {version}")
            conn.commit()


async def run_migrations(conn: aiosqlite.Connection) -> None:
    """Run pending migrations using an async aiosqlite connection."""
    cur = await conn.execute("PRAGMA user_version")
    current = (await cur.fetchone())[0]
    for i, (_, migrate_async) in enumerate(_MIGRATIONS):
        version = i + 1
        if current < version:
            # Disable FK during schema changes (SQLite requirement for DDL)
            await conn.execute("PRAGMA foreign_keys=OFF")
            await migrate_async(conn)
            await conn.execute("PRAGMA foreign_keys=ON")
            await conn.execute(f"PRAGMA user_version = {version}")
            await conn.commit()


# ---------------------------------------------------------------------------
# Ensure a default conversation exists (for fresh databases)
# ---------------------------------------------------------------------------

def ensure_default_conversation_sync(conn: sqlite3.Connection) -> None:
    """Insert the default conversation if the table is empty (fresh DB)."""
    row = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()
    if row[0] == 0:
        now = utc_iso_now()
        conn.execute(
            "INSERT INTO conversations (id, title, created_at, updated_at) VALUES (1, ?, ?, ?)",
            ("Default", now, now),
        )
        conn.commit()


async def ensure_default_conversation(conn: aiosqlite.Connection) -> None:
    """Insert the default conversation if the table is empty (fresh DB)."""
    cur = await conn.execute("SELECT COUNT(*) FROM conversations")
    row = await cur.fetchone()
    if row[0] == 0:
        now = utc_iso_now()
        await conn.execute(
            "INSERT INTO conversations (id, title, created_at, updated_at) VALUES (1, ?, ?, ?)",
            ("Default", now, now),
        )
        await conn.commit()


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def default_storage_root() -> Path:
    """Respect PENTA_DATA_DIR, fall back to ~/.local/share."""
    override = os.environ.get("PENTA_DATA_DIR")
    if override:
        return Path(override)
    return Path.home() / ".local" / "share"


def db_path_for(directory: Path, storage_root: Path | None = None) -> Path:
    """Locate the DB file for a project directory."""
    resolved = directory.resolve()
    root = storage_root or default_storage_root()
    dir_hash = hashlib.sha256(str(resolved).encode()).hexdigest()
    return root / "penta" / "chats" / dir_hash / "penta.db"
