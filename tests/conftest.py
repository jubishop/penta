"""Shared fixtures for Penta tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from penta.app_state import AppState
from penta.models.agent_config import AgentConfig
from penta.services.db import PentaDB

from .fakes import FakeAgentService


@pytest.fixture
async def memory_db():
    db = PentaDB(Path("/unused"), in_memory=True)
    await db.connect()
    yield db
    await db.close()


@pytest.fixture
def fake_service():
    return FakeAgentService()


@pytest.fixture
def service_factory(fake_service):
    """Returns the same FakeAgentService for every agent — suitable for
    single-agent tests or tests that don't need per-agent control."""
    return lambda config: fake_service


@pytest.fixture
def fake_services():
    """Returns (services_dict, factory) for multi-agent tests.

    Each agent gets its own FakeAgentService, keyed by agent name.
    """
    services: dict[str, FakeAgentService] = {}

    def factory(config: AgentConfig) -> FakeAgentService:
        svc = FakeAgentService()
        services[config.name] = svc
        return svc

    return services, factory


@pytest.fixture
async def app_state(memory_db, service_factory):
    """AppState with a single shared FakeAgentService for all agents."""
    state = AppState(Path("/tmp/test"), db=memory_db, service_factory=service_factory)
    await state.connect()
    yield state
    await state.shutdown()


@pytest.fixture
async def multi_agent_state(memory_db, fake_services):
    """AppState with per-agent FakeAgentService instances.

    Yields (state, services_dict) where services_dict maps agent name
    to its FakeAgentService.
    """
    services, factory = fake_services
    state = AppState(Path("/tmp/test"), db=memory_db, service_factory=factory)
    await state.connect()
    yield state, services
    await state.shutdown()
