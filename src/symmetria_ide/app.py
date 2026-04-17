"""QApplication wiring: spawns NvimBackend, loads the QML scene.

This is the boundary between Python backend code and the QML UI. The
QML import module `Symmetria.Ide` is registered so that QML files can
`import Symmetria.Ide 1.0` and instantiate `NvimView`.

`CapsuleModel` is a thin ListModel-like wrapper around a Python list
that the StatusBar QML repeats over. Keeping it in Python (not QML)
means capsules are updated by signal-connecting to `NvimBackend`, not
by QML polling.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
from pathlib import Path

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QObject,
    Qt,
    QTimer,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine, QmlElement
from PySide6.QtQuick import QQuickWindow

from .nvim_backend import NvimBackend
from .nvim_view import NvimView  # noqa: F401 — side-effect: registers @QmlElement

QML_IMPORT_NAME = "Symmetria.Ide"
QML_IMPORT_MAJOR_VERSION = 1


log = logging.getLogger(__name__)


@QmlElement
class CapsuleModel(QAbstractListModel):
    """ListModel exposing capsule dicts to QML Repeater/ListView.

    Each capsule carries at least `id`, `label`, `value`. QML accesses
    fields via role names (so the delegate writes `model.label`, etc.).

    Updates are idempotent — `update(payload)` replaces-or-appends by
    `id`, keeping display order stable as capsules refresh.
    """

    IdRole = Qt.ItemDataRole.UserRole + 1
    LabelRole = Qt.ItemDataRole.UserRole + 2
    ValueRole = Qt.ItemDataRole.UserRole + 3

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._items: list[dict[str, str]] = []

    def roleNames(self) -> dict[int, bytes]:
        return {
            self.IdRole: b"id",
            self.LabelRole: b"label",
            self.ValueRole: b"value",
        }

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008, ARG002
        return len(self._items)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self._items):
            return None
        item = self._items[index.row()]
        if role == self.IdRole:
            return item.get("id", "")
        if role == self.LabelRole:
            return item.get("label", "")
        if role == self.ValueRole:
            return item.get("value", "")
        return None

    @Slot(dict)
    def update(self, payload: dict) -> None:
        """Upsert a capsule by `id`. New capsules append, existing replace."""
        cid = str(payload.get("id") or "")
        if not cid:
            return
        label = str(payload.get("label") or "")
        value = str(payload.get("value") or "")
        for i, existing in enumerate(self._items):
            if existing.get("id") == cid:
                self._items[i] = {"id": cid, "label": label, "value": value}
                idx = self.index(i)
                self.dataChanged.emit(idx, idx, [self.LabelRole, self.ValueRole])
                return
        self.beginInsertRows(QModelIndex(), len(self._items), len(self._items))
        self._items.append({"id": cid, "label": label, "value": value})
        self.endInsertRows()


class AppController(QObject):
    """Glue object exposed to QML as `controller`.

    Holds references to `NvimBackend` and `CapsuleModel` so the QML can
    bind the view and the status bar to them. A root controller keeps
    QML simpler than exposing each object as a separate context
    property.
    """

    backendReady = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._backend = NvimBackend(cols=120, rows=30)
        self._capsules = CapsuleModel(self)
        self._backend.capsule_updated.connect(self._capsules.update)

    def start(self) -> None:
        self._backend.start()
        self.backendReady.emit()

    def shutdown(self) -> None:
        self._backend.stop()

    @property
    def backend(self) -> NvimBackend:
        return self._backend

    @property
    def capsules(self) -> CapsuleModel:
        return self._capsules


def _qml_dir() -> Path:
    """Resolve the qml/ directory — works both in-tree and when installed."""
    in_tree = Path(__file__).resolve().parents[2] / "qml"
    if in_tree.exists():
        return in_tree
    packaged = Path(__file__).resolve().parent / "qml"
    return packaged


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s — %(message)s",
    )


def run() -> int:
    _configure_logging()
    # Ctrl-C in the terminal should kill the app, not be caught by Qt.
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    app = QGuiApplication(sys.argv)
    app.setApplicationName("Symmetria IDE")
    app.setOrganizationName("Symmetria")
    # Sets the Wayland xdg-shell `app_id` — Hyprland sees this as the
    # window class, so window rules can match on `symmetria-ide`.
    app.setDesktopFileName("symmetria-ide")

    controller = AppController()
    engine = QQmlApplicationEngine()

    # Make backend + capsules available to QML as a single `controller`
    # context property — keeps the QML surface small.
    engine.rootContext().setContextProperty("controller", controller)
    engine.rootContext().setContextProperty("nvimBackend", controller.backend)
    engine.rootContext().setContextProperty("capsuleModel", controller.capsules)

    qml_root = _qml_dir() / "Main.qml"
    engine.load(QUrl.fromLocalFile(str(qml_root)))
    if not engine.rootObjects():
        log.error("failed to load Main.qml at %s", qml_root)
        return 1

    controller.start()
    app.aboutToQuit.connect(controller.shutdown)
    # If nvim exits on its own (user typed `:q`), close the window too
    # — otherwise the grid freezes on whatever was last rendered and
    # the user has no way to exit except killing the process.
    controller.backend.closed.connect(app.quit, Qt.ConnectionType.QueuedConnection)

    # Optional: headless screenshot-and-exit for smoke testing.
    # `SYMMETRIA_IDE_SCREENSHOT=/path.png` waits the given delay, grabs
    # the window directly from Qt's scene graph (works under Wayland
    # without compositor capture perms), saves it, then quits.
    shot_path = os.environ.get("SYMMETRIA_IDE_SCREENSHOT")
    test_keys = os.environ.get("SYMMETRIA_IDE_TEST_KEYS")
    if shot_path or test_keys:
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

    return app.exec()
