"""Tests for sender identity validation in external messages."""

import sqlite3
from pathlib import Path

import pytest

from penta.models.agent_type import AgentType
from penta.models.message_sender import RESERVED_SENDER_NAMES
from penta.services.db import PentaDB
from penta_mcp.server import send_to_group_chat


def _get_senders(directory: Path) -> list[str]:
    """Read sender names directly from the DB file."""
    path = PentaDB.db_path_for(directory)
    conn = sqlite3.connect(str(path))
    rows = conn.execute("SELECT sender FROM messages ORDER BY id").fetchall()
    conn.close()
    return [r[0] for r in rows]


class TestMCPSenderValidation:
    """MCP tool should reject or rename reserved sender names."""

    def test_reserved_name_gets_suffix(self, tmp_path: Path):
        send_to_group_chat(str(tmp_path), "hi", "User")
        assert _get_senders(tmp_path) == ["User (external)"]

    def test_agent_name_gets_suffix(self, tmp_path: Path):
        send_to_group_chat(str(tmp_path), "hi", "Claude")
        assert _get_senders(tmp_path) == ["Claude (external)"]

    def test_case_insensitive(self, tmp_path: Path):
        send_to_group_chat(str(tmp_path), "hi", "USER")
        assert _get_senders(tmp_path) == ["USER (external)"]

    def test_normal_name_unchanged(self, tmp_path: Path):
        send_to_group_chat(str(tmp_path), "hi", "Alice")
        assert _get_senders(tmp_path) == ["Alice"]

    def test_empty_name_rejected(self):
        result = send_to_group_chat("/tmp", "hi", "")
        assert "error" in result.lower()

    def test_whitespace_name_rejected(self):
        result = send_to_group_chat("/tmp", "hi", "   ")
        assert "error" in result.lower()

    def test_all_reserved_names_covered(self, tmp_path: Path):
        """Every reserved name should get the (external) suffix."""
        for name in RESERVED_SENDER_NAMES | AgentType.all_names():
            send_to_group_chat(str(tmp_path), "test", name)
        senders = _get_senders(tmp_path)
        for sender in senders:
            assert sender.endswith("(external)"), f"{sender} was not renamed"
