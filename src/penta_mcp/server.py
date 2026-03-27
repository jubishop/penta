"""Standalone MCP server for Penta group chat.

Provides tools for external agents to read/write the shared chat database.
Installed as `penta-mcp-server` entry point.
"""

from __future__ import annotations

from pathlib import Path

from mcp.server.fastmcp import FastMCP

from penta.services.db import PentaDB

mcp = FastMCP("penta-group-chat")


@mcp.tool()
def get_group_chat(directory: str, last_n: int = 50) -> str:
    """Get recent messages from the Penta group chat for the given project directory."""
    path = PentaDB.db_path_for(Path(directory))
    if not path.exists():
        return f"No group chat messages yet for {directory}."

    db = PentaDB(Path(directory))
    try:
        rows = db.get_messages(limit=last_n)
        if not rows:
            return f"No group chat messages yet for {directory}."
        return "\n".join(f"[{ts}] {sender}: {text}" for _, sender, text, ts in rows)
    finally:
        db.close()


_RESERVED_NAMES = {"user", "shell", "system", "claude", "codex", "gemini"}


@mcp.tool()
def send_to_group_chat(directory: str, message: str, your_name: str) -> str:
    """Post a message to the Penta group chat. All agents and the user can see it."""
    if not your_name or not your_name.strip():
        return "Error: your_name is required."
    name = your_name.strip()
    if name.lower() in _RESERVED_NAMES:
        name = f"{name} (external)"
    db = PentaDB(Path(directory))
    try:
        db.append_message(name, message)
        return "Message posted to group chat."
    finally:
        db.close()


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
