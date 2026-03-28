"""Shared DB schema and path helpers — stdlib only, no async dependencies."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

CREATE_TABLES_SQL = """
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
"""


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
