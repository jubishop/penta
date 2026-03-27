"""Standalone MCP server for Penta group chat.

Provides tools for external agents to read/write the shared chat database.
Installed as `penta-mcp-server` entry point.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("penta-group-chat")


def _db_path(directory: str) -> Path:
    resolved = str(Path(directory).resolve())
    dir_hash = hashlib.sha256(resolved.encode()).hexdigest()
    return Path.home() / ".local" / "share" / "penta" / "chats" / dir_hash / "penta.db"


def _ensure_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript("""
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
    return conn


@mcp.tool()
def get_group_chat(directory: str, last_n: int = 50) -> str:
    """Get recent messages from the Penta group chat for the given project directory."""
    path = _db_path(directory)
    if not path.exists():
        return f"No group chat messages yet for {directory}."

    conn = _ensure_db(path)
    rows = conn.execute(
        "SELECT sender, text, timestamp FROM messages ORDER BY id DESC LIMIT ?",
        (last_n,),
    ).fetchall()[::-1]
    conn.close()

    if not rows:
        return f"No group chat messages yet for {directory}."

    return "\n".join(f"[{ts}] {sender}: {text}" for sender, text, ts in rows)


@mcp.tool()
def send_to_group_chat(directory: str, message: str, your_name: str) -> str:
    """Post a message to the Penta group chat. All agents and the user can see it."""
    path = _db_path(directory)
    conn = _ensure_db(path)
    conn.execute(
        "INSERT INTO messages (sender, text, timestamp) VALUES (?, ?, ?)",
        (your_name, message, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()
    return "Message posted to group chat."


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
