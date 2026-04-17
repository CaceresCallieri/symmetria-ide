"""Entry point: `python -m symmetria_ide` or `symmetria-ide`."""

from __future__ import annotations

import sys

from .app import run


def main() -> int:
    return run()


if __name__ == "__main__":
    sys.exit(main())
