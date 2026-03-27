"""Entry point for `python -m penta` and the `penta` console script."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    directory = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()

    if not directory.is_dir():
        print(f"Error: {directory} is not a directory", file=sys.stderr)
        sys.exit(1)

    from penta.app import PentaApp

    app = PentaApp(directory=directory)
    app.run()


if __name__ == "__main__":
    main()
