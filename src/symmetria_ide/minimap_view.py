"""Minimap renderer, exposed to QML as `MinimapView`.

Phase 0 — surface skeleton. Paints a single solid rectangle in the
Theme-provided minimap background colour, exposes the two QML
properties (`scrollPosition`, `bufferRowCount`) that subsequent phases
will read, and pre-allocates the gotcha #10 pool (memoized QColor +
pooled QRectF) so the Phase 2 block painter and Phase 5 glyph painter
can extend the same `paint()` without revisiting allocation hygiene.

See `docs/minimap-prd.md` for the full phase sequence. The structural
disciplines pinned here correspond 1:1 with the gotchas listed in
`CLAUDE.md`:

- gotcha #10 — paint allocates no fresh QColor / QRectF wrappers.
  Background colour is constructed once at module load (`_background_color`);
  the paint rect is a pooled QRectF mutated via `setRect()`. Same
  shape as `terminal_view.py` so the discipline is uniform across
  the IDE's `QQuickPaintedItem` subclasses.

- gotcha #23 — no font work in Phase 0; Phase 5 (glyph sprite atlas)
  will reuse `NvimView._default_font()` as the high-res source raster
  so cell metrics line up exactly across the editor and minimap panes.

Theme drift: `_BACKGROUND_RGBA` mirrors `Theme.color.minimap.background`
in `qml/design/Theme.qml`. Until the Theme palette is piped through
Python via a context property (a v2 refactor — would mean an extra
constructor arg or a setter slot), drift is detected by
`tests/test_minimap_view.py::test_background_matches_theme_qml`, same
dual-source-of-truth pattern `_ANSI_PALETTE` uses in `terminal_view.py`.

QML registration: the `@QmlElement` decorator + the module-level
`QML_IMPORT_NAME` / `QML_IMPORT_MAJOR_VERSION` constants are what make
the QML side resolve `MinimapView { … }`. Without the decorator the
side-effect import in `app.py` would succeed but `Main.qml` would
raise "MinimapView is not a type" at engine load. Same registration
contract as `NvimView` and `TerminalView`.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Property, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtQml import QmlElement
from PySide6.QtQuick import QQuickPaintedItem


log = logging.getLogger(__name__)


QML_IMPORT_NAME = "Symmetria.Ide"
QML_IMPORT_MAJOR_VERSION = 1


# Background colour mirrored from Theme.color.minimap.background.
# Stored as a 4-tuple (R, G, B, A) so the alpha (~20%) survives the
# round-trip and the drift-detection test can compare the hex string
# in Theme.qml against the same numeric channels.
#
# 0x33 = 51 / 255 ≈ 20% opacity — paired with the editor's ~60% black
# ambient tint, gives the minimap an effective ~80% dim that reads as
# a perceptibly darker right-edge ribbon while remaining in the
# wallpaper-blend family (no hard edge between editor and minimap).
_BACKGROUND_RGBA: tuple[int, int, int, int] = (0x00, 0x00, 0x00, 0x33)

# Memoized QColor — constructed once at module load so the paint loop
# never instantiates a fresh shiboken wrapper (gotcha #10). The same
# rationale that drives `_qcolor_cache` in `terminal_view.py` and
# `_rgb_to_qcolor` in `nvim_view.py`.
_background_color: QColor = QColor(*_BACKGROUND_RGBA)


@QmlElement
class MinimapView(QQuickPaintedItem):
    """Paints the minimap surface.

    Phase 0 paints a single background rectangle. Subsequent phases
    extend this incrementally:

    - Phase 2 — per-line indent-coloured block render (run-coalesced
      fillRect calls over the pooled `_paint_rect`).
    - Phase 3 — viewport-indicator rectangle overlay (uses
      `scrollPosition` + `bufferRowCount` to map editor viewport
      rows onto minimap y-coords).
    - Phase 4 — left-edge diagnostic + git-diff gutter (4-px column
      reading from a future `MinimapModel` populated by Lua via
      `gitsigns.nvim` + `vim.diagnostic`).
    - Phase 5 — per-cell glyph blits from a pre-rasterized sprite
      atlas (2×4 px target cells, see PRD §9 for the rationale).

    Establishing the gotcha #10 pool in `__init__` now means each
    later phase can layer additional drawing without revisiting the
    allocation hygiene — the discipline is uniform from day one.
    """

    scrollPositionChanged = Signal()
    bufferRowCountChanged = Signal()

    def __init__(self, parent=None) -> None:  # noqa: ANN001
        # Untyped parent matches the NvimView/TerminalView constructors —
        # PySide6 stubs are stricter (QQuickItem|None) than the runtime,
        # which accepts any QObject. Keeping untyped sidesteps the stub
        # mismatch without breaking Qt's metaobject system (gotcha #7).
        super().__init__(parent)

        # No mouse handling in Phase 0. Phase 3 wires the click +
        # drag scrubber through a sibling QML MouseArea (cleaner than
        # routing mouse events through QQuickPaintedItem), so this
        # Python class stays input-free.
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)

        # Transparent backing-store fill — the background colour is
        # painted explicitly inside `paint()` so its alpha composites
        # cleanly over the wallpaper-blend underneath. Same contract
        # NvimView and TerminalView use.
        self.setFillColor(QColor(0, 0, 0, 0))

        # Item flags. The minimap is a visualisation, NOT a focus
        # target — keyboard focus must never land here, so:
        #   - ItemHasContents = True (QML calls paint())
        #   - ItemIsFocusScope deliberately NOT set
        #   - setActiveFocusOnTab(False) so Tab cycling skips the pane
        # Mirrors the focus discipline TerminalView uses (which sets
        # ItemIsFocusScope because the terminal IS a focus target) —
        # the inversion here is intentional.
        self.setFlag(QQuickPaintedItem.Flag.ItemHasContents, True)
        self.setActiveFocusOnTab(False)

        # Pooled QRectF for the background fill — mutated via
        # `setRect()` inside paint(). Even though Phase 0 only does
        # one fill, the pool is established here so Phase 2's per-line
        # block painter can extend the same discipline without
        # revisiting init. Same pattern as TerminalView's `_run_rect`
        # / `_clip_rect` / `_cursor_rect` trio.
        self._paint_rect = QRectF()

        # Backing fields for the QML-visible properties. Phase 0 wires
        # the setters but does nothing with the values; Phase 2 reads
        # `_buffer_row_count` to drive the per-line iteration and
        # Phase 3 reads `_scroll_position` to position the viewport
        # indicator. Setting them now (rather than at first phase that
        # needs them) means the QML bindings on the wrapper side can
        # land immediately — no half-state where the property is
        # missing.
        self._scroll_position = 0.0
        self._buffer_row_count = 0

    # --- QML-visible properties ----------------------------------------
    #
    # Named-function Property form (not the @Property decorator) for
    # read/write properties — same rationale as NvimView.backend:
    # the @Property + @setter pair trips pyright's reportRedeclaration,
    # while the named form reads as a class attribute that pyright
    # handles cleanly.

    def _get_scroll_position(self) -> float:
        return self._scroll_position

    def _set_scroll_position(self, value: float) -> None:
        if value == self._scroll_position:
            return
        self._scroll_position = value
        # Phase 0 ignores the value beyond storing it; Phase 3 will
        # add `self.update()` here so the viewport indicator
        # repaints. The emit is required now so QML bindings on the
        # Main.qml wrapper side actually fire — without the notify
        # signal, QML's binding system caches the initial read and
        # ignores subsequent writes from the NvimView side (cf.
        # CLAUDE.md gotcha #3 — function-call bindings don't
        # re-evaluate).
        self.scrollPositionChanged.emit()

    scrollPosition = Property(
        float,
        _get_scroll_position,
        _set_scroll_position,
        notify=scrollPositionChanged,
    )

    def _get_buffer_row_count(self) -> int:
        return self._buffer_row_count

    def _set_buffer_row_count(self, value: int) -> None:
        if value == self._buffer_row_count:
            return
        self._buffer_row_count = value
        self.bufferRowCountChanged.emit()

    bufferRowCount = Property(
        int,
        _get_buffer_row_count,
        _set_buffer_row_count,
        notify=bufferRowCountChanged,
    )

    # --- Painting ------------------------------------------------------

    def paint(self, painter: QPainter) -> None:
        """Phase 0 — single background fill.

        Phase 2 will extend this into a per-line block render iterating
        over a future `MinimapModel`'s line count; the pooled
        `_paint_rect` + memoized `_background_color` are already in
        place for that extension. No allocation here — every QPainter
        op takes pre-existing wrappers (gotcha #10).
        """
        bounds = self.boundingRect()
        # Pooled QRectF — `setRect` from boundingRect's coords so
        # paint never instantiates a fresh QRectF (gotcha #10). Same
        # rationale as `_clip_rect.setRect(...)` in TerminalView.paint.
        self._paint_rect.setRect(0.0, 0.0, bounds.width(), bounds.height())
        painter.fillRect(self._paint_rect, _background_color)
