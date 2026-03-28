"""Tests for PermissionManager in isolation."""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

from penta.models.agent_config import AgentConfig
from penta.models.agent_status import AgentStatus
from penta.models.agent_type import AgentType
from penta.models.permission_request import PermissionRequest
from penta.permissions import PermissionManager


def _make_agent(name: str, agent_type: AgentType) -> AgentConfig:
    return AgentConfig(
        id=uuid4(), name=name, type=agent_type, status=AgentStatus.IDLE,
    )


def _make_manager(
    agents: list[AgentConfig] | None = None,
) -> tuple[PermissionManager, dict]:
    agents = agents or []
    coordinators = {}
    for a in agents:
        mock_coord = MagicMock()
        coordinators[a.id] = mock_coord
    return PermissionManager(agents, coordinators), coordinators


class TestPermissionApproval:
    def test_approve_resolves_via_coordinator(self):
        claude = _make_agent("claude", AgentType.CLAUDE)
        mgr, coords = _make_manager([claude])

        mgr.handle_http_request("tool-1", "bash", '{"cmd": "ls"}')
        assert len(mgr.pending) == 1

        mgr.approve("tool-1")
        coords[claude.id].resolve_permission.assert_called_once_with("tool-1", True)
        assert len(mgr.pending) == 0

    def test_deny_resolves_via_coordinator(self):
        claude = _make_agent("claude", AgentType.CLAUDE)
        mgr, coords = _make_manager([claude])

        mgr.handle_http_request("tool-1", "bash", '{"cmd": "rm"}')
        mgr.deny("tool-1")
        coords[claude.id].resolve_permission.assert_called_once_with("tool-1", False)
        assert len(mgr.pending) == 0

    def test_approve_unknown_id_is_noop(self):
        claude = _make_agent("claude", AgentType.CLAUDE)
        mgr, coords = _make_manager([claude])
        mgr.approve("nonexistent")  # no crash
        coords[claude.id].resolve_permission.assert_not_called()

    def test_deny_unknown_id_is_noop(self):
        claude = _make_agent("claude", AgentType.CLAUDE)
        mgr, coords = _make_manager([claude])
        mgr.deny("nonexistent")
        coords[claude.id].resolve_permission.assert_not_called()


class TestPermissionHTTPCallback:
    def test_request_routed_to_claude_agent(self):
        claude = _make_agent("claude", AgentType.CLAUDE)
        codex = _make_agent("codex", AgentType.CODEX)
        mgr, _ = _make_manager([claude, codex])

        mgr.handle_http_request("tool-1", "bash", "{}")
        assert len(mgr.pending) == 1
        assert mgr.pending[0].agent_id == claude.id

    def test_no_claude_agent_logs_no_crash(self):
        codex = _make_agent("codex", AgentType.CODEX)
        mgr, _ = _make_manager([codex])
        mgr.handle_http_request("tool-1", "bash", "{}")
        assert len(mgr.pending) == 0

    def test_callback_fires_with_request(self):
        claude = _make_agent("claude", AgentType.CLAUDE)
        mgr, _ = _make_manager([claude])
        received: list[PermissionRequest] = []
        mgr.on_permission_request = lambda r: received.append(r)

        mgr.handle_http_request("tool-1", "bash", '{"cmd": "ls"}')
        assert len(received) == 1
        assert received[0].id == "tool-1"
        assert received[0].tool_name == "bash"


class TestPendingFor:
    def test_returns_matching_request(self):
        claude = _make_agent("claude", AgentType.CLAUDE)
        mgr, _ = _make_manager([claude])
        mgr.handle_http_request("tool-1", "bash", "{}")
        assert mgr.pending_for(claude.id) is not None
        assert mgr.pending_for(claude.id).id == "tool-1"

    def test_returns_none_when_empty(self):
        claude = _make_agent("claude", AgentType.CLAUDE)
        mgr, _ = _make_manager([claude])
        assert mgr.pending_for(claude.id) is None

    def test_returns_none_for_wrong_agent(self):
        claude = _make_agent("claude", AgentType.CLAUDE)
        codex = _make_agent("codex", AgentType.CODEX)
        mgr, _ = _make_manager([claude, codex])
        mgr.handle_http_request("tool-1", "bash", "{}")
        assert mgr.pending_for(codex.id) is None
