"""Tests for sender identity validation in external messages."""

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from penta.models.agent_type import AgentType
from penta.models.message_sender import RESERVED_SENDER_NAMES
from penta_mcp.server import send_to_group_chat


class TestMCPSenderValidation:
    """MCP tool should reject or rename reserved sender names."""

    def test_reserved_name_gets_suffix(self):
        with patch("penta_mcp.server.PentaDB") as MockDB:
            mock_db = MagicMock()
            MockDB.return_value = mock_db

            send_to_group_chat("/tmp/project", "hi", "User")

            mock_db.append_message.assert_called_once_with("User (external)", "hi")

    def test_agent_name_gets_suffix(self):
        with patch("penta_mcp.server.PentaDB") as MockDB:
            mock_db = MagicMock()
            MockDB.return_value = mock_db

            send_to_group_chat("/tmp/project", "hi", "Claude")

            mock_db.append_message.assert_called_once_with("Claude (external)", "hi")

    def test_case_insensitive(self):
        with patch("penta_mcp.server.PentaDB") as MockDB:
            mock_db = MagicMock()
            MockDB.return_value = mock_db

            send_to_group_chat("/tmp/project", "hi", "USER")

            mock_db.append_message.assert_called_once_with("USER (external)", "hi")

    def test_normal_name_unchanged(self):
        with patch("penta_mcp.server.PentaDB") as MockDB:
            mock_db = MagicMock()
            MockDB.return_value = mock_db

            send_to_group_chat("/tmp/project", "hi", "Alice")

            mock_db.append_message.assert_called_once_with("Alice", "hi")

    def test_empty_name_rejected(self):
        result = send_to_group_chat("/tmp", "hi", "")
        assert "error" in result.lower()

    def test_whitespace_name_rejected(self):
        result = send_to_group_chat("/tmp", "hi", "   ")
        assert "error" in result.lower()

    def test_all_reserved_names_covered(self):
        """Every reserved name should get the (external) suffix."""
        for name in RESERVED_SENDER_NAMES | AgentType.all_names():
            with patch("penta_mcp.server.PentaDB") as MockDB:
                mock_db = MagicMock()
                MockDB.return_value = mock_db

                send_to_group_chat("/tmp/project", "test", name)

                called_name = mock_db.append_message.call_args[0][0]
                assert called_name.endswith("(external)"), f"{name} was not renamed"
