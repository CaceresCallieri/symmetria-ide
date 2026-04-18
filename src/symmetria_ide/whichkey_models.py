"""Which-key overlay QML models: WhichKeyState, WhichKeyModel.

Extracted from `app.py` so the which-key subsystem's state lives next to
its peers. Each class uses `@QmlElement` for QML registration, so this
module must be imported before `QQmlApplicationEngine.load()` runs. That
import (with `noqa: F401`) is kept in `app.py`.

The emission protocol lives in `runtime/lua/orchestrator/whichkey/init.lua`
and the dispatch routing in `app.AppController._on_whichkey_event`.
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
class WhichKeyState(QObject):
    """Visibility + breadcrumb state for the native which-key overlay.

    Per-field `@Property` with notify signals so QML bindings re-evaluate
    when any single field changes (same pattern as `StatusBarState` and
    `CmdlineState`). `visible` drives show/hide; `trail` will carry the
    breadcrumb path (e.g. "<leader> b") once C2 lands real tree data;
    `canGoBack` gates the "⏎ back" hint in the footer.

    Emission protocol is in `runtime/lua/orchestrator/whichkey/init.lua`.
    """

    visibleChanged = Signal()
    trailChanged = Signal()
    canGoBackChanged = Signal()
    modeChanged = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._visible = False
        self._trail = ""
        self._can_go_back = False
        self._mode = "n"

    @Property(bool, notify=visibleChanged)
    def visible(self) -> bool:
        return self._visible

    @Property(str, notify=trailChanged)
    def trail(self) -> str:
        return self._trail

    @Property(bool, notify=canGoBackChanged)
    def canGoBack(self) -> bool:
        return self._can_go_back

    @Property(str, notify=modeChanged)
    def mode(self) -> str:
        return self._mode

    @Slot(dict)
    def apply(self, payload: dict) -> None:
        op = str(payload.get("op") or "")
        if op == "show":
            new_trail = str(payload.get("trail") or "")
            new_can_go_back = bool(payload.get("can_go_back") or False)
            new_mode = str(payload.get("mode") or "n")
            if new_trail != self._trail:
                self._trail = new_trail
                self.trailChanged.emit()
            if new_can_go_back != self._can_go_back:
                self._can_go_back = new_can_go_back
                self.canGoBackChanged.emit()
            if new_mode != self._mode:
                self._mode = new_mode
                self.modeChanged.emit()
            if not self._visible:
                self._visible = True
                self.visibleChanged.emit()
            return
        if op == "hide":
            if self._visible:
                self._visible = False
                self.visibleChanged.emit()
            if self._can_go_back:
                self._can_go_back = False
                self.canGoBackChanged.emit()


@QmlElement
class WhichKeyModel(QAbstractListModel):
    """ListModel of which-key menu entries.

    Each entry has: key (the keystroke label), desc (the description —
    leaf actions OR group label without the `+` prefix; QML decides how
    to render), is_group (bool), icon (nerd-font glyph or empty),
    icon_color (hex string or empty — empty means "no icon").

    Full list replaces on each `show` payload. Hiding clears the list so
    the overlay never shows stale state on the next open.
    """

    KeyRole = Qt.ItemDataRole.UserRole + 1
    DescRole = Qt.ItemDataRole.UserRole + 2
    IsGroupRole = Qt.ItemDataRole.UserRole + 3
    IconRole = Qt.ItemDataRole.UserRole + 4
    IconColorRole = Qt.ItemDataRole.UserRole + 5

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._items: list[dict] = []

    def roleNames(self) -> dict[int, bytes]:
        return {
            self.KeyRole: b"key",
            self.DescRole: b"desc",
            self.IsGroupRole: b"isGroup",
            self.IconRole: b"icon",
            self.IconColorRole: b"iconColor",
        }

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008, ARG002
        return len(self._items)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self._items):
            return None
        item = self._items[index.row()]
        if role == self.KeyRole:
            return item.get("key", "")
        if role == self.DescRole:
            return item.get("desc", "")
        if role == self.IsGroupRole:
            return item.get("is_group", False)
        if role == self.IconRole:
            return item.get("icon", "")
        if role == self.IconColorRole:
            return item.get("icon_color", "")
        return None

    @Slot(dict)
    def apply(self, payload: dict) -> None:
        op = str(payload.get("op") or "")
        if op == "show":
            new_items: list[dict] = []
            for raw in payload.get("items") or ():
                if not isinstance(raw, dict):
                    continue
                new_items.append(
                    {
                        "key": str(raw.get("key") or ""),
                        "desc": str(raw.get("desc") or ""),
                        "is_group": bool(raw.get("is_group") or False),
                        "icon": str(raw.get("icon") or ""),
                        "icon_color": str(raw.get("icon_color") or ""),
                    }
                )
            self.beginResetModel()
            self._items = new_items
            self.endResetModel()
            return
        if op == "hide":
            if self._items:
                self.beginResetModel()
                self._items = []
                self.endResetModel()
