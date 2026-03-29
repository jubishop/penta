from __future__ import annotations

import logging
from typing import Callable
from uuid import UUID

from penta.coordinators.agent_coordinator import AgentCoordinator
from penta.models.agent_config import AgentConfig
from penta.models.agent_status import AgentStatus
from penta.models.agent_type import AgentType
from penta.models.permission_request import PermissionRequest

log = logging.getLogger(__name__)


class PermissionManager:
    def __init__(
        self,
        agents: list[AgentConfig],
        coordinators: dict[UUID, AgentCoordinator],
    ) -> None:
        self._agents = agents
        self._coordinators = coordinators
        self.pending: list[PermissionRequest] = []
        self.on_permission_request: Callable[[PermissionRequest], None] | None = None

    def handle_http_request(
        self, tool_use_id: str, tool_name: str, tool_input: str,
    ) -> None:
        """Called by PermissionServer when Claude POSTs a permission request."""
        # TODO: routes to the first Claude agent — when multiple Claude agents
        # are supported, the hook protocol will need to carry an agent identifier.
        claude_agent = next(
            (a for a in self._agents if a.type == AgentType.CLAUDE), None
        )
        if not claude_agent:
            log.error("Permission request but no Claude agent registered")
            return
        request = PermissionRequest(
            id=tool_use_id,
            agent_id=claude_agent.id,
            tool_name=tool_name,
            tool_input=tool_input,
        )
        self.pending.append(request)
        coord = self._coordinators.get(claude_agent.id)
        if coord:
            coord._set_status(AgentStatus.AWAITING_PERMISSION)
        if self.on_permission_request:
            self.on_permission_request(request)

    def approve(self, request_id: str) -> None:
        request = self._pop(request_id)
        if request:
            self._resolve(request, granted=True)

    def deny(self, request_id: str) -> None:
        request = self._pop(request_id)
        if request:
            self._resolve(request, granted=False)

    def pending_for(self, agent_id: UUID) -> PermissionRequest | None:
        return next(
            (p for p in self.pending if p.agent_id == agent_id), None
        )

    def _pop(self, request_id: str) -> PermissionRequest | None:
        for i, req in enumerate(self.pending):
            if req.id == request_id:
                return self.pending.pop(i)
        return None

    def _resolve(self, request: PermissionRequest, granted: bool) -> None:
        coord = self._coordinators.get(request.agent_id)
        if not coord:
            return
        coord._set_status(AgentStatus.PROCESSING)
        coord.resolve_permission(request.id, granted)
