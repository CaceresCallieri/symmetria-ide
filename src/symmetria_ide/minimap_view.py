"""Minimap renderer, exposed to QML as `MinimapView`.

Phase 2 of docs/minimap-prd.md — block-mode painter that draws one
horizontal bar per buffer line, coloured by leading-whitespace indent
depth (4 rungs). The resulting silhouette gives an at-a-glance view
of document shape (function boundaries, indent structure, paragraph
spacing) without needing the per-character glyph renderer Phase 5
delivers.

See `docs/minimap-prd.md` for the full phase sequence. Disciplines
pinned upfront so subsequent phases (3: viewport indicator, 4:
diagnostic gutter, 5: glyph atlas) can extend `paint()` without
reintroducing gotcha #10 hazards:

- gotcha #10 — paint() allocates no fresh QColor / QRectF wrappers.
  Background + indent palette are memoized at module load
  (`_background_color`, `_indent_colors`); the paint rect is a single
  pooled QRectF mutated via `setRect()` once per buffer line. Same
  shape as `terminal_view.py` so the discipline is uniform across
  the IDE's `QQuickPaintedItem` subclasses.

- gotcha #23 — no font work in Phase 2; Phase 5 (glyph sprite atlas)
  will reuse `NvimView._default_font()` as the high-res source raster
  so cell metrics line up exactly across the editor and minimap panes.

Theme drift: `_BACKGROUND_RGBA` mirrors `Theme.color.minimap.background`
in `qml/design/Theme.qml`, and `_INDENT_RGBA` mirrors
`Theme.color.minimap.indent.level0..level3`. Until the Theme palette
is piped through Python via a context property (a v2 refactor — would
mean an extra constructor arg or a setter slot), drift is detected by
`tests/test_minimap_view.py::test_background_matches_theme_qml` and
`test_indent_palette_matches_theme_qml`, same dual-source-of-truth
pattern `_ANSI_PALETTE` uses in `terminal_view.py`.

QML registration: the `@QmlElement` decorator + the module-level
`QML_IMPORT_NAME` / `QML_IMPORT_MAJOR_VERSION` constants are what make
the QML side resolve `MinimapView { … }`. Without the decorator the
side-effect import in `app.py` would succeed but `Main.qml` would
raise "MinimapView is not a type" at engine load. Same registration
contract as `NvimView` and `TerminalView`.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Property, QObject, QRectF, Qt, Signal, Slot
from PySide6.QtGui import QColor, QPainter
from PySide6.QtQml import QmlElement
from PySide6.QtQuick import QQuickPaintedItem

from .minimap_model import MinimapModel


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


# Indent block palette — Phase 2 mirror of `Theme.color.minimap.indent`.
# Four rungs, brightest-at-top so top-level structure pops in the
# silhouette. Stored as 3-tuples (R, G, B) — these are opaque colours
# (the background already supplies the wallpaper-blend), so no alpha
# channel needed. See Theme.qml's `minimap.indent` block for the
# tone-gradient rationale.
_INDENT_RGBA: tuple[tuple[int, int, int], ...] = (
    (0xC8, 0xA3, 0x7A),  # level 0 — accent.primary, top-level (brightest)
    (0x9A, 0x85, 0x68),  # level 1 — function-body level
    (0x6E, 0x60, 0x55),  # level 2 — conditional / loop body
    (0x52, 0x48, 0x3F),  # level 3 — deep nesting (faintest, still legible)
)

# Memoized indent QColors. Module-level construction so the paint loop
# only ever indexes the tuple — never allocates. Same pattern as the
# background colour above.
_indent_colors: tuple[QColor, ...] = tuple(
    QColor(r, g, b) for (r, g, b) in _INDENT_RGBA
)


# Viewport indicator palette — Phase 3 mirror of
# `Theme.color.minimap.viewportFill` / `viewportFrame`. Stored as
# 4-tuples (R, G, B, A) so the alpha survives the round-trip and the
# drift-detection test can compare the AARRGGBB hex strings in Theme.qml
# against the same numeric channels.
#
# Fill ≈ 10% white over the indent silhouette — perceptible spotlight
# without obscuring the underlying indent colour. Frame ≈ 40% white,
# painted as 1-px hairlines at the top and bottom of the visible range
# (aliases `Theme.accent.focus`, the existing focus-indicator vocabulary).
_VIEWPORT_FILL_RGBA: tuple[int, int, int, int] = (0xFF, 0xFF, 0xFF, 0x1A)
_VIEWPORT_FRAME_RGBA: tuple[int, int, int, int] = (0xFF, 0xFF, 0xFF, 0x66)

# Memoized — same gotcha #10 discipline as the indent palette.
_viewport_fill_color: QColor = QColor(*_VIEWPORT_FILL_RGBA)
_viewport_frame_color: QColor = QColor(*_VIEWPORT_FRAME_RGBA)


# Viewport frame hairline thickness. 1 px is the visual sweet spot —
# thicker than that competes with the silhouette underneath, thinner
# than that vanishes at HiDPI scaling. The Top + Bottom 1-px edges
# form a brackets-style indicator rather than a full outlined box;
# vertical sides would over-frame the already-narrow ribbon.
_VIEWPORT_FRAME_THICKNESS_PX = 1.0


# Layout constants for the block-mode painter.

# Minimum pixel height per rendered minimap row. At 1 px the silhouette
# becomes too noisy to read; 2 px is the floor that still preserves
# legible structure. Buffers small enough to render every row at >= 2 px
# get the natural row height; larger buffers compress to fit the view
# but never below this floor (deep documents will still show the top
# portion at 2 px even if they overflow — Phase 3's viewport indicator
# is the user's anchor for scroll position).
_MIN_ROW_HEIGHT_PX = 2.0

# Horizontal indent step per level. A 4-px step means level-3
# (deepest) bars start 12 px in from the left edge of the minimap;
# at the default 80-px minimap width that leaves ~68 px for the
# coloured bar, easily wide enough to read. Tune-down to 3 if the
# minimap width shrinks under 70 px; tune-up to 5/6 if the silhouette
# reads too flat on shallow-indent codebases.
_INDENT_STEP_PX = 4.0


@QmlElement
class MinimapView(QQuickPaintedItem):
    """Paints the minimap surface.

    Phase 2 paints a per-line indent silhouette over the background
    ribbon. Subsequent phases extend this incrementally:

    - Phase 3 — viewport-indicator rectangle overlay (uses
      `scrollPosition` + `bufferRowCount` to map editor viewport
      rows onto minimap y-coords).
    - Phase 4 — left-edge diagnostic + git-diff gutter (4-px column
      reading from a future model surface populated by Lua via
      `gitsigns.nvim` + `vim.diagnostic`).
    - Phase 5 — per-cell glyph blits from a pre-rasterized sprite
      atlas (2×4 px target cells, see PRD §9 for the rationale).

    Establishing the gotcha #10 pool in `__init__` now means each
    later phase can layer additional drawing without revisiting the
    allocation hygiene — the discipline is uniform from day one.
    """

    scrollPositionChanged = Signal()
    bufferRowCountChanged = Signal()
    modelChanged = Signal()

    def __init__(self, parent=None) -> None:  # noqa: ANN001
        # Untyped parent matches the NvimView/TerminalView constructors —
        # PySide6 stubs are stricter (QQuickItem|None) than the runtime,
        # which accepts any QObject. Keeping untyped sidesteps the stub
        # mismatch without breaking Qt's metaobject system (gotcha #7).
        super().__init__(parent)

        # No mouse handling in Phase 2. Phase 3 wires the click +
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

        # Pooled QRectF for paint(). One pool entry suffices — the
        # background fill, every per-line block, and (future) the
        # viewport indicator all mutate this same rect via setRect().
        # Phase 2 deliberately does NOT introduce additional pool
        # entries; if Phase 3+ needs concurrent rect bookkeeping
        # (e.g. for the viewport overlay drawn on top of blocks),
        # add a second pool entry rather than re-using this one
        # mid-paint — same hygiene as TerminalView's _run_rect /
        # _clip_rect / _cursor_rect separation.
        self._paint_rect = QRectF()

        # Backing fields for the QML-visible properties.
        self._scroll_position = 0.0
        self._buffer_row_count = 0
        # MinimapModel reference — wired via QML setter pattern
        # matching `NvimView.backend` / `TerminalView.backend`. None
        # is a legitimate state at construction time (before QML
        # assigns `model: minimapModel`). paint() bails out cleanly
        # on None, falling through to the Phase 0 background-only
        # render path.
        self._model: MinimapModel | None = None

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
        # Phase 3 uses the model-signal path (viewportChanged →
        # _on_viewport_changed → update()) rather than repaint-on-
        # scroll-position, so this setter intentionally does NOT call
        # self.update(). The emit is still required so QML bindings on
        # the Main.qml wrapper side actually fire — without the notify
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
        # Buffer row count drives the per-line iteration extent — when
        # it changes, repaint so the silhouette reflects the new
        # length. (linesChanged is the primary repaint trigger via the
        # model; this is the belt-and-braces case where bufferRowCount
        # ticks without a linesChanged having reached us yet.)
        self.update()

    bufferRowCount = Property(
        int,
        _get_buffer_row_count,
        _set_buffer_row_count,
        notify=bufferRowCountChanged,
    )

    # MinimapModel injection — QML side assigns
    #   MinimapView { model: minimapModel }
    # in Main.qml. The setter manages signal lifecycle: disconnects
    # from the prior model's linesChanged (if any), stores the new
    # model, and wires its linesChanged → self.update() so content
    # mutations repaint immediately. Mirrors the NvimView.backend
    # setter shape so future readers find a familiar pattern.

    def _get_model(self) -> MinimapModel | None:
        return self._model

    def _set_model(self, value: MinimapModel | None) -> None:
        if value is self._model:
            return
        if self._model is not None:
            # Tolerate already-disconnected (model handover during
            # teardown) — same defensive disconnect TerminalView uses.
            try:
                self._model.linesChanged.disconnect(self._on_lines_changed)
            except (RuntimeError, TypeError):
                pass
            try:
                self._model.viewportChanged.disconnect(self._on_viewport_changed)
            except (RuntimeError, TypeError):
                pass
        self._model = value
        if value is not None:
            # Direct connection — MinimapModel.apply already runs on
            # the GUI thread (queued from NvimBackend.minimap_event in
            # AppController), so linesChanged is GUI-thread by the
            # time it reaches us. No QueuedConnection needed here.
            value.linesChanged.connect(self._on_lines_changed)
            # Same GUI-thread guarantee for viewportChanged — the
            # queued connection at NvimBackend.minimap_viewport_event
            # marshals to the GUI thread; viewportChanged emits from
            # apply_viewport, which already runs there.
            value.viewportChanged.connect(self._on_viewport_changed)
            # Immediate repaint so the first model that arrives (often
            # carrying the initial snapshot already) shows content
            # without waiting for a subsequent edit to trip linesChanged.
            self.update()
        self.modelChanged.emit()

    model = Property(QObject, _get_model, _set_model, notify=modelChanged)

    # --- Model → view --------------------------------------------------

    @Slot()
    def _on_viewport_changed(self) -> None:
        """Repaint when the editor's viewport range changes. Phase 3
        viewport indicator chases the new bounds; the silhouette
        below is unchanged but a single update() repaints both the
        blocks and the overlay (cheap; the block loop is O(line_count)
        but the constant is tiny — see PRD §6 R2.1)."""
        self.update()

    @Slot(int, int)
    def _on_lines_changed(self, first: int, last: int) -> None:
        """Request a repaint when the model's content mutates.

        v1 ignores the (first, last) range and repaints the whole
        minimap — block-mode is cheap (~100k lines completes well
        under one frame on the test rig) and a partial-update path
        adds bookkeeping cost that doesn't pay off until the buffer
        sizes get extreme. A v2 optimisation would map the line
        range to a y-coord range and call `update(QRect)` instead;
        the gating signal is `_on_lines_changed`'s del-of-args
        becoming a real cost on large buffers.
        """
        del first, last
        self.update()

    # --- Painting ------------------------------------------------------

    def paint(self, painter: QPainter) -> None:
        """Phase 2 — background fill + per-line indent blocks.

        Order of operations:
        1. Fill the full ribbon with the background colour (preserves
           the wallpaper-blend feel; subsequent fills layer on top).
        2. If no model is attached (Phase 0 fallback path) or the
           model is empty, stop after step 1. The Theme background is
           still useful as a visual placeholder.
        3. Compute row_height as `max(_MIN_ROW_HEIGHT_PX,
           view_height / line_count)` — small buffers paint at the
           floor and don't fill the column; large buffers compress to
           fit. The painter only iterates rows whose y-coords overlap
           `boundingRect()`, so a 100k-line buffer at floor height
           skips most of the loop body.
        4. For each visible row, look up the cached indent level via
           `model.indent_level(i)`, mutate the pooled rect to the
           (indent-step inset, y, remaining width, row_height - 1px gap)
           dimensions, fill with the memoized indent colour.

        gotcha #10 invariants:
        - No QColor construction; reads `_background_color` +
          `_indent_colors[level]` from module-level memoized values.
        - No QRectF construction; mutates `self._paint_rect` in place
          via `setRect()`.
        - No string allocation; `model.indent_level()` reads a cached
          int array — no `str.lstrip()` per row (PRD §6 R2.2).
        """
        bounds = self.boundingRect()
        view_w = bounds.width()
        view_h = bounds.height()

        # Step 1 — background fill. Always paint, even when the model
        # is empty / unattached, so the ribbon is visible as a
        # placeholder.
        self._paint_rect.setRect(0.0, 0.0, view_w, view_h)
        painter.fillRect(self._paint_rect, _background_color)

        # Step 2 — bail out if there's nothing to render. The
        # background-only state is the legitimate Phase 0 fallback;
        # the minimap doesn't disappear, it just shows the placeholder.
        if self._model is None:
            return
        line_count = self._model.line_count()
        if line_count <= 0:
            return

        # Step 3 — compute row height. The minimum floor keeps the
        # silhouette legible even when a buffer would compress to
        # sub-pixel rows; very deep buffers overflow off the bottom
        # rather than compressing into mush. Phase 3's viewport
        # indicator will be the user's anchor for "where am I in
        # the file" — the silhouette tells shape, the indicator
        # tells position.
        natural_h = view_h / line_count
        row_h = _MIN_ROW_HEIGHT_PX if natural_h < _MIN_ROW_HEIGHT_PX else natural_h

        # Step 4 — per-line blocks. Iterate only rows whose y-band
        # overlaps the visible bounds. `max_drawable_rows` caps the
        # iteration; for buffers that fit, we draw every row; for
        # buffers that overflow, the bottom rows fall off-pane
        # (acceptable per the design — see step 3 rationale).
        max_drawable_rows = int(view_h / row_h) + 1
        rows_to_draw = (
            line_count if line_count < max_drawable_rows else max_drawable_rows
        )
        # `row_h - 1` leaves a 1-px gap between rows — visually
        # separates adjacent blocks so the silhouette reads as
        # discrete lines, not a solid mass. `_MIN_ROW_HEIGHT_PX` >= 2.0
        # (enforced by test_layout_constants_within_sane_bounds) guarantees
        # block_h = row_h - 1.0 >= 1.0, so the gap is always achievable.
        block_h = row_h - 1.0
        # Per-iteration locals lifted out of the loop. Even though
        # the loop body is shape-stable, hoisting attribute lookups
        # is the same hot-path discipline NvimView's _paint_row uses.
        rect = self._paint_rect
        indent_level = self._model.indent_level
        fill_rect = painter.fillRect
        indent_step = _INDENT_STEP_PX
        for row_idx in range(rows_to_draw):
            level = indent_level(row_idx)
            x_offset = level * indent_step
            block_w = view_w - x_offset
            if block_w <= 0.0:
                # Defensive: if the minimap is narrower than the deepest
                # indent's offset, skip the row rather than emit a
                # negative-width fillRect (Qt would paint a 0-width
                # nothing but the call still costs).
                continue
            y = row_idx * row_h
            rect.setRect(x_offset, y, block_w, block_h)
            fill_rect(rect, _indent_colors[level])

        # Step 5 — viewport indicator overlay. Skip cleanly when the
        # editor hasn't reported any viewport bounds yet (e.g. terminal-
        # first startup: no apply_viewport has fired). The fill +
        # top/bottom frame composite over the indent silhouette
        # painted above.
        viewport_count = self._model.viewport_count()
        if viewport_count <= 0:
            return
        viewport_first = self._model.viewport_first()
        # Clamp the visible range to actual buffer extent — a viewport
        # that extends past the buffer's end (because the buffer
        # shrank and the viewport bookkeeping is one tick behind) just
        # truncates rather than running off into negative-height land.
        viewport_end_row = viewport_first + viewport_count
        if viewport_end_row > line_count:
            viewport_end_row = line_count
        if viewport_first >= viewport_end_row:
            return
        # Convert buffer-row range to pixel y-range using the same
        # `row_h` already established for the silhouette. The indicator
        # tracks the silhouette exactly because they share the formula.
        vp_y_top = viewport_first * row_h
        vp_y_bot = viewport_end_row * row_h
        vp_height = vp_y_bot - vp_y_top
        # 5a — translucent fill covering the visible band.
        rect.setRect(0.0, vp_y_top, view_w, vp_height)
        fill_rect(rect, _viewport_fill_color)
        # 5b — 1-px hairline at top edge. Painted AT vp_y_top, so it
        # sits on the inner boundary of the fill (the eye reads the
        # frame as the start of the viewport, not just above it).
        rect.setRect(0.0, vp_y_top, view_w, _VIEWPORT_FRAME_THICKNESS_PX)
        fill_rect(rect, _viewport_frame_color)
        # 5c — 1-px hairline at bottom edge. Positioned so its bottom
        # aligns with vp_y_bot (the inner edge).
        rect.setRect(
            0.0,
            vp_y_bot - _VIEWPORT_FRAME_THICKNESS_PX,
            view_w,
            _VIEWPORT_FRAME_THICKNESS_PX,
        )
        fill_rect(rect, _viewport_frame_color)
