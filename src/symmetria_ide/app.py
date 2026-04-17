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
    Property,
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
class StatusBarState(QObject):
    """Per-field statusline state with individual notify signals.

    QML binds to properties (`mode`, `file`, `branch`, `project`,
    `position`) and each `*Changed` signal makes dependent bindings
    re-evaluate automatically. This is why we moved off a generic
    `ListModel`-of-dicts — `Text.text: model.valueFor("mode")` won't
    re-bind when the dict is replaced, but `Text.text: state.mode` will.

    Unknown capsule ids still flow through `CapsuleModel` so future
    extensions (LSP progress, task state) have somewhere to land
    without touching this class.
    """

    modeChanged = Signal()
    fileChanged = Signal()
    branchChanged = Signal()
    projectChanged = Signal()
    positionChanged = Signal()

    _KNOWN_IDS = frozenset({"mode", "file", "branch", "project", "pos"})

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._mode = ""
        self._file = ""
        self._branch = ""
        self._project = ""
        self._position = ""

    @Property(str, notify=modeChanged)
    def mode(self) -> str:
        return self._mode

    @Property(str, notify=fileChanged)
    def file(self) -> str:
        return self._file

    @Property(str, notify=branchChanged)
    def branch(self) -> str:
        return self._branch

    @Property(str, notify=projectChanged)
    def project(self) -> str:
        return self._project

    @Property(str, notify=positionChanged)
    def position(self) -> str:
        return self._position

    @Slot(dict, result=bool)
    def apply(self, payload: dict) -> bool:
        """Apply one capsule payload; return True if it was handled.

        Returning a bool lets the caller decide whether to also forward
        unhandled payloads into a generic model.
        """
        cid = str(payload.get("id") or "")
        value = str(payload.get("value") or "")
        if cid == "mode" and value != self._mode:
            self._mode = value
            self.modeChanged.emit()
            return True
        if cid == "file" and value != self._file:
            self._file = value
            self.fileChanged.emit()
            return True
        if cid == "branch" and value != self._branch:
            self._branch = value
            self.branchChanged.emit()
            return True
        if cid == "project" and value != self._project:
            self._project = value
            self.projectChanged.emit()
            return True
        if cid == "pos" and value != self._position:
            self._position = value
            self.positionChanged.emit()
            return True
        return cid in self._KNOWN_IDS  # handled but unchanged


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

    Owns the `NvimBackend`, the `StatusBarState` (for well-known
    capsules bound directly into QML properties), and `CapsuleModel`
    (for unknown/extension capsules). Every incoming capsule is tried
    against `StatusBarState.apply` first; if unhandled, it goes into
    the generic model.
    """

    backendReady = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._backend = NvimBackend(cols=120, rows=30)
        self._status = StatusBarState(self)
        self._capsules = CapsuleModel(self)
        self._backend.capsule_updated.connect(self._route_capsule)

    @Slot(dict)
    def _route_capsule(self, payload: dict) -> None:
        if self._status.apply(payload):
            return
        self._capsules.update(payload)

    def start(self) -> None:
        self._backend.start()
        self.backendReady.emit()

    def shutdown(self) -> None:
        self._backend.stop()

    @property
    def backend(self) -> NvimBackend:
        return self._backend

    @property
    def status(self) -> StatusBarState:
        return self._status

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
    engine.rootContext().setContextProperty("statusState", controller.status)

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
