"""Standalone MCP server for Penta group chat.

Provides tools for external agents to read/write the shared chat database.
Installed as `penta-mcp-server` entry point.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from penta.models.agent_type import AgentType
from penta.models.message_sender import sanitize_external_name
from penta.services.db_schema import (
    CREATE_TABLES_SQL,
    SCHEMA_VERSION,
    db_path_for,
    ensure_default_conversation_sync,
    run_migrations_sync,
)
from penta.utils import utc_iso_now

mcp = FastMCP("penta-group-chat")


def _open_db(directory: str) -> sqlite3.Connection:
    """Open a sync sqlite3 connection for the given project directory."""
    path = db_path_for(Path(directory))
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")

    version = conn.execute("PRAGMA user_version").fetchone()[0]

    if version == 0:
        is_existing = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='messages'"
        ).fetchone()[0] > 0

        if is_existing:
            run_migrations_sync(conn)
        else:
            conn.executescript(CREATE_TABLES_SQL)
            ensure_default_conversation_sync(conn)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()
    else:
        run_migrations_sync(conn)

    return conn


def _default_conversation_id(conn: sqlite3.Connection) -> int:
    """Return the most recently updated conversation id."""
    row = conn.execute(
        "SELECT id FROM conversations ORDER BY updated_at DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else 1


def _resolve_conversation_id(conn: sqlite3.Connection, conversation_id: int | None) -> int | str:
    """Resolve and validate a conversation id. Returns the id or an error string."""
    cid = conversation_id if conversation_id is not None else _default_conversation_id(conn)
    if conversation_id is not None:
        row = conn.execute(
            "SELECT COUNT(*) FROM conversations WHERE id = ?", (cid,)
        ).fetchone()
        if row[0] == 0:
            return f"Error: conversation {cid} does not exist."
    return cid


@mcp.tool()
def list_conversations(directory: str) -> str:
    """List all conversations for the given project directory."""
    path = db_path_for(Path(directory))
    if not path.exists():
        return f"No conversations yet for {directory}."

    conn = _open_db(directory)
    try:
        rows = conn.execute(
            "SELECT id, title, created_at, updated_at FROM conversations "
            "ORDER BY updated_at DESC"
        ).fetchall()
        if not rows:
            return f"No conversations yet for {directory}."
        lines = [f"id={cid} | {title} | updated: {updated}" for cid, title, _, updated in rows]
        return "\n".join(lines)
    finally:
        conn.close()


@mcp.tool()
def get_group_chat(
    directory: str, last_n: int = 50, conversation_id: int | None = None,
) -> str:
    """Get recent messages from the Penta group chat for the given project directory.

    If conversation_id is not provided, reads from the most recently updated conversation.
    """
    path = db_path_for(Path(directory))
    if not path.exists():
        return f"No group chat messages yet for {directory}."

    conn = _open_db(directory)
    try:
        resolved = _resolve_conversation_id(conn, conversation_id)
        if isinstance(resolved, str):
            return resolved
        rows = conn.execute(
            "SELECT id, sender, text, timestamp FROM messages "
            "WHERE conversation_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (resolved, last_n),
        ).fetchall()[::-1]
        if not rows:
            return f"No group chat messages yet for {directory}."
        return "\n".join(f"[{ts}] {sender}: {text}" for _, sender, text, ts in rows)
    finally:
        conn.close()


@mcp.tool()
def send_to_group_chat(
    directory: str,
    message: str,
    your_name: str,
    conversation_id: int | None = None,
) -> str:
    """Post a message to the Penta group chat. All agents and the user can see it.

    If conversation_id is not provided, posts to the most recently updated conversation.
    """
    if not your_name or not your_name.strip():
        return "Error: your_name is required."
    name = sanitize_external_name(your_name.strip(), AgentType.all_names())
    conn = _open_db(directory)
    try:
        resolved = _resolve_conversation_id(conn, conversation_id)
        if isinstance(resolved, str):
            return resolved
        now = utc_iso_now()
        conn.execute(
            "INSERT INTO messages (conversation_id, sender, text, timestamp) VALUES (?, ?, ?, ?)",
            (resolved, name, message, now),
        )
        conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (now, resolved),
        )
        conn.commit()
        return "Message posted to group chat."
    finally:
        conn.close()


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
