---
name: Running penta and tests in worktrees
description: Worktrees live in worktrees/ (not .claude/worktrees/) — use .venv/bin/penta to run, global penta stays on main via pipx
type: feedback
---

Worktrees are created in `worktrees/` at the project root (gitignored), NOT `.claude/worktrees/`. This avoids macOS `UF_HIDDEN` flag inheritance from `.claude/` which breaks Python 3.14's `.pth` file processing.

In a worktree, run `.venv/bin/penta` or `pytest tests/`. The global `penta` command (pipx) points at `~/Desktop/penta` main and is unaffected.

**Why:** `.claude/` has macOS `UF_HIDDEN`, Python 3.14 silently skips hidden `.pth` files, editable installs break. Moving worktrees out of `.claude/` eliminates the problem entirely.

**How to apply:** When creating worktrees, use `git worktree add worktrees/<name>`. Run `bin/prep-worktree worktrees/<name>` to set up the venv. `prep-worktree` does a standard `pip install -e ".[dev]"` — no special workarounds needed.
