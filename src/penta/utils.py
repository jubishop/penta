from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)


def utc_iso_now() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def log_task_error(task: asyncio.Task) -> None:
    """Done-callback for fire-and-forget tasks — log exceptions."""
    if not task.cancelled() and task.exception():
        log.error("Background task failed", exc_info=task.exception())
