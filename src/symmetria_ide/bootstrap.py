"""Process-bootstrap utilities for Symmetria IDE.

These helpers run exactly once at startup. They're separated from
`app.py` so that file can focus on QObject models, AppController, and
the QML engine wiring without mixing in single-use bootstrap code.

Three things live here:

  * `QML_DIR`               — the `qml/` directory, resolved at import time.
  * `configure_logging`     — root logger setup for the CLI entry.
  * `configure_headless_mode` — wires the smoke-test screenshot +
    key-injection timers when `SYMMETRIA_IDE_SCREENSHOT` /
    `SYMMETRIA_IDE_TEST_KEYS` env vars are set.

`configure_headless_mode` takes the controller/engine/app explicitly so
it stays pure bootstrap plumbing — no circular import back into
`app.py`.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QTimer
from PySide6.QtQuick import QQuickWindow

if TYPE_CHECKING:
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine

    from .app import AppController


log = logging.getLogger(__name__)


def _resolve_qml_dir() -> Path:
    """Resolve the qml/ directory — works both in-tree and when installed.

    In-tree layout (development): project_root/qml/Main.qml
      __file__ is project_root/src/symmetria_ide/bootstrap.py
      parents[2]  is project_root/

    Installed layout: pyproject.toml package-data copies qml/ into the
      package directory alongside this file (symmetria_ide/qml/).
      parents[0] is the package directory.

    Note: Phase 0 is always run in-tree. The installed fallback path is
    provided for completeness but is untested until packaging is wired up.
    """
    in_tree = Path(__file__).resolve().parents[2] / "qml"
    if in_tree.exists():
        return in_tree
    return Path(__file__).resolve().parent / "qml"


QML_DIR: Path = _resolve_qml_dir()


def configure_logging() -> None:
    """Install the project-standard logging format on the root logger."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s — %(message)s",
    )


def configure_headless_mode(
    controller: AppController,
    engine: QQmlApplicationEngine,
    app: QGuiApplication,
    shot_path: str | None,
    test_keys: str | None,
) -> None:
    """Wire smoke-test timers when headless env vars are set.

    `SYMMETRIA_IDE_SCREENSHOT=/path.png` — grabs the window from Qt's
    scene graph after a warmup delay and saves it (works under Wayland
    without compositor capture permissions).
    `SYMMETRIA_IDE_TEST_KEYS=<keys>` — injects a keycode string before
    the screenshot is taken.
    `SYMMETRIA_IDE_WARMUP_MS` / `SYMMETRIA_IDE_SETTLE_MS` — tune timing.
    """
    warmup_ms = int(os.environ.get("SYMMETRIA_IDE_WARMUP_MS", "1500"))
    settle_ms = int(os.environ.get("SYMMETRIA_IDE_SETTLE_MS", "800"))

    def _send_keys() -> None:
        if test_keys:
            log.info("injecting test keys: %r", test_keys)
            controller.backend.input(test_keys)

    def _grab_and_exit() -> None:
        if shot_path:
            for obj in engine.rootObjects():
                if isinstance(obj, QQuickWindow):
                    img = obj.grabWindow()
                    ok = img.save(shot_path)
                    log.info("screenshot saved to %s: %s", shot_path, ok)
                    break
        app.quit()

    QTimer.singleShot(warmup_ms, _send_keys)
    QTimer.singleShot(warmup_ms + settle_ms, _grab_and_exit)
