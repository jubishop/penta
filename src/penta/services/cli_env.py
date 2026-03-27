"""Shared environment setup for CLI-based agent services."""

from __future__ import annotations

import os
from pathlib import Path


def build_cli_env() -> dict[str, str]:
    """Build an environment dict with common CLI install locations on PATH."""
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
