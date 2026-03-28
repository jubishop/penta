"""Entry point for `python -m penta` and the `penta` console script."""

from __future__ import annotations

import atexit
import logging
import sys
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from pathlib import Path
from queue import SimpleQueue


def _configure_logging(directory: Path) -> None:
    from penta.services.db import PentaDB

    log_dir = PentaDB.db_path_for(directory).parent
    log_dir.mkdir(parents=True, exist_ok=True)

    file_handler = RotatingFileHandler(
        log_dir / "penta.log",
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=3,
    )
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    ))

    # QueueHandler + QueueListener: log calls push to an in-memory queue
    # (non-blocking), a background thread drains to the file handler.
    log_queue: SimpleQueue = SimpleQueue()
    listener = QueueListener(log_queue, file_handler, respect_handler_level=True)
    listener.start()
    atexit.register(listener.stop)

    root = logging.getLogger()
    root.addHandler(QueueHandler(log_queue))
    root.setLevel(logging.DEBUG)


def main() -> None:
    directory = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()

    if not directory.is_dir():
        print(f"Error: {directory} is not a directory", file=sys.stderr)
        sys.exit(1)

    _configure_logging(directory)

    from penta.app import PentaApp

    app = PentaApp(directory=directory)
    app.run()


if __name__ == "__main__":
    main()
