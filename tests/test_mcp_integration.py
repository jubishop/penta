"""Integration tests: MCP server → DB → AppState.

Exercises the full path an external message takes:
  send_to_group_chat() writes to DB
  → DB polling detects the new row
  → receive_external_message() processes it
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from penta.app_state import AppState
from penta.models import AgentType, Message
from penta.services.db import PentaDB
from penta_mcp.server import get_group_chat, list_conversations, send_to_group_chat


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """A temp directory that serves as the 'project' for both MCP and app."""
    return tmp_path / "project"


@pytest.fixture
async def app_state(project_dir: Path) -> AppState:
    """An AppState backed by the same project directory as the MCP server."""
    state = AppState(project_dir)
    await state.connect()
    await state.add_agent("Claude", AgentType.CLAUDE)
    await state.add_agent("Codex", AgentType.CODEX)
    yield state
    await state.db.close()


def _mcp_write(project_dir: Path, sender: str, text: str) -> str:
    """Simulate an external MCP client writing via send_to_group_chat."""
    return send_to_group_chat(str(project_dir), text, sender)


class TestMCPToAppIntegration:
    """Full path: MCP write → DB poll → AppState receives message."""

    @pytest.mark.asyncio
    async def test_external_message_arrives_via_polling(self, app_state: AppState, project_dir: Path):
        """A message written by MCP should be picked up by the DB poller
        and delivered to receive_external_message."""
        received: list[tuple[str, str]] = []
        app_state.router.on_external_message = lambda sender, text: received.append((sender, text))

        # Start polling
        poll_task = app_state.start_external_polling(app_state.receive_external_message)

        # Write via MCP (separate DB connection, like a real external client)
        _mcp_write(project_dir, "Alice", "hello from outside")

        # Give poller time to detect (polls every 500ms)
        await asyncio.sleep(1.0)
        poll_task.cancel()

        assert len(received) == 1
        assert received[0] == ("Alice", "hello from outside")

    @pytest.mark.asyncio
    async def test_spoofed_agent_name_is_relabeled(self, app_state: AppState, project_dir: Path):
        """An external writer claiming to be 'Claude' should be relabeled."""
        received: list[tuple[str, str]] = []
        app_state.router.on_external_message = lambda sender, text: received.append((sender, text))

        poll_task = app_state.start_external_polling(app_state.receive_external_message)

        # MCP server renames "Claude" → "Claude (external)" at write time
        _mcp_write(project_dir, "Claude", "I'm totally Claude")

        await asyncio.sleep(1.0)
        poll_task.cancel()

        assert len(received) == 1
        sender = received[0][0]
        # The sender should NOT be the raw "Claude" — either the MCP server
        # or receive_external_message should have renamed it
        assert "external" in sender.lower()

    @pytest.mark.asyncio
    async def test_spoofed_user_name_is_relabeled(self, app_state: AppState, project_dir: Path):
        """An external writer claiming to be 'User' should be relabeled."""
        received: list[tuple[str, str]] = []
        app_state.router.on_external_message = lambda sender, text: received.append((sender, text))

        poll_task = app_state.start_external_polling(app_state.receive_external_message)

        _mcp_write(project_dir, "User", "fake user message")

        await asyncio.sleep(1.0)
        poll_task.cancel()

        assert len(received) == 1
        assert "external" in received[0][0].lower()

    @pytest.mark.asyncio
    async def test_normal_external_name_preserved(self, app_state: AppState, project_dir: Path):
        """A legitimate external name should pass through unchanged."""
        received: list[tuple[str, str]] = []
        app_state.router.on_external_message = lambda sender, text: received.append((sender, text))

        poll_task = app_state.start_external_polling(app_state.receive_external_message)

        _mcp_write(project_dir, "Bob", "hi everyone")

        await asyncio.sleep(1.0)
        poll_task.cancel()

        assert len(received) == 1
        assert received[0][0] == "Bob"

    @pytest.mark.asyncio
    async def test_message_added_to_conversation(self, app_state: AppState, project_dir: Path):
        """External messages should appear in the conversation list."""
        poll_task = app_state.start_external_polling(app_state.receive_external_message)

        _mcp_write(project_dir, "Eve", "checking in")

        await asyncio.sleep(1.0)
        poll_task.cancel()

        external_msgs = [
            m for m in app_state.conversation
            if m.sender.is_external and "Eve" in (m.sender.name or "")
        ]
        assert len(external_msgs) == 1
        assert external_msgs[0].text == "checking in"

    @pytest.mark.asyncio
    async def test_external_participant_tracked(self, app_state: AppState, project_dir: Path):
        """New external participants should be tracked."""
        joined: list[str] = []
        app_state.router.on_external_participant_joined = lambda name: joined.append(name)

        poll_task = app_state.start_external_polling(app_state.receive_external_message)

        _mcp_write(project_dir, "NewAgent", "first message")

        await asyncio.sleep(1.0)
        poll_task.cancel()

        assert "NewAgent" in joined
        assert "NewAgent" in app_state.external_participants


class TestMCPConversationTargeting:
    """MCP tools should respect conversation_id and validate invalid ids."""

    def test_send_to_specific_conversation(self, project_dir: Path):
        """Writing to a specific conversation_id targets that conversation."""
        # Ensure DB is initialized
        _mcp_write(project_dir, "Alice", "default msg")

        # Create a second conversation via the DB directly
        import sqlite3
        from penta.services.db_schema import db_path_for

        path = db_path_for(project_dir)
        conn = sqlite3.connect(str(path))
        from penta.utils import utc_iso_now

        now = utc_iso_now()
        conn.execute(
            "INSERT INTO conversations (title, created_at, updated_at) VALUES (?, ?, ?)",
            ("Second", now, now),
        )
        conn.commit()
        cid2 = conn.execute("SELECT MAX(id) FROM conversations").fetchone()[0]
        conn.close()

        # Write to the second conversation explicitly
        send_to_group_chat(str(project_dir), "targeted msg", "Bob", conversation_id=cid2)

        # Read from each conversation
        default_chat = get_group_chat(str(project_dir), conversation_id=1)
        second_chat = get_group_chat(str(project_dir), conversation_id=cid2)

        assert "default msg" in default_chat
        assert "targeted msg" not in default_chat
        assert "targeted msg" in second_chat

    def test_send_to_invalid_conversation_returns_error(self, project_dir: Path):
        # Ensure DB exists
        _mcp_write(project_dir, "Alice", "setup")

        result = send_to_group_chat(str(project_dir), "bad", "Bob", conversation_id=9999)
        assert "does not exist" in result

    def test_get_from_invalid_conversation_returns_error(self, project_dir: Path):
        _mcp_write(project_dir, "Alice", "setup")

        result = get_group_chat(str(project_dir), conversation_id=9999)
        assert "does not exist" in result

    def test_list_conversations_shows_all(self, project_dir: Path):
        _mcp_write(project_dir, "Alice", "setup")

        result = list_conversations(str(project_dir))
        assert "Default" in result
