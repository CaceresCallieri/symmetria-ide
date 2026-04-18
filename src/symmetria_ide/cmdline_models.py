"""Cmdline-overlay QML models: CmdlineState, PopupmenuModel, CompletionModel.

Extracted from `app.py` so cmdline-subsystem state lives next to its peers
rather than alongside `AppController`/`run()`. Each class uses `@QmlElement`
for QML registration, so this module must be imported (even if nothing
references the names directly) before `QQmlApplicationEngine.load()` runs.
That's why `app.py` keeps `from .cmdline_models import ... # noqa: F401`.

See CLAUDE.md "The completion pipeline" for how these models connect to
the Lua runtime's `getcompletion()` emitter.
"""

from __future__ import annotations

from PySide6.QtCore import (
    Property,
    QAbstractListModel,
    QModelIndex,
    QObject,
    Qt,
    Signal,
    Slot,
)
from PySide6.QtQml import QmlElement

QML_IMPORT_NAME = "Symmetria.Ide"
QML_IMPORT_MAJOR_VERSION = 1


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
