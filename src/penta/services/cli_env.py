"""Shared environment setup for CLI-based agent services."""

from __future__ import annotations

import functools
import os
from pathlib import Path


@functools.lru_cache(maxsize=1)
def build_cli_env() -> dict[str, str]:
    """Build an environment dict with common CLI install locations on PATH.

    Result is cached — the environment is stable for the lifetime of the process.
    Call ``build_cli_env.cache_clear()`` in tests to reset.
    """
    env = os.environ.copy()
    extra_paths = [
        str(Path.home() / ".local" / "bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
    ]
    existing = env.get("PATH", "")
    for p in extra_paths:
        if p not in existing:
            existing = f"{p}:{existing}"
    env["PATH"] = existing
    return env
