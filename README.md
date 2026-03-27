# Penta

Multi-agent group chat in your terminal. Talk to Claude, Codex, and Gemini in one conversation — they see each other's messages and can collaborate.

Built with [Textual](https://github.com/textualize/textual/).

## Install

```bash
pip install -e .
```

Requires `claude`, `codex`, and/or `gemini` CLIs on your PATH (or set `PENTA_CLAUDE_PATH` / `PENTA_CODEX_PATH` / `PENTA_GEMINI_PATH`).

## Usage

```bash
penta                  # chat scoped to current directory
penta ~/projects/foo   # chat scoped to a specific directory
```

Type a message and press **Ctrl+Enter** to send.

### Mentioning agents

Just use their name — no `@` or capitalization needed:

```
claude explain this function        → only Claude responds
codex review these changes          → only Codex responds
gemini summarize this file          → only Gemini responds
claude and codex debate this        → both respond
(no name mentioned)                 → all respond
```

### Shell commands

Prefix with `$` to run a shell command inline:

```
$ git status
$ ls -la src/
```

### Permissions

Only **Claude** prompts for tool-use approval — an inline dialog appears when it needs to run a shell command, edit a file, etc. Click **Allow** or **Deny**.

**Codex** and **Gemini** auto-approve all tool use (no interactive dialogs).

## How it works

- **Chat history** is stored in SQLite (`~/.local/share/penta/chats/<hash>/penta.db`), scoped by working directory
- **Sessions persist** across restarts — agents resume with full context
- **Context catch-up** — when an agent is addressed after missing messages, it receives everything it missed
- **MCP server** (`penta-mcp-server`) lets external agents read/write to the group chat

## MCP server

Install for use with other Claude/Codex/Gemini sessions:

```json
{
  "mcpServers": {
    "penta-group-chat": {
      "command": "penta-mcp-server"
    }
  }
}
```

Tools: `get_group_chat(directory, last_n)` and `send_to_group_chat(directory, message, your_name)`.

## Development

```bash
pip install -e ".[dev]"
python -m pytest tests/ -v
```
