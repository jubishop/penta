from __future__ import annotations

import os
import shutil
from enum import Enum


class AgentType(Enum):
    CLAUDE = "claude"
    CODEX = "codex"
    GEMINI = "gemini"

    @property
    def display_name(self) -> str:
        return self.value.capitalize()

    @property
    def color(self) -> str:
        return {"claude": "orange", "codex": "green", "gemini": "dodger_blue"}[self.value]

    def find_executable(self) -> str | None:
        env_key = f"PENTA_{self.value.upper()}_PATH"
        override = os.environ.get(env_key)
        if override and os.path.isfile(override) and os.access(override, os.X_OK):
            return override
        return shutil.which(self.value)

    @classmethod
    def all_names(cls) -> frozenset[str]:
        return frozenset(t.value for t in cls)
