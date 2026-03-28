from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)


def log_task_error(task: asyncio.Task) -> None:
    """Done-callback for fire-and-forget tasks — log exceptions."""
    if not task.cancelled() and task.exception():
        log.error("Background task failed", exc_info=task.exception())
