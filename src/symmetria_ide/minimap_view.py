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
  pooled QRectF mutated via `setRect()` once per buffer line. This is
  the only `QQuickPaintedItem` left in the IDE (the grid + pyte terminal
  renderers were deleted); the discipline is mandated by project-standards
  §5 for any paint hot path.

- gotcha #23 — no font work in Phase 2; Phase 5 (glyph sprite atlas)
  will reuse `editor_font.default_font()` as the high-res source raster
  so cell metrics line up exactly across the editor and minimap panes.

Theme drift: `_BACKGROUND_RGBA` mirrors `Theme.color.minimap.background`
in `qml/design/Theme.qml`, and `_INDENT_RGBA` mirrors
`Theme.color.minimap.indent.level0..level3`. Until the Theme palette
is piped through Python via a context property (a v2 refactor — would
mean an extra constructor arg or a setter slot), drift is detected by
`tests/test_minimap_view.py::test_background_matches_theme_qml` and
`test_indent_palette_matches_theme_qml` — the same dual-source-of-truth
shape the terminal palette uses (Theme.qml mirrored by the qmltermwidget
fork's `Symmetria.colorscheme`).

QML registration: the `@QmlElement` decorator + the module-level
`QML_IMPORT_NAME` / `QML_IMPORT_MAJOR_VERSION` constants are what make
the QML side resolve `MinimapView { … }`. Without the decorator the
side-effect import in `app.py` would succeed but `Main.qml` would
raise "MinimapView is not a type" at engine load. Same registration
contract as the other `@QmlElement` models (CmdlineState, WhichKeyModel,
etc.) registered via side-effect import in `app.py`.
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
# never instantiates a fresh shiboken wrapper (gotcha #10, the cache-every-
# wrapper rule project-standards §5 mandates for any paint hot path).
_background_color: QColor = QColor(*_BACKGROUND_RGBA)


# Indent block palette — Phase 2 mirror of `Theme.color.minimap.indent`.
# Four rungs, brightest-at-top so top-level structure pops in the
# silhouette. Stored as 3-tuples (R, G, B) — these are opaque colours
# (the background already supplies the wallpaper-blend), so no alpha
# channel needed. See Theme.qml's `minimap.indent` block for the
# tone-gradient rationale.
_INDENT_RGBA: tuple[tuple[int, int, int], ...] = (
    # Neutral gray ramp tracking Theme.text.* family. See Theme.qml
    # `minimap.indent` block for the rationale on shifting from the
    # warm-amber palette to neutrals (the silhouette reads as text,
    # not chrome). Phase 6's off-viewport syntax-highlight pipeline
    # will eventually replace this fixed ramp with per-row colors
    # from the editor's actual treesitter highlights.
    # Re-derived with the flat-aesthetic palette move: Theme.text.* came down
    # a rung, so these followed to keep the annotations honest.
    #
    # ⚠ These track the text ramp's LUMINANCE, not its exact hex. The flat
    # palette's text rungs carry a slight cool tint (text.normal is #a8a8ae),
    # while this ramp must stay strictly R == G == B — the Phase 4.5 decision
    # that the silhouette reads as text rather than as tinted chrome, enforced
    # by `test_indent_palette_is_neutral_gray`. So each rung is its text
    # counterpart with the tint removed, not a copy of it.
    (0xE4, 0xE4, 0xE4),  # level 0 — text.emphasis luminance (brightest)
    (0xA8, 0xA8, 0xA8),  # level 1 — text.normal luminance
    (0x7E, 0x7E, 0x7E),  # level 2 — mid-tone
    (0x52, 0x52, 0x52),  # level 3 — deep nesting (quietest)
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


# Diagnostic gutter palette — Phase 4 mirror of
# `Theme.color.minimap.diagnostic.{error, warn, info, hint}`. Stored
# as 3-tuples (R, G, B) — fully opaque markers so the gutter dot
# stands out even when the indent silhouette underneath is in a
# similar tone family. Order matters: severity rank should map to
# index, with `error` brightest / most urgent at index 0. The
# `_DIAGNOSTIC_KEY_TO_COLOR_INDEX` dict ties wire-format severity
# strings to the tuple position so a future palette extension just
# adds an entry.
_DIAGNOSTIC_RGBA: tuple[tuple[int, int, int], ...] = (
    (0xD2, 0x60, 0x2D),  # error — Theme.mode.replace (wine_theme.error_red)
    (0xC2, 0x8B, 0x12),  # warn  — Theme.mode.normal  (wine_theme.keyword)
    (0x6D, 0x94, 0xE9),  # info  — Theme.mode.command (wine_theme.accent_blue)
    # `hint` is the one entry aliasing a NEUTRAL token rather than an accent,
    # so it is the one that moved with the flat-aesthetic palette (text.dim
    # #7a7a7a -> #6e6e73). The three accent rows above are wine_theme-derived
    # and deliberately did not move.
    (0x6E, 0x6E, 0x73),  # hint  — Theme.text.dim
)

# Map wire-format severity string to its index in `_DIAGNOSTIC_RGBA`.
# A diagnostic_at() lookup that returns a string not in this dict
# is silently skipped at paint time — keeps the painter loop crash-
# free if a future severity is added to vim.diagnostic before we
# update the palette.
_DIAGNOSTIC_KEY_TO_COLOR_INDEX: dict[str, int] = {
    "error": 0,
    "warn": 1,
    "info": 2,
    "hint": 3,
}

_diagnostic_colors: tuple[QColor, ...] = tuple(
    QColor(r, g, b) for (r, g, b) in _DIAGNOSTIC_RGBA
)


# Git-diff gutter palette — Phase 4 mirror of
# `Theme.color.minimap.gitDiff.{added, modified, deleted}`. Same
# opaque-3-tuple shape as the diagnostic palette.
_GITDIFF_RGBA: tuple[tuple[int, int, int], ...] = (
    (0x62, 0xBA, 0x46),  # added    — Theme.mode.insert  (wine_theme.string)
    (0xC8, 0xA3, 0x7A),  # modified — Theme.accent.primary
    (0xD2, 0x60, 0x2D),  # deleted  — Theme.mode.replace (wine_theme.error_red)
)

_GITDIFF_KEY_TO_COLOR_INDEX: dict[str, int] = {
    "added": 0,
    "modified": 1,
    "deleted": 2,
}

_gitdiff_colors: tuple[QColor, ...] = tuple(
    QColor(r, g, b) for (r, g, b) in _GITDIFF_RGBA
)


# Gutter column width — Phase 4. 4 px is the floor that's still
# perceptible as a coloured stripe at minimap scale; below that the
# bar reads as noise rather than signal. Sits at x=0 to
# _GUTTER_WIDTH_PX; the indent silhouette starts at x=_GUTTER_WIDTH_PX
# so the two regions don't overlap.
_GUTTER_WIDTH_PX = 4.0


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

# Pixels per character in the minimap silhouette — Phase 4.5.
# Painter clips each row's block width to `content_length * _CHAR_WIDTH_PX`
# (clamped to the available width). 0.7 px/char means a 100-char line
# occupies ~70 px (most of the 80 px minimap minus gutter); shorter
# lines render as proportionally shorter bars and blanks render as
# gaps. This gives the silhouette real document-shape fidelity
# instead of a wall of full-width bars.
#
# Tune-up to 0.9 if your project mostly uses ≤80-char lines and you
# want the bars to fill more; tune-down to 0.55 if you regularly
# exceed 130-char lines and want every line to fit without clipping.
_CHAR_WIDTH_PX = 0.7


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
        # Untyped parent: PySide6 stubs are stricter (QQuickItem|None) than the
        # runtime, which accepts any QObject. Keeping it untyped sidesteps the
        # stub mismatch without breaking Qt's metaobject system (gotcha #7).
        super().__init__(parent)

        # No mouse handling in Phase 2. Phase 3 wires the click +
        # drag scrubber through a sibling QML MouseArea (cleaner than
        # routing mouse events through QQuickPaintedItem), so this
        # Python class stays input-free.
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)

        # Transparent backing-store fill — the background colour is
        # painted explicitly inside `paint()` so its alpha composites
        # cleanly over the wallpaper-blend underneath. Same transparent-fill
        # contract the QMLTermWidget terminal panes use.
        self.setFillColor(QColor(0, 0, 0, 0))

        # Item flags. The minimap is a visualisation, NOT a focus
        # target — keyboard focus must never land here, so:
        #   - ItemHasContents = True (QML calls paint())
        #   - ItemIsFocusScope deliberately NOT set
        #   - setActiveFocusOnTab(False) so Tab cycling skips the pane
        # The terminal panes ARE focus targets (they grab keyboard focus);
        # the minimap deliberately inverts that — it must never accept focus.
        self.setFlag(QQuickPaintedItem.Flag.ItemHasContents, True)
        self.setActiveFocusOnTab(False)

        # Pooled QRectF for paint(). One pool entry suffices — the
        # background fill, every per-line block, and (future) the
        # viewport indicator all mutate this same rect via setRect().
        # Phase 2 deliberately does NOT introduce additional pool
        # entries; if Phase 3+ needs concurrent rect bookkeeping
        # (e.g. for the viewport overlay drawn on top of blocks),
        # add a second pool entry rather than re-using this one
        # mid-paint — the pooled-wrapper hygiene project-standards §5
        # mandates for any paint hot path (gotcha #10).
        self._paint_rect = QRectF()

        # Backing fields for the QML-visible properties.
        self._scroll_position = 0.0
        self._buffer_row_count = 0
        # MinimapModel reference — wired via the QML setter pattern
        # (`model: minimapModel` in Main.qml). None is a legitimate
        # state at construction time (before QML
        # assigns `model: minimapModel`). paint() bails out cleanly
        # on None, falling through to the Phase 0 background-only
        # render path.
        self._model: MinimapModel | None = None

    # --- QML-visible properties ----------------------------------------
    #
    # Named-function Property form (not the @Property decorator) for
    # read/write properties: the @Property + @setter pair trips pyright's
    # reportRedeclaration, while the named form reads as a class attribute
    # that pyright handles cleanly.

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
        # ignores subsequent writes from the Python side (cf.
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
    # mutations repaint immediately.

    def _get_model(self) -> MinimapModel | None:
        return self._model

    def _set_model(self, value: MinimapModel | None) -> None:
        if value is self._model:
            return
        if self._model is not None:
            # Tolerate already-disconnected (model handover during
            # teardown) — a defensive disconnect guarded by try/except.
            try:
                self._model.linesChanged.disconnect(self._on_lines_changed)
            except (RuntimeError, TypeError):
                pass
            try:
                self._model.viewportChanged.disconnect(self._on_viewport_changed)
            except (RuntimeError, TypeError):
                pass
            try:
                self._model.diagnosticsChanged.disconnect(self._on_diagnostics_changed)
            except (RuntimeError, TypeError):
                pass
            try:
                self._model.gitChanged.disconnect(self._on_git_changed)
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
            # Phase 4 — gutter signals. Both apply_diagnostics and
            # apply_git run on the GUI thread (via the queued connections
            # in AppController), so direct connections are correct.
            value.diagnosticsChanged.connect(self._on_diagnostics_changed)
            value.gitChanged.connect(self._on_git_changed)
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

    @Slot()
    def _on_diagnostics_changed(self) -> None:
        """Repaint when the model's diagnostic set changes (Phase 4).
        Same single-update strategy as the viewport handler — the
        gutter pass at the end of paint() picks up the new state."""
        self.update()

    @Slot()
    def _on_git_changed(self) -> None:
        """Repaint when git-hunk state changes (Phase 4). Cadence
        constrained at the Lua side to BufWritePost / FocusGained /
        debounced ~2s TextChanged per PRD §8.3 R4.1; the painter
        responds whenever new state arrives."""
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
        # is standard paint hot-path discipline (project-standards §5).
        rect = self._paint_rect
        indent_level = self._model.indent_level
        content_length = self._model.content_length
        fill_rect = painter.fillRect
        indent_step = _INDENT_STEP_PX
        char_width = _CHAR_WIDTH_PX
        # Silhouette inset — Phase 4 reserves `_GUTTER_WIDTH_PX` on the
        # left for the diagnostic + git column. Without this offset the
        # gutter pass would overpaint the silhouette's leftmost pixels
        # for every indent-level-0 row (and the gutter pass runs
        # AFTER the silhouette, so the silhouette would lose visually).
        # By inset-ing here, the two regions never overlap and the
        # painter order becomes irrelevant for correctness.
        gutter_inset = _GUTTER_WIDTH_PX
        for row_idx in range(rows_to_draw):
            # Phase 4.5: blank / pure-whitespace lines have
            # content_length == 0 and render as gaps. Skipping early
            # avoids a 0-width fillRect that costs the Qt round-trip.
            content_len = content_length(row_idx)
            if content_len <= 0:
                continue
            level = indent_level(row_idx)
            x_offset = gutter_inset + level * indent_step
            # Phase 4.5: clip the block width to the actual content
            # length so short lines render short. The min() clamps
            # to remaining-after-indent so very long lines don't
            # overflow the minimap; min-with-view_w-x_offset is the
            # belt for the gutter-inset clamp.
            block_w = content_len * char_width
            max_w = view_w - x_offset
            if block_w > max_w:
                block_w = max_w
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
        #
        # NOTE: Step 6 (gutter) must run regardless of whether viewport
        # data has arrived — diagnostics and git markers are independent
        # of viewport state. The viewport guard is therefore a conditional
        # block rather than an early return.
        viewport_count = self._model.viewport_count()
        if viewport_count > 0:
            viewport_first = self._model.viewport_first()
            # Clamp the visible range to actual buffer extent — a viewport
            # that extends past the buffer's end (because the buffer
            # shrank and the viewport bookkeeping is one tick behind) just
            # truncates rather than running off into negative-height land.
            viewport_end_row = viewport_first + viewport_count
            if viewport_end_row > line_count:
                viewport_end_row = line_count
            if viewport_first < viewport_end_row:
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

        # Step 6 — gutter pass (Phase 4). Paints diagnostic + git-diff
        # markers in the leftmost `_GUTTER_WIDTH_PX` column. The
        # silhouette has already been inset by `gutter_inset` so the
        # two regions don't overlap. Painted AFTER the viewport
        # overlay so a row with a diagnostic INSIDE the viewport
        # still shows the diagnostic dot (the viewport tint composites
        # under the gutter marker, not over it).
        #
        # Order within a single row: git bar drawn first (low z),
        # diagnostic dot drawn over it (high z) — when both are present
        # the diagnostic dominates per PRD §8 design. Rows with neither
        # are skipped entirely (no fill, gutter shows through as the
        # background colour painted in step 1). Runs independently of
        # step 5 — gutter markers render even before the first
        # apply_viewport has fired (terminal-first startup).
        diag_count = self._model.diagnostic_count()
        git_count = self._model.git_count()
        if diag_count == 0 and git_count == 0:
            return
        diagnostic_at = self._model.diagnostic_at
        git_at = self._model.git_at
        diag_idx_map = _DIAGNOSTIC_KEY_TO_COLOR_INDEX
        git_idx_map = _GITDIFF_KEY_TO_COLOR_INDEX
        for row_idx in range(rows_to_draw):
            y = row_idx * row_h
            git_kind = git_at(row_idx)
            if git_kind:
                # _GIT_KINDS filtering in apply_git() guarantees git_kind is always in
                # git_idx_map; the None guard is defense-in-depth only.
                git_color_idx = git_idx_map.get(git_kind)
                if git_color_idx is not None:
                    rect.setRect(0.0, y, _GUTTER_WIDTH_PX, block_h)
                    fill_rect(rect, _gitdiff_colors[git_color_idx])
            diag_sev = diagnostic_at(row_idx)
            if diag_sev:
                # Unknown future severity strings (e.g. a new vim.diagnostic level)
                # pass through apply_diagnostics unchanged; the None guard here keeps
                # the painter crash-free if the palette hasn't been updated yet.
                diag_color_idx = diag_idx_map.get(diag_sev)
                if diag_color_idx is not None:
                    rect.setRect(0.0, y, _GUTTER_WIDTH_PX, block_h)
                    fill_rect(rect, _diagnostic_colors[diag_color_idx])
