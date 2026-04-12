---
name: Git hooks must be silent
description: Git hooks (post-checkout, etc.) must never output anything — stdout/stderr can break Claude Code sessions
type: feedback
---

Git hooks must produce zero output (stdout or stderr). Any output from hooks can interfere with Claude Code sessions.

**Why:** Output from git hooks leaks into the Claude Code CLI session and can corrupt it.

**How to apply:** When writing or modifying git hooks, always redirect all output to /dev/null or a log file. Never use `echo` in hooks.
