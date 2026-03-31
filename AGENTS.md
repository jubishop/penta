# Penta

Multi-agent group chat TUI — Claude and Codex in one terminal conversation.

## Stack

- Python 3.11+, Textual TUI framework
- SQLite (WAL mode) for chat persistence
- asyncio throughout — no blocking calls
- HTTP hooks for Claude CLI permission handling

## Project layout

- `src/penta/` — main app package
- `src/penta_mcp/` — standalone MCP server
- `tests/` — pytest suite

## Key patterns

- `pathlib.Path` for all paths (no string paths)
- Dataclasses for models, not Pydantic (keep it lightweight)
- `asyncio.Event` for completion signaling (no polling)
- Textual `Message` objects for decoupling services from UI
- Type hints everywhere
- All agent services use spawn-per-turn CLI execution with JSON streaming — no long-lived server processes
- External MCP messages are always labeled `(external)` when the sender name matches a built-in agent or reserved name
- Constructor injection for testability — `AgentCoordinator`, `AppState`, and `PentaDB` accept optional dependencies so tests can substitute fakes and in-memory storage

## Testing

### Philosophy

Test at behavioral boundaries by controlling external touchpoints through fakes, then asserting on observable outcomes. External touchpoints are: agent CLI services (Claude, Codex) and the SQLite database.

- **Fakes over mocks**: Use `FakeAgentService` (`tests/fakes.py`) for agent services — it records calls and lets you enqueue responses. Don't use `unittest.mock` for agent services.
- **In-memory SQLite**: Use the `memory_db` fixture (via `PentaDB(in_memory=True)`) for all tests. The only exception is tests that need two connections to the same file to simulate external process writes (e.g. MCP integration, external-change detection) — those use file-based SQLite via `tmp_path`.
- **Constructor injection**: Production code accepts optional DI params (`service` on `AgentCoordinator`, `db`/`service_factory` on `AppState`). Tests use these — never monkey-patch `coord.service = ...`.
- **Assert on observables**: Check what was sent to fakes (`fake.calls`), what ended up in `conversation`, and what was persisted to DB. Don't inspect internal state like `_streaming` or `last_prompted_index`.
- **Shared fixtures in `conftest.py`**: Use `memory_db`, `fake_service`, `service_factory`, `fake_services`, `app_state`, `multi_agent_state` from `tests/conftest.py`. Don't duplicate DB or service fixtures in individual test files.
- **No `asyncio.sleep` in tests**: Wait on `Message.wait_for_completion()` or `router.drain()`, not fixed sleeps. The only acceptable sleep is `asyncio.sleep(0)` to yield to the event loop so a just-created task can advance to its first await. This follows the project-wide `asyncio.Event` for completion signaling pattern.
- **Textual pilot for UI tests**: Use `app.run_test()` for widget/UI-level tests.
- **Self-isolated tests**: Every test must be fully self-contained — no shared mutable state between tests. Each test creates its own fixtures (DB, fakes, app state) so the entire suite can run in parallel without interference.

### Running tests

```bash
pytest
```

## Running

```bash
pip install -e ".[dev]"
penta [directory]
```
