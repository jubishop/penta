# PentaMac

Native macOS rewrite of Penta built with SwiftUI and GRDB.

This package intentionally lives next to the Python/Textual app while the rewrite is in progress. It uses the same per-directory database path and SQLite schema as the current app:

```text
~/.local/share/penta/chats/<sha256-of-directory>/penta.db
```

## Run

```bash
cd macos/PentaMac
swift run PentaMac /path/to/project
```

If no directory is provided, PentaMac scopes the chat to the current working directory.
`swift run` launches a GUI process, so the terminal remains attached until you quit the app.

The app discovers `claude` and `codex` on `PATH`, with the same overrides as the Python app:

```bash
PENTA_CLAUDE_PATH=/path/to/claude
PENTA_CODEX_PATH=/path/to/codex
```

## Current Port Status

Implemented:

- Native SwiftUI chat window with conversation list, status chips, message stream, and composer
- GRDB-backed persistence using the existing Penta schema and migration path
- Claude and Codex spawn-per-turn CLI execution with JSON stream parsing
- `@mention` routing and agent toggle prefixes
- External database polling for messages written by the MCP server or another process

Not yet ported:

- Claude hook HTTP server for `ExitPlanMode` and `AskUserQuestion`
- The standalone MCP server itself, which still exists in Python
- Rename/delete conversation UI actions

## Test

```bash
cd macos/PentaMac
swift test
```
