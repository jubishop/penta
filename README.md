# Penta

Multi-agent group chat in your terminal. Talk to Claude and Codex in one conversation — they see each other's messages and can collaborate.

Built with [Textual](https://github.com/textualize/textual/).

## Install

```bash
git clone git@github.com:jubishop/penta.git
pipx install -e penta/
```

This installs `penta` as a global command in an isolated environment. If you don't have pipx: `brew install pipx`.

Requires `claude` and/or `codex` CLIs on your PATH (or set `PENTA_CLAUDE_PATH` / `PENTA_CODEX_PATH`).

## Usage

```bash
penta                  # chat scoped to current directory
penta ~/projects/foo   # chat scoped to a specific directory
```

Type a message and press **Ctrl+Enter** to send.

### Mentioning agents

Use `@name` to direct a message — capitalization doesn't matter:

```
@claude explain this function       → only Claude responds
@codex review these changes         → only Codex responds
@claude and @codex debate this      → both respond
(no mention)                        → all respond
```

### Permissions

Only **Claude** prompts for tool-use approval — an inline dialog appears when it needs to run a shell command, edit a file, etc. Click **Allow** or **Deny**.

**Codex** auto-approves all tool use (no interactive dialogs).

## How it works

- **Chat history** is stored in SQLite (`~/.local/share/penta/chats/<hash>/penta.db`), scoped by working directory
- **Sessions persist** across restarts — agents resume with full context
- **Context catch-up** — when an agent is addressed after missing messages, it receives everything it missed
- **MCP server** (`penta-mcp-server`) lets external agents read/write to the group chat

## MCP server

Install for use with other Claude/Codex sessions:

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

