# Penta

Multi-agent group chat TUI — Claude, Codex, and Gemini in one terminal conversation.

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

## Running

```bash
pip install -e ".[dev]"
penta [directory]
```
