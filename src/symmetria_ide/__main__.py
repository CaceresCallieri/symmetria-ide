"""Entry point: `python -m symmetria_ide` or `symmetria-ide`."""

from __future__ import annotations

import faulthandler
import os
import sys
from pathlib import Path

_state_dir = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "symmetria-ide"
_state_dir.mkdir(parents=True, exist_ok=True)
_crash_log_path = _state_dir / "crash.log"
_crash_log = _crash_log_path.open("a", buffering=1)
faulthandler.enable(file=_crash_log, all_threads=True)
sys.stderr.write(f"[symmetria-ide] crash log: {_crash_log_path}\n")

from .app import run  # noqa: E402 — faulthandler must arm before Qt/pynvim import


def main() -> int:
    return run()


if __name__ == "__main__":
    sys.exit(main())
