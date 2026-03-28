"""Shared environment setup for CLI-based agent services."""

from __future__ import annotations

import os
from pathlib import Path


_cached_env: dict[str, str] | None = None


def build_cli_env() -> dict[str, str]:
    """Build an environment dict with common CLI install locations on PATH.

    Result is cached — the environment is stable for the lifetime of the process.
    """
    global _cached_env
    if _cached_env is not None:
        return _cached_env
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
    _cached_env = env
    return env
