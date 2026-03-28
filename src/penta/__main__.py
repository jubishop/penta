"""Entry point for `python -m penta` and the `penta` console script."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def _configure_logging(directory: Path) -> None:
    from penta.services.db import PentaDB

    log_dir = PentaDB.db_path_for(directory).parent
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_dir / "penta.log",
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=3,
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    ))
    root = logging.getLogger()
    root.addHandler(handler)
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
