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

import gc
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
from PySide6.QtGui import QGuiApplication, QSurfaceFormat
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


@QmlElement
class CmdlineState(QObject):
    """Floating command-line state sourced from NeoVim's ext_cmdline events.

    Per-field `@Property` with notify signals so QML bindings re-evaluate
    when any single field changes (same pattern as `StatusBarState`).
    `visible` drives show/hide; `text` + `cursorPos` drive the content
    split around a native block cursor.

    `level` tracks nested cmdlines (triggered e.g. by `<C-r>=` expressions
    inside a cmdline). For MVP we render any level; block-mode
    (`cmdline_block_*`) multi-line input is deliberately not handled yet.
    """

    visibleChanged = Signal()
    firstcharChanged = Signal()
    promptChanged = Signal()
    textChanged = Signal()
    cursorPosChanged = Signal()
    levelChanged = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._visible = False
        self._firstchar = ""
        self._prompt = ""
        self._text = ""
        self._cursor_pos = 0
        self._level = 0

    @Property(bool, notify=visibleChanged)
    def visible(self) -> bool:
        return self._visible

    @Property(str, notify=firstcharChanged)
    def firstchar(self) -> str:
        return self._firstchar

    @Property(str, notify=promptChanged)
    def prompt(self) -> str:
        return self._prompt

    @Property(str, notify=textChanged)
    def text(self) -> str:
        return self._text

    @Property(int, notify=cursorPosChanged)
    def cursorPos(self) -> int:
        return self._cursor_pos

    @Property(int, notify=levelChanged)
    def level(self) -> int:
        return self._level

    @Slot(dict)
    def apply(self, payload: dict) -> None:
        """Handle a cmdline event payload from NvimBackend."""
        kind = payload.get("kind")
        if kind == "show":
            new_firstchar = str(payload.get("firstchar", ""))
            new_prompt = str(payload.get("prompt", ""))
            new_text = str(payload.get("text", ""))
            new_pos = int(payload.get("pos", 0))
            new_level = int(payload.get("level", 0))
            if new_firstchar != self._firstchar:
                self._firstchar = new_firstchar
                self.firstcharChanged.emit()
            if new_prompt != self._prompt:
                self._prompt = new_prompt
                self.promptChanged.emit()
            if new_text != self._text:
                self._text = new_text
                self.textChanged.emit()
            if new_pos != self._cursor_pos:
                self._cursor_pos = new_pos
                self.cursorPosChanged.emit()
            if new_level != self._level:
                self._level = new_level
                self.levelChanged.emit()
            if not self._visible:
                self._visible = True
                self.visibleChanged.emit()
            return
        if kind == "pos":
            new_pos = int(payload.get("pos", 0))
            if new_pos != self._cursor_pos:
                self._cursor_pos = new_pos
                self.cursorPosChanged.emit()
            return
        if kind == "hide":
            # Reset text/pos so a subsequent show starts clean — avoids
            # a brief flash of stale text if the overlay re-shows before
            # the next cmdline_show populates it.
            if self._visible:
                self._visible = False
                self.visibleChanged.emit()
            if self._text:
                self._text = ""
                self.textChanged.emit()
            if self._cursor_pos:
                self._cursor_pos = 0
                self.cursorPosChanged.emit()
            if self._firstchar:
                self._firstchar = ""
                self.firstcharChanged.emit()
            if self._prompt:
                self._prompt = ""
                self.promptChanged.emit()
            if self._level:
                self._level = 0
                self.levelChanged.emit()


@QmlElement
class PopupmenuModel(QAbstractListModel):
    """Wildmenu / completion popup exposed as a QAbstractListModel.

    Carries its own `visible` and `selected` properties alongside the
    row data — QML delegates can bind to both `ListView.isCurrentItem`
    (set via `currentIndex: popupmenuModel.selected`) and to the roles
    (`word`, `kind`, `menu`) for per-row text.

    `info` (the fourth element of each popupmenu item) is often a long
    documentation string and is deliberately dropped in the backend
    before it reaches here.
    """

    WordRole = Qt.ItemDataRole.UserRole + 1
    KindRole = Qt.ItemDataRole.UserRole + 2
    MenuRole = Qt.ItemDataRole.UserRole + 3

    selectedChanged = Signal()
    visibleChanged = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._items: list[dict[str, str]] = []
        self._selected = -1
        self._visible = False

    @Property(int, notify=selectedChanged)
    def selected(self) -> int:
        return self._selected

    @Property(bool, notify=visibleChanged)
    def visible(self) -> bool:
        return self._visible

    def roleNames(self) -> dict[int, bytes]:
        return {
            self.WordRole: b"word",
            self.KindRole: b"kind",
            self.MenuRole: b"menu",
        }

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008, ARG002
        return len(self._items)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self._items):
            return None
        item = self._items[index.row()]
        if role == self.WordRole:
            return item.get("word", "")
        if role == self.KindRole:
            return item.get("kind", "")
        if role == self.MenuRole:
            return item.get("menu", "")
        return None

    @Slot(dict)
    def apply(self, payload: dict) -> None:
        kind = payload.get("kind")
        if kind == "show":
            self.beginResetModel()
            self._items = list(payload.get("items") or [])
            self.endResetModel()
            new_selected = int(payload.get("selected", -1))
            if new_selected != self._selected:
                self._selected = new_selected
                self.selectedChanged.emit()
            if not self._visible:
                self._visible = True
                self.visibleChanged.emit()
            return
        if kind == "select":
            new_selected = int(payload.get("selected", -1))
            if new_selected != self._selected:
                self._selected = new_selected
                self.selectedChanged.emit()
            return
        if kind == "hide":
            if self._items:
                self.beginResetModel()
                self._items = []
                self.endResetModel()
            if self._selected != -1:
                self._selected = -1
                self.selectedChanged.emit()
            if self._visible:
                self._visible = False
                self.visibleChanged.emit()


@QmlElement
class CompletionModel(QAbstractListModel):
    """Live cmdline completion items from our runtime's `getcompletion()`.

    Distinct from `PopupmenuModel` (which wraps NeoVim's ext_popupmenu
    wildmenu events). Our runtime fires `CmdlineChanged` and hands back
    `vim.fn.getcompletion(line, "cmdline")` — the same list NeoVim's
    native completion engine would produce, but always, regardless of
    whether the user has nvim-cmp/wilder/noice managing the cmdline.
    This makes our completion UX self-contained and plugin-agnostic.
    """

    WordRole = Qt.ItemDataRole.UserRole + 1

    visibleChanged = Signal()
    selectedChanged = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._items: list[str] = []
        self._visible = False
        self._selected = -1

    @Property(bool, notify=visibleChanged)
    def visible(self) -> bool:
        return self._visible

    @Property(int, notify=selectedChanged)
    def selected(self) -> int:
        """Index of the wildmenu-cycled row, or -1 when nothing is selected.

        Driven from the Lua side via `wildmenumode()` detection: when
        the user presses Tab and nvim cycles through matches, the Lua
        runtime keeps the last computed list stable and reports which
        item now matches the cmdline text. The overlay then uses this
        to paint a selection highlight.
        """
        return self._selected

    def roleNames(self) -> dict[int, bytes]:
        return {self.WordRole: b"word"}

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008, ARG002
        return len(self._items)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self._items):
            return None
        if role == self.WordRole:
            return self._items[index.row()]
        return None

    @Slot(dict)
    def apply(self, payload: dict) -> None:
        raw_items = payload.get("items") or []
        items = [str(it) for it in raw_items]
        new_selected = int(payload.get("selected", -1))
        # Only reset the model when the item list actually changed. Skipping
        # during Tab cycling (items unchanged, only selected advances) avoids
        # a full delegate re-create on every cycle step.
        if items != self._items:
            self.beginResetModel()
            self._items = items
            self.endResetModel()
        new_visible = bool(items)
        if new_visible != self._visible:
            self._visible = new_visible
            self.visibleChanged.emit()
        if new_selected != self._selected:
            self._selected = new_selected
            self.selectedChanged.emit()


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
        # These initial dimensions are a seed that gets immediately overridden
        # when NvimView first receives a geometryChange event and calls
        # backend.resize() with the real pixel-derived cell count.
        self._backend = NvimBackend(cols=120, rows=30)
        self._status = StatusBarState(self)
        self._capsules = CapsuleModel(self)
        self._cmdline = CmdlineState(self)
        self._popupmenu = PopupmenuModel(self)
        self._completion = CompletionModel(self)
        self._backend.capsule_updated.connect(self._route_capsule)
        self._backend.cmdline_updated.connect(self._cmdline.apply)
        self._backend.popupmenu_updated.connect(self._popupmenu.apply)
        self._backend.completions_updated.connect(self._completion.apply)

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

    @property
    def cmdline(self) -> CmdlineState:
        return self._cmdline

    @property
    def popupmenu(self) -> PopupmenuModel:
        return self._popupmenu

    @property
    def completion(self) -> CompletionModel:
        return self._completion


def _qml_dir() -> Path:
    """Resolve the qml/ directory — works both in-tree and when installed.

    In-tree layout (development): project_root/qml/Main.qml
      __file__ is  project_root/src/symmetria_ide/app.py
      parents[2]   is project_root/

    Installed layout: pyproject.toml package-data copies qml/ into the
      package directory alongside this file (symmetria_ide/qml/).
      parents[0] is the package directory.

    Note: Phase 0 is always run in-tree. The installed fallback path is
    provided for completeness but is untested until packaging is wired up.
    """
    in_tree = Path(__file__).resolve().parents[2] / "qml"
    if in_tree.exists():
        return in_tree
    # Installed case: qml/ is expected alongside this module file.
    packaged = Path(__file__).resolve().parent / "qml"
    return packaged


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s — %(message)s",
    )


def _configure_headless_mode(
    controller: "AppController",
    engine: "QQmlApplicationEngine",
    app: "QGuiApplication",
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


def run() -> int:
    _configure_logging()
    # Ctrl-C in the terminal should kill the app, not be caught by Qt.
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    # Request an alpha channel on the default surface BEFORE QGuiApplication
    # spins up the QPA plugin. Without this, Wayland (and X) hand us an
    # opaque framebuffer and `color: "transparent"` in QML has no effect —
    # the compositor composites against black. Must precede app creation.
    fmt = QSurfaceFormat.defaultFormat()
    fmt.setAlphaBufferSize(8)
    QSurfaceFormat.setDefaultFormat(fmt)

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
    engine.rootContext().setContextProperty("cmdlineState", controller.cmdline)
    engine.rootContext().setContextProperty("popupmenuModel", controller.popupmenu)
    engine.rootContext().setContextProperty("completionModel", controller.completion)

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

    shot_path = os.environ.get("SYMMETRIA_IDE_SCREENSHOT")
    test_keys = os.environ.get("SYMMETRIA_IDE_TEST_KEYS")
    if shot_path or test_keys:
        _configure_headless_mode(controller, engine, app, shot_path, test_keys)

    # Everything allocated up to here is long-lived (Qt wrappers, QML
    # engine state, controller, backend). Freeze those objects into the
    # permanent generation so the cyclic collector skips them on every
    # subsequent pass. Combined with the gc-disabled window in
    # `NvimBackend._dispatch_redraw`, this shrinks the "GC runs while
    # Qt renders" race surface that was causing SIGSEGVs under Python
    # 3.14 (see nvim_backend.py for full context).
    gc.collect()
    gc.freeze()

    return app.exec()
