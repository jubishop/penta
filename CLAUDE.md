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

## Running

```bash
pip install -e ".[dev]"
penta [directory]
```
