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
from penta_mcp.server import send_to_group_chat


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
