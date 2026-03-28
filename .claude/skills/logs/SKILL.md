---
name: logs
description: Find, read, and parse Penta rotating log files for debugging agent issues. Use this skill whenever the user asks to check logs, debug agent behavior, see what an agent sent/received, investigate errors, or diagnose streaming/parsing issues. Also use proactively when debugging agent problems — the raw JSON in the logs is often the fastest way to understand what happened.
user_invocable: true
---

# Penta Log Viewer

Penta writes rotating debug logs alongside its SQLite chat database. These logs capture every raw JSON line from agent CLIs, plus all application-level info/warning/error messages. They're invaluable for debugging agent behavior.

## Log location

Logs live at `~/.local/share/penta/chats/<hash>/penta.log` where `<hash>` is the SHA-256 hex digest of the **resolved** working directory path.

To find the log for the current project:

```bash
python3 -c "
import hashlib
from pathlib import Path
d = Path('<WORKING_DIR>').resolve()
h = hashlib.sha256(str(d).encode()).hexdigest()
print(Path.home() / '.local/share/penta/chats' / h / 'penta.log')
"
```

Replace `<WORKING_DIR>` with the project directory (defaults to cwd). The SQLite DB (`penta.db`) is in the same directory.

Rotating backups exist as `.log.1`, `.log.2`, `.log.3` (newest to oldest). Max 5 MB per file, 4 files total (20 MB max).

## Log format

Each line follows:

```
2026-03-27 19:30:15,123 DEBUG    penta.services.agent_service: [Gemini] raw: {"type":"message","role":"assistant",...}
```

Fields: `timestamp  level  logger_name: message`

## What to look for

### Raw agent JSON (most useful for debugging)

Every JSON line from agent CLIs is logged at DEBUG level with the pattern `[AgentName] raw:`. To extract raw JSON from a specific agent:

```bash
grep '\[Gemini\] raw:' penta.log | tail -50
```

```bash
grep '\[Claude\] raw:' penta.log | tail -50
```

```bash
grep '\[Codex\] raw:' penta.log | tail -50
```

The JSON after `raw:` is the literal line from the agent's stdout (truncated at 2000 chars). This shows exactly what the CLI sent, before any parsing.

### Errors and warnings

```bash
grep -E '(ERROR|WARNING)' penta.log | tail -30
```

### Session lifecycle

```bash
grep -E 'Session (started|resumed)|Launching|stdout stream ended' penta.log
```

### Thinking events (Gemini/Claude)

```bash
grep 'Thinking:' penta.log | tail -20
```

### Filter by time window

Timestamps are ISO-ish (`YYYY-MM-DD HH:MM:SS,mmm`). To see the last N minutes:

```bash
# Everything in the last 5 minutes (adjust the timestamp)
awk '$1 " " $2 >= "2026-03-27 19:25:00"' penta.log
```

## Common debugging workflows

**"Gemini thinking shows up wrong"** — Check the raw JSON to see if `thought` is a JSON field or literal text in `content`:
```bash
grep '\[Gemini\] raw:' penta.log | grep -i thought | tail -20
```

**"Agent didn't respond"** — Check if the process launched, if there were errors, and if stdout ended:
```bash
grep -E '\[AgentName\] (Launching|Error|stderr|stdout stream ended)' penta.log
```

**"Message content looks wrong"** — Compare raw JSON (what the CLI sent) against what ended up in the DB:
```bash
# Raw from CLI
grep '\[Gemini\] raw:.*"type":"message"' penta.log | tail -10
# What was saved
sqlite3 ~/.local/share/penta/chats/<hash>/penta.db "SELECT substr(text,1,200) FROM messages ORDER BY id DESC LIMIT 5;"
```

## Arguments

`/logs` accepts an optional argument to filter:

- `/logs` — show the log path and recent errors/warnings
- `/logs <agent>` — show recent raw JSON from that agent (e.g., `/logs gemini`)
- `/logs errors` — show recent errors and warnings
- `/logs raw` — show the last 50 raw JSON lines from all agents
- `/logs path` — just print the log file path

When the user provides arguments, use Grep/Read tools to extract the relevant lines from the log file. Start by computing the log path from the current working directory.
