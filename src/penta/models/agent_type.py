from __future__ import annotations

import os
import shutil
from enum import Enum
from pathlib import Path


class AgentType(Enum):
    CLAUDE = "claude"
    CODEX = "codex"

    @property
    def display_name(self) -> str:
        return self.value.capitalize()

    @property
    def color(self) -> str:
        return {"claude": "orange", "codex": "green"}[self.value]

    def find_executable(self) -> str | None:
        env_key = f"PENTA_{self.value.upper()}_PATH"
        override = os.environ.get(env_key)
        if override and Path(override).is_file() and os.access(override, os.X_OK):
            return override
        return shutil.which(self.value)

    @classmethod
    def all_names(cls) -> frozenset[str]:
        return frozenset(t.value for t in cls)
