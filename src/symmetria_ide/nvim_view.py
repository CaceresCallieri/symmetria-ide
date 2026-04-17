"""NeoVim grid renderer, exposed to QML as `NvimView`.

Subclasses `QQuickPaintedItem` — the standard way to draw custom 2-D
content inside a QML scene. Each cell is painted as a rectangle of
background color plus its character; the cursor is a reverse-video
block on top.

Resizing the QML item recomputes the cell grid size and pushes the new
dimensions to `NvimBackend.resize`, so NeoVim itself reflows to match.
"""

from __future__ import annotations

from PySide6.QtCore import Property, QObject, QRectF, QSize, Qt, Signal, Slot
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QFontMetricsF,
    QKeyEvent,
    QPainter,
)
from PySide6.QtQml import QmlElement
from PySide6.QtQuick import QQuickPaintedItem

from .grid import Grid, HlAttr
from .keys import translate as translate_key
from .nvim_backend import NvimBackend


QML_IMPORT_NAME = "Symmetria.Ide"
QML_IMPORT_MAJOR_VERSION = 1


def _rgb_to_qcolor(value: int | None, fallback: int) -> QColor:
    """Convert a 24-bit RGB integer (NeoVim's rgb_attr format) to QColor."""
    if value is None:
        value = fallback
    r = (value >> 16) & 0xFF
    g = (value >> 8) & 0xFF
    b = value & 0xFF
    return QColor(r, g, b)


@QmlElement
class NvimView(QQuickPaintedItem):
    """Paints the backend's `Grid` into the QML scene.

    The QML side sets `backend` once (from `Main.qml`); the view then
    listens for `redraw_flushed` and triggers `update()` so Qt's render
    thread repaints in the next frame.
    """

    backendChanged = Signal()
    cellMetricsChanged = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        # ItemHasContents: tells the scene graph this item paints pixels.
        self.setFlag(QQuickPaintedItem.Flag.ItemHasContents, True)
        # ItemIsFocusScope: makes this item a focus boundary so Tab
        # navigation stops here rather than passing through to QML peers.
        self.setFlag(QQuickPaintedItem.Flag.ItemIsFocusScope, True)
        # ItemAcceptsInputMethod: required for IME / compose-key support
        # on Wayland so the compositor routes input method events here.
        self.setFlag(QQuickPaintedItem.Flag.ItemAcceptsInputMethod, True)
        self.setActiveFocusOnTab(True)

        self._font = self._default_font()
        self._metrics = QFontMetricsF(self._font)
        self._cell_w = max(1.0, self._metrics.horizontalAdvance("M"))
        self._cell_h = max(1.0, self._metrics.height())
        # _ascent is reserved for future baseline-accurate text placement
        # (drawing text at `y + ascent` rather than relying on AlignVCenter).
        self._ascent = self._metrics.ascent()

        # Pre-build bold/italic font variants so the paint loop doesn't
        # allocate transient QFont objects per run. Key is (bold, italic).
        self._font_variants: dict[tuple[bool, bool], QFont] = {}
        for bold in (False, True):
            for italic in (False, True):
                if bold or italic:
                    variant = QFont(self._font)
                    variant.setBold(bold)
                    variant.setItalic(italic)
                    self._font_variants[(bold, italic)] = variant

        self._backend: NvimBackend | None = None
        self._cols = 0
        self._rows = 0

    # --- Font setup ----------------------------------------------------

    @staticmethod
    def _default_font() -> QFont:
        """Pick the first installed monospace font we recognize.

        Falls back to whatever Qt resolves as the system fixed-pitch
        font. Keeping this deliberately conservative — Phase 0 does not
        expose font configuration to the user yet.
        """
        preferred = [
            "Iosevka",
            "JetBrains Mono",
            "Fira Code",
            "Cascadia Code",
            "Source Code Pro",
            "Hack",
            "DejaVu Sans Mono",
        ]
        for name in preferred:
            if name in QFontDatabase.families():
                font = QFont(name)
                font.setPointSize(11)
                font.setStyleHint(QFont.StyleHint.Monospace)
                font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
                return font
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        font.setPointSize(11)
        return font

    # --- QML-visible properties ----------------------------------------

    @Property(QObject, notify=backendChanged)
    def backend(self) -> NvimBackend | None:
        return self._backend

    @backend.setter
    def backend(self, value: NvimBackend | None) -> None:
        if value is self._backend:
            return
        if self._backend is not None:
            try:
                self._backend.redraw_flushed.disconnect(self._on_redraw_flushed)
            except (RuntimeError, TypeError):
                pass
        self._backend = value
        if value is not None:
            value.redraw_flushed.connect(self._on_redraw_flushed)
            self._push_current_size()
        self.backendChanged.emit()

    @Property(float, notify=cellMetricsChanged)
    def cellWidth(self) -> float:
        return self._cell_w

    @Property(float, notify=cellMetricsChanged)
    def cellHeight(self) -> float:
        return self._cell_h

    # --- Backend → view ------------------------------------------------

    @Slot()
    def _on_redraw_flushed(self) -> None:
        self.update()

    # --- Resizing ------------------------------------------------------

    def geometryChange(self, new_geom, old_geom) -> None:  # noqa: ANN001
        super().geometryChange(new_geom, old_geom)
        self._push_current_size()

    def _push_current_size(self) -> None:
        if self._backend is None:
            return
        w = self.width()
        h = self.height()
        if w <= 0 or h <= 0:
            return
        cols = max(20, int(w // self._cell_w))
        rows = max(5, int(h // self._cell_h))
        if (cols, rows) != (self._cols, self._rows):
            self._cols = cols
            self._rows = rows
            self._backend.resize(cols, rows)

    # --- Painting ------------------------------------------------------

    def paint(self, painter: QPainter) -> None:
        if self._backend is None:
            painter.fillRect(self.boundingRect(), QColor(30, 30, 30))
            return
        grid: Grid = self._backend.grid
        painter.setFont(self._font)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        default_fg = grid.default_fg
        default_bg = grid.default_bg
        painter.fillRect(self.boundingRect(), _rgb_to_qcolor(default_bg, 0x1E1E1E))

        cw = self._cell_w
        ch = self._cell_h

        for r in range(grid.rows):
            y = r * ch
            c = 0
            while c < grid.cols:
                cell = grid.cells[r][c]
                attr = grid.hl_attrs.get(cell.hl_id, HlAttr())
                fg_val = attr.foreground if attr.foreground is not None else default_fg
                bg_val = attr.background if attr.background is not None else default_bg
                if attr.reverse:
                    fg_val, bg_val = bg_val, fg_val

                # Coalesce consecutive cells with the same attrs into one
                # fillRect + drawText call. Saves many painter state
                # changes when large regions share a highlight.
                run_start = c
                run_chars: list[str] = [cell.char]
                c += 1
                while c < grid.cols:
                    nxt = grid.cells[r][c]
                    if nxt.hl_id != cell.hl_id:
                        break
                    run_chars.append(nxt.char)
                    c += 1

                rect = QRectF(run_start * cw, y, (c - run_start) * cw, ch)
                painter.fillRect(rect, _rgb_to_qcolor(bg_val, default_bg))

                # Use the pre-cached variant — avoids allocating a QFont
                # copy per run on every frame.
                if attr.bold or attr.italic:
                    painter.setFont(
                        self._font_variants.get((attr.bold, attr.italic), self._font)
                    )
                else:
                    painter.setFont(self._font)

                painter.setPen(_rgb_to_qcolor(fg_val, default_fg))
                painter.drawText(
                    QRectF(run_start * cw, y, (c - run_start) * cw, ch),
                    int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                    "".join(run_chars),
                )

        # Cursor: simple reverse block for Phase 0. Mode-aware shapes
        # (bar for insert, underline for replace) come in Phase 3 when
        # we extract the command line / messages — for now, one shape.
        cur_row = grid.cursor_row
        cur_col = grid.cursor_col
        if 0 <= cur_row < grid.rows and 0 <= cur_col < grid.cols:
            cursor_cell = grid.cells[cur_row][cur_col]
            cursor_rect = QRectF(cur_col * cw, cur_row * ch, cw, ch)
            painter.fillRect(cursor_rect, _rgb_to_qcolor(default_fg, 0xD0D0D0))
            painter.setPen(_rgb_to_qcolor(default_bg, 0x1E1E1E))
            painter.drawText(
                cursor_rect,
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                cursor_cell.char,
            )

    # --- Keyboard ------------------------------------------------------

    def keyPressEvent(self, event: QKeyEvent) -> None:
        keys = translate_key(event.key(), event.text(), event.modifiers())
        if keys is None:
            event.ignore()
            return
        if self._backend is not None:
            self._backend.input(keys)
        event.accept()

    def sizeHint(self) -> QSize:
        return QSize(int(120 * self._cell_w), int(30 * self._cell_h))
