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
from penta.services.db_schema import CREATE_TABLES_SQL, db_path_for
from penta.utils import utc_iso_now

mcp = FastMCP("penta-group-chat")


def _open_db(directory: str) -> sqlite3.Connection:
    """Open a sync sqlite3 connection for the given project directory."""
    path = db_path_for(Path(directory))
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(CREATE_TABLES_SQL)
    return conn


@mcp.tool()
def get_group_chat(directory: str, last_n: int = 50) -> str:
    """Get recent messages from the Penta group chat for the given project directory."""
    path = db_path_for(Path(directory))
    if not path.exists():
        return f"No group chat messages yet for {directory}."

    conn = _open_db(directory)
    try:
        rows = conn.execute(
            "SELECT id, sender, text, timestamp FROM messages "
            "ORDER BY id DESC LIMIT ?",
            (last_n,),
        ).fetchall()[::-1]
        if not rows:
            return f"No group chat messages yet for {directory}."
        return "\n".join(f"[{ts}] {sender}: {text}" for _, sender, text, ts in rows)
    finally:
        conn.close()


@mcp.tool()
def send_to_group_chat(directory: str, message: str, your_name: str) -> str:
    """Post a message to the Penta group chat. All agents and the user can see it."""
    if not your_name or not your_name.strip():
        return "Error: your_name is required."
    name = sanitize_external_name(your_name.strip(), AgentType.all_names())
    conn = _open_db(directory)
    try:
        conn.execute(
            "INSERT INTO messages (sender, text, timestamp) VALUES (?, ?, ?)",
            (name, message, utc_iso_now()),
        )
        conn.commit()
        return "Message posted to group chat."
    finally:
        conn.close()


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
