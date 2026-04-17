"""NeoVim grid renderer, exposed to QML as `NvimView`.

Subclasses `QQuickPaintedItem` — the standard way to draw custom 2-D
content inside a QML scene. Each cell is painted as a rectangle of
background color plus its character; the cursor is a reverse-video
block on top.

Resizing the QML item recomputes the cell grid size and pushes the new
dimensions to `NvimBackend.resize`, so NeoVim itself reflows to match.
"""

from __future__ import annotations

import logging
import math
import time

from PySide6.QtCore import (
    Property,
    QObject,
    QRectF,
    QSize,
    Qt,
    Signal,
    Slot,
)
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

from .grid import Cell, Grid, HlAttr
from .keys import translate as translate_key
from .nvim_backend import NvimBackend


log = logging.getLogger(__name__)


QML_IMPORT_NAME = "Symmetria.Ide"
QML_IMPORT_MAJOR_VERSION = 1


# Smooth-scroll animation tunables. Defaults adapted from Neovide
# (src/renderer/mod.rs lines 120-122 at commit 04fcd7ac). Hardcoded here
# rather than exposed as user config until the feel is dialed in.
SCROLL_ANIMATION_LENGTH = 0.3       # seconds — spring settle time
# For too-far jumps (gg, G, search) we clamp visual travel to this many
# lines instead of animating the full delta. Neovide default is 1.
# We tried 5 but the scrollback outer slots are blank during the
# flourish (we can't reconstruct intermediate lines without fetching
# from nvim), so a larger value shows a visible blank strip for 300ms —
# worse-looking than a quick 1-line blink. Keep at 1 to match Neovide.
SCROLL_ANIMATION_FAR_LINES = 1
# oversize factor for the cell buffer. For `SCROLLBACK_MULTIPLIER = n`,
# the center slot occupies `grid.rows` rows and there are
# `((n - 1) * grid.rows) // 2` rows of headroom ABOVE and BELOW center
# — that headroom caps how many lines an active scroll animation can
# displace before `paint()` starts reading past the buffer edges and
# produces a blank band at the entering/leaving edge of the viewport.
# mult=2 gave only `rows/2` headroom, which was exactly breached by
# compounding two half-page Ctrl-d presses. mult=3 gives a full
# `rows` of headroom, enough for a full-viewport scroll or two
# half-page scrolls in quick succession before the far-jump clamp
# kicks in.
SCROLLBACK_MULTIPLIER = 3


_qcolor_cache: dict[tuple[int | None, int], QColor] = {}


def _rgb_to_qcolor(value: int | None, fallback: int) -> QColor:
    """Convert a 24-bit RGB integer (NeoVim's rgb_attr format) to QColor.

    Memoized. Every `_paint_row` call allocates two QColors per hl run
    (fg + bg), and a typical frame has dozens of runs per row x 30+
    rows = hundreds of QColors per frame. Each fresh QColor is a
    shiboken-tracked wrapper that counts against `gc.threshold`, so
    throwing them away per paint was pushing the Qt render thread into
    a race with cyclic GC on the pynvim worker thread (SIGSEGV in
    `painter.setPen(...)`). Caching stabilizes the wrapper objects and
    eliminates allocation from the paint hot path. Palettes are tiny
    (one entry per distinct rgb used by the colorscheme), so the cache
    saturates quickly and stays small.
    """
    cached = _qcolor_cache.get((value, fallback))
    if cached is not None:
        return cached
    resolved = fallback if value is None else value
    r = (resolved >> 16) & 0xFF
    g = (resolved >> 8) & 0xFF
    b = resolved & 0xFF
    color = QColor(r, g, b)
    _qcolor_cache[(value, fallback)] = color
    return color


class ScrollAnimation:
    """Critically-damped spring animator for the scroll offset.

    Python port of Neovide's `CriticallyDampedSpringAnimation` (see
    `src/renderer/animation_utils.rs` lines 88-123 at commit 04fcd7ac).

    Unit of `position` is *lines*, not pixels. Target is always 0.0. A
    scroll "displaces" position away from zero via `shift()`; the spring
    pulls it back on each `tick(dt)`.

    Sign convention matches Neovide: positive scroll delta (viewport
    moves down in the buffer / content scrolls up) produces *negative*
    position, so the old content still appears where it was until the
    spring decays toward zero and reveals the new lines below.
    """

    __slots__ = ("position", "velocity", "_far_jump_clear_pending")

    def __init__(self) -> None:
        self.position: float = 0.0
        self.velocity: float = 0.0
        self._far_jump_clear_pending: bool = False

    @property
    def active(self) -> bool:
        return self.position != 0.0 or self.velocity != 0.0

    def shift(self, delta_lines: int, max_delta: int) -> None:
        """Register a scroll delta; displaces `position` away from 0.

        `max_delta` is the one-sided scrollback headroom — the number of
        rows available on one side of the center slot, i.e.
        `_scrollback_center_slot(grid_rows)` = `(scrollback_rows - grid_rows) // 2`.
        Using the full `scrollback_rows - grid_rows` (2x the true headroom)
        caused the blank-band compound-scroll regression (see CLAUDE.md gotcha #11).
        If the requested scroll exceeds max_delta, clamp to a short decorative
        flourish and raise the clear flag so the view blanks the
        scrolled-in region (matches Neovide's far-jump behavior — stops
        `gg`/`G` from streaking across thousands of lines).
        """
        if max_delta <= 0 or delta_lines == 0:
            return
        if abs(delta_lines) > max_delta:
            sign = 1 if delta_lines > 0 else -1
            self.position = -float(sign * SCROLL_ANIMATION_FAR_LINES)
            self.velocity = 0.0
            self._far_jump_clear_pending = True
        else:
            self.position -= float(delta_lines)
            if self.position > max_delta:
                self.position = float(max_delta)
            elif self.position < -max_delta:
                self.position = float(-max_delta)

    def tick(self, dt: float) -> bool:
        """Advance one frame. Returns True iff still animating.

        Integrator: closed-form critically-damped spring step. See
        Neovide's source for the math derivation (cited GDC talk).
        """
        if SCROLL_ANIMATION_LENGTH <= dt:
            # Frame took longer than the whole animation — snap.
            self.position = 0.0
            self.velocity = 0.0
            return False
        if not self.active:
            return False
        omega = 4.0 / SCROLL_ANIMATION_LENGTH
        a = self.position
        b = self.position * omega + self.velocity
        c = math.exp(-omega * dt)
        self.position = (a + b * dt) * c
        self.velocity = c * (-a * omega - b * dt * omega + b)
        if abs(self.position) < 0.01 and abs(self.velocity) < 0.01:
            self.position = 0.0
            self.velocity = 0.0
            return False
        return True

    def reset(self) -> None:
        self.position = 0.0
        self.velocity = 0.0
        self._far_jump_clear_pending = False

    def consume_far_jump_clear(self) -> bool:
        """Return the pending-clear flag and reset it (one-shot)."""
        flag = self._far_jump_clear_pending
        self._far_jump_clear_pending = False
        return flag


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
        # Transparent fill: QQuickPaintedItem otherwise clears its backing
        # with white before every paint(), which would paint over the
        # Window's transparent clear and defeat wallpaper see-through.
        # Combined with skipping per-cell fills when bg == default_bg,
        # this produces the terminal-style "only glyphs + explicit-bg
        # cells paint" look that matches Ghostty on Hyprland.
        self.setFillColor(QColor(0, 0, 0, 0))
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

        # --- Smooth-scroll animation state (all GUI-thread-only) ---------
        self._scroll_anim = ScrollAnimation()
        # Oversized cell buffer (2× viewport rows). Populated on demand
        # when an animation starts; dormant otherwise to avoid per-flush
        # copy cost while idle.
        self._scrollback: list[list[Cell]] = []
        self._scrollback_rows = 0
        # Multiple grid_scroll events may arrive in one redraw batch
        # (e.g. `zz` center-on-cursor). Accumulate until flush fires.
        self._pending_scroll_delta = 0
        # Wall-clock timestamp of the last frameSwapped tick, for real
        # dt measurement. None when the driver is disconnected.
        self._last_frame_t: float | None = None
        self._driver_connected = False

        # Frame driver runs off the QQuickWindow's frameSwapped signal.
        # The item isn't attached to a window in __init__ — connect when
        # the window becomes known via the built-in windowChanged signal.
        self.windowChanged.connect(self._on_window_changed)

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
            try:
                self._backend.viewport_scrolled.disconnect(self._on_viewport_scrolled)
            except (RuntimeError, TypeError):
                pass
        self._backend = value
        if value is not None:
            value.redraw_flushed.connect(self._on_redraw_flushed)
            # `viewport_scrolled` is fed by the WinScrolled autocmd in
            # runtime/init.lua — fires for any topline change whether
            # nvim used grid_scroll or a full grid_line repaint. Queued
            # connection across the worker/GUI boundary (auto-selected
            # by Qt when sender and receiver live in different threads).
            value.viewport_scrolled.connect(self._on_viewport_scrolled)
            self._push_current_size()
        self.backendChanged.emit()

    @Property(float, notify=cellMetricsChanged)
    def cellWidth(self) -> float:
        return self._cell_w

    @Property(float, notify=cellMetricsChanged)
    def cellHeight(self) -> float:
        return self._cell_h

    # --- Backend → view ------------------------------------------------

    @Slot(int)
    def _on_viewport_scrolled(self, delta: int) -> None:
        """Record a viewport scroll delta. Applied on next flush.

        Delta is the line-count change in `line('w0')` (topline) from
        the WinScrolled autocmd. Positive = content scrolled up
        (Ctrl-d / <C-f> / J at end-of-viewport). Negative = content
        scrolled down (Ctrl-u / <C-b>).

        We don't apply here — the WinScrolled notification arrives
        before the flush for the same event, so we accumulate and let
        _on_redraw_flushed rotate the scrollback and displace the
        spring once grid state has stabilized.
        """
        self._pending_scroll_delta += delta

    @Slot()
    def _on_redraw_flushed(self) -> None:
        """Apply any pending scroll delta and repaint.

        Ordering:
          1. If we have a pending delta, allocate/resize scrollback,
             rotate it by the delta, and displace the spring.
          2. Copy the live grid rows into the center slot of scrollback
             so scrolled-in content is present at its destination.
          3. Trigger a paint.
          4. If the spring is active, make sure the frame driver is
             connected so the animation advances on every frame.

        If no delta and no active animation: we still paint, but skip
        the scrollback copy (fast path — idle typing stays cheap).

        Exceptions here are caught and logged rather than bubbling —
        Qt would otherwise swallow them on its slot-dispatch path,
        making any bug invisible. We always call update() in the
        finally block so a single-frame glitch doesn't freeze the UI.
        """
        try:
            self._maybe_apply_scroll_delta()
        except Exception:  # noqa: BLE001
            log.exception("scroll-delta application failed; animation reset")
            self._reset_animation_state_after_error()
        self.update()

    def _maybe_apply_scroll_delta(self) -> None:
        """Meat of _on_redraw_flushed — extracted so try/except can wrap it.

        **Always snapshots the current viewport into the scrollback
        center slot**, even when no scroll is pending. This is the
        critical invariant: when a scroll *does* arrive, the rotation
        needs the pre-scroll viewport to already be present in the
        center slot so it can carry those rows into the outer slots
        where they'll animate out of frame. Without this, the first
        scroll starts with a blank scrollback and the outgoing rows
        render as background gaps while they slide off.

        Snapshot cost: one list-per-row shallow copy (~grid.rows ×
        grid.cols cell refs). For a 120×30 grid that's ~3600 refs per
        flush, measured at under 0.1ms. Cheap relative to the paint
        itself.
        """
        if self._backend is None:
            return
        grid = self._backend.grid
        if grid.rows <= 0 or grid.cols <= 0:
            return
        self._ensure_scrollback_sized(grid.rows, grid.cols)
        if self._pending_scroll_delta != 0:
            delta = self._pending_scroll_delta
            # Rotate the scrollback so the pre-scroll viewport (currently
            # in the center slot from the previous flush's snapshot)
            # moves to the outer slots where the animation will render
            # it sliding out. After rotation, the center slot still
            # holds stale content — the snapshot below overwrites it
            # with the post-scroll viewport for the animation target.
            self._rotate_scrollback(delta)
            # max_delta is the animation's position cap in LINES. We can
            # only paint with a displaced position as large as the number
            # of rows of scrollback *on one side* of the center slot — if
            # `|position|` exceeds `slot_start`, the paint loop reads
            # `src = buf_start + dr` at indices past the buffer ends, the
            # range guard skips those iterations, and the viewport shows
            # a blank strip of `default_bg` at the leading edge. Using
            # `scrollback_rows - grid.rows` (2x slot_start) allowed twice
            # the displacement we can actually render — compound scrolls
            # landed in that invalid range and showed a visible gap.
            max_delta = self._scrollback_center_slot(grid.rows)
            self._scroll_anim.shift(delta, max_delta)
            self._pending_scroll_delta = 0
            if self._scroll_anim.consume_far_jump_clear():
                self._clear_scrollback_excluding_viewport(grid.rows)
        # Always snapshot — whether or not a scroll happened this flush.
        # Overwrite the pre-allocated destination row IN PLACE rather
        # than allocating a new list per row. This keeps allocation
        # pressure low (reusing lists instead of churning them), which
        # matters a lot under Python 3.14's GC interacting with
        # pynvim's greenlet-based RPC dispatch on the worker thread.
        # GIL guarantees list item assignment is atomic, so this is
        # safe vs. the worker thread's concurrent redraw batch.
        slot_start = self._scrollback_center_slot(grid.rows)
        for r in range(grid.rows):
            try:
                src_row = grid.cells[r]
                dst_row = self._scrollback[slot_start + r]
            except IndexError:
                # Race: worker thread reassigned grid.cells mid-snapshot
                # (resize). Bail — the next flush will resync.
                return
            # Length mismatch tolerated: scrollback rows are pre-sized
            # to whatever cols the view was sized at; grid.cols may
            # differ transiently across a resize. Cap at the smaller
            # end and the paint path handles short rows.
            limit = min(len(src_row), len(dst_row))
            for c in range(limit):
                dst_row[c] = src_row[c]
        if self._scroll_anim.active:
            self._maybe_start_frame_driver()

    def _reset_animation_state_after_error(self) -> None:
        """Snap to a clean state after an exception in the scroll path.

        We'd rather lose the current animation than leave the UI in a
        partially-updated state that keeps crashing on every repaint.
        """
        self._scroll_anim.reset()
        self._pending_scroll_delta = 0
        self._scrollback = []
        self._scrollback_rows = 0
        self._stop_frame_driver()

    # --- Scrollback management ----------------------------------------

    def _scrollback_center_slot(self, grid_rows: int) -> int:
        """First index of the center (viewport) slot within the scrollback buffer.

        The scrollback buffer is SCROLLBACK_MULTIPLIER × grid_rows rows.
        The live viewport occupies the center `grid_rows` rows so that
        equal overrun space is available above and below during animation.
        """
        return (self._scrollback_rows - grid_rows) // 2

    def _ensure_scrollback_sized(self, grid_rows: int, grid_cols: int) -> None:
        """Allocate scrollback to 2× grid_rows, seeded with blank cells.

        Only reallocates when the row count actually changes (cheap
        no-op on steady-state redraws). Column count is reflected in
        the per-row list length for any newly allocated rows; rows that
        already exist keep whatever content they had — the next
        snapshot from the viewport overwrites the center slot anyway.
        """
        target = SCROLLBACK_MULTIPLIER * grid_rows
        if target == self._scrollback_rows and self._scrollback:
            # Same row count; snapshot will refresh center slot below.
            # Column mismatches are tolerated — paint reads up to
            # grid.cols per row and the live snapshot overwrites.
            return
        self._scrollback = [
            [Cell() for _ in range(grid_cols)] for _ in range(target)
        ]
        self._scrollback_rows = target

    def _rotate_scrollback(self, delta: int) -> None:
        """Rotate the scrollback buffer in place by `delta` rows.

        Positive `delta` (scroll down / content up) shifts rows toward
        lower indices — matches the sign convention in Neovide's
        `RingBuffer::rotate` (`src/renderer/rendered_window.rs`).
        """
        n = self._scrollback_rows
        if n == 0:
            return
        d = delta % n
        if d == 0:
            return
        # In-place slice assignment reuses the same list object, avoiding
        # a ref-cycle from replacing _scrollback entirely (keeps GC pressure
        # low on the pynvim worker thread path).
        self._scrollback[:] = self._scrollback[d:] + self._scrollback[:d]

    def _clear_scrollback_excluding_viewport(self, grid_rows: int) -> None:
        """Zero out scrollback rows outside the center viewport slot.

        Called when a far-jump clamp fires (see ScrollAnimation.shift):
        the buffer has been rotated by a huge delta but we clamp the
        visible travel to SCROLL_ANIMATION_FAR_LINES, so the scrolled-in
        region contains stale content from thousands of lines ago.
        Wipe it so the short flourish doesn't reveal garbage.
        """
        slot_start = self._scrollback_center_slot(grid_rows)
        slot_end = slot_start + grid_rows
        for i in range(self._scrollback_rows):
            if slot_start <= i < slot_end:
                continue
            # Reset each row in-place using its own length — avoids
            # deriving cols from scrollback[0] (stale after a resize).
            row = self._scrollback[i]
            row[:] = [Cell() for _ in range(len(row))]

    # --- Frame driver -------------------------------------------------

    def _maybe_start_frame_driver(self) -> None:
        """Connect to frameSwapped if an animation is active.

        Idempotent and defensive: if the item isn't attached to a
        QQuickWindow yet, defer — the windowChanged connection in
        __init__ will retry once the window becomes known.
        """
        if self._driver_connected or not self._scroll_anim.active:
            return
        window = self.window()
        if window is None:
            return
        # QQuickWindow.frameSwapped is emitted from the render thread,
        # not the GUI thread. Force a queued connection so our handler
        # runs on the main thread where it can safely call self.update()
        # and touch shared Python state. Without this, AutoConnection
        # MIGHT queue (based on receiver's thread affinity) but it's
        # implementation-detail-fragile — explicit queued is safer.
        window.frameSwapped.connect(
            self._on_frame_swapped,
            Qt.ConnectionType.QueuedConnection,
        )
        self._last_frame_t = time.perf_counter()
        self._driver_connected = True

    def _stop_frame_driver(self) -> None:
        if not self._driver_connected:
            return
        window = self.window()
        if window is not None:
            try:
                window.frameSwapped.disconnect(self._on_frame_swapped)
            except (RuntimeError, TypeError):
                pass
        self._driver_connected = False
        self._last_frame_t = None

    @Slot()
    def _on_frame_swapped(self) -> None:
        """One animation step, synced to the compositor's vsync.

        `frameSwapped` fires *after* a frame is presented, so dt here
        reflects real elapsed time between presentations. Required for
        framerate-independent spring integration.
        """
        try:
            now = time.perf_counter()
            dt = now - self._last_frame_t if self._last_frame_t is not None else 0.0
            self._last_frame_t = now
            # Negative or zero dt (clock skew, first tick) — just repaint.
            dt = max(0.0, dt)
            still = self._scroll_anim.tick(dt)
            self.update()
            if not still:
                self._stop_frame_driver()
        except Exception:  # noqa: BLE001
            log.exception("frame-swap tick failed; stopping animation")
            self._reset_animation_state_after_error()

    @Slot()
    def _on_window_changed(self) -> None:
        """Handle the QML item being attached to or detached from a window.

        If we had a pending animation start waiting on a window, wire
        the driver now. If we're being detached, drop the connection so
        we don't hold a reference to a dead window.
        """
        try:
            self._stop_frame_driver()
            if self._scroll_anim.active:
                self._maybe_start_frame_driver()
        except Exception:  # noqa: BLE001
            log.exception("windowChanged handler failed; resetting animation")
            self._reset_animation_state_after_error()

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
            # Resize invalidates the scrollback buffer (row count
            # changes) and any in-flight scroll would now be measured
            # against the wrong geometry. Snap to the new state — a
            # brief snap is less jarring than a mid-resize wobble.
            self._scroll_anim.reset()
            self._pending_scroll_delta = 0
            self._scrollback = []
            self._scrollback_rows = 0
            self._stop_frame_driver()

    # --- Painting ------------------------------------------------------

    def paint(self, painter: QPainter) -> None:
        if self._backend is None:
            # No fill: leave the backing store as the transparent clear
            # from setFillColor(). The compositor shows the wallpaper.
            return
        grid: Grid = self._backend.grid
        painter.setFont(self._font)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        default_fg = grid.default_fg
        default_bg = grid.default_bg
        # Ghostty-parity ambient dim: black at 60% alpha over the
        # wallpaper, matching `~/ghostty/config` (`background = #000000`,
        # `background-opacity = 0.6`). Improves text contrast against
        # bright wallpapers without hiding them. Cells with explicit,
        # non-default backgrounds (line numbers, diff, cursorline,
        # signs, visual-selection, reversed highlights) still paint
        # opaquely on top of this tint — see `_paint_row`.
        painter.fillRect(self.boundingRect(), QColor(0, 0, 0, 153))

        cw = self._cell_w
        ch = self._cell_h

        # Bootstrap-only fast path: scrollback not yet allocated (first
        # flush hasn't landed). Read grid.cells directly this one time.
        if self._scrollback_rows <= 0:
            self._paint_rows_from_grid(painter, grid, cw, ch, default_fg, default_bg)
            self._paint_cursor(painter, grid, cw, ch)
            return

        # All other paints go through the scrollback. Even when not
        # animating (pos=0), this reads from the scrollback center slot
        # which was snapshotted at the last flush. Reading grid.cells
        # here would expose a race: the worker thread mutates grid.cells
        # between viewport_scrolled and redraw_flushed, and a paint at
        # that moment would render the already-mutated NEW viewport
        # without any animation, producing a single-frame snap that
        # looks like "content disappears before leaving the viewport."
        painter.save()
        # Clip to EXACT grid dimensions, not boundingRect(). QML float
        # sizing can make the widget's actual painted area marginally
        # larger than `grid.rows * ch`, and boundingRect() reflects that
        # — enough slack for a full row of stale scrollback content to
        # leak through at the bottom edge. Tight clipping is defense in
        # depth on top of the row-iteration guard in
        # `_paint_rows_from_scrollback`.
        painter.setClipRect(QRectF(0.0, 0.0, grid.cols * cw, grid.rows * ch))
        try:
            pos = self._scroll_anim.position
            # Geometry: pos=-2.7 lines means the viewport is displaced
            # 2.7 lines upward (old content above, new content entering
            # from below). Decompose into an integer row offset + a
            # sub-cell pixel residual so each paint lands on a row
            # boundary plus a smooth pixel fraction.
            #
            #   floor(-2.7) = -3  → read buf rows starting 3 above center
            #   residual = (-3 - (-2.7)) * ch = -0.3*ch
            #   row dr=3 (center) renders at 3*ch + (-0.3*ch) = 2.7*ch  ✓
            scroll_offset_lines = math.floor(pos)
            pixel_residual_y = (scroll_offset_lines - pos) * ch  # in (-ch, 0]
            slot_start = self._scrollback_center_slot(grid.rows)
            buf_start = slot_start + scroll_offset_lines
            self._paint_rows_from_scrollback(
                painter,
                grid,
                cw,
                ch,
                default_fg,
                default_bg,
                buf_start,
                pixel_residual_y,
            )
        finally:
            painter.restore()

        # Cursor pinned to cur_row * ch regardless of animation state.
        # Read glyph from grid.cells (truth source).
        self._paint_cursor(painter, grid, cw, ch)

    def _paint_rows_from_grid(
        self,
        painter: QPainter,
        grid: Grid,
        cw: float,
        ch: float,
        default_fg: int,
        default_bg: int,
    ) -> None:
        """Fast-path paint: read directly from grid.cells."""
        for r in range(grid.rows):
            y = r * ch
            self._paint_row(
                painter, grid.cells[r], grid.cols, grid.hl_attrs,
                default_fg, default_bg, y, cw, ch,
            )

    def _paint_rows_from_scrollback(
        self,
        painter: QPainter,
        grid: Grid,
        cw: float,
        ch: float,
        default_fg: int,
        default_bg: int,
        buf_start: int,
        pixel_residual_y: float,
    ) -> None:
        """Animated paint: iterate the scrollback slice with pixel offset.

        Row iteration range:
        - `dr=-1` would draw at `y = -ch + residual` which is always
          above the viewport top (residual is in (-ch, 0]), so it is
          never visible and is never iterated.
        - `dr=grid.rows` is only visible when `residual < 0` (sub-cell
          animation in flight) — it then peeks in from the bottom
          with `-residual` pixels of height. At settled state
          (residual == 0) the row would be at `y = grid.rows * ch`
          exactly at the viewport bottom; if the widget's actual
          painted area overshoots that by even one pixel (which has
          happened due to QML float sizing), a full row of STALE
          content from earlier scroll-rotations leaks through and
          shows up as a wrong-numbered line at the bottom of the
          viewport. Gating on residual eliminates that leak.
        """
        cols = grid.cols
        last_dr = grid.rows if pixel_residual_y < 0.0 else grid.rows - 1
        for dr in range(0, last_dr + 1):
            src = buf_start + dr
            if not (0 <= src < self._scrollback_rows):
                continue
            y = dr * ch + pixel_residual_y
            row_cells = self._scrollback[src]
            # Scrollback rows may have been allocated with a stale col
            # count if the grid resized; cap at whichever is smaller.
            row_cols = min(cols, len(row_cells))
            self._paint_row(
                painter, row_cells, row_cols, grid.hl_attrs,
                default_fg, default_bg, y, cw, ch,
            )

    def _paint_row(
        self,
        painter: QPainter,
        row_cells: list[Cell],
        cols: int,
        hl_attrs: dict,
        default_fg: int,
        default_bg: int,
        y: float,
        cw: float,
        ch: float,
    ) -> None:
        """Paint one row with hl_id run-coalescing.

        Shared by the fast path (row from grid.cells) and the animated
        path (row from scrollback). Run-coalescing keeps large
        same-attribute regions to a single fillRect + drawText.
        """
        c = 0
        while c < cols:
            cell = row_cells[c]
            attr = hl_attrs.get(cell.hl_id, HlAttr())
            fg_val = attr.foreground if attr.foreground is not None else default_fg
            bg_val = attr.background if attr.background is not None else default_bg
            if attr.reverse:
                fg_val, bg_val = bg_val, fg_val

            run_start = c
            run_chars: list[str] = [cell.char]
            c += 1
            while c < cols:
                nxt = row_cells[c]
                if nxt.hl_id != cell.hl_id:
                    break
                run_chars.append(nxt.char)
                c += 1

            rect = QRectF(run_start * cw, y, (c - run_start) * cw, ch)
            # Skip the bg fill when the run's effective background equals
            # the colorscheme's default — the paint() ambient tint
            # (Ghostty-parity black @ 60%) already covers these cells,
            # and painting default_bg opaquely here would black out the
            # wallpaper. Runs with explicit bg (signs column, diff,
            # cursorline, visual selection, reversed highlights) still
            # paint. `bg_val` is post-reverse so a reversed cell with
            # original bg == default_bg now has bg_val == default_fg and
            # correctly paints.
            if bg_val != default_bg:
                painter.fillRect(rect, _rgb_to_qcolor(bg_val, default_bg))

            if attr.bold or attr.italic:
                painter.setFont(
                    self._font_variants.get((attr.bold, attr.italic), self._font)
                )
            else:
                painter.setFont(self._font)

            painter.setPen(_rgb_to_qcolor(fg_val, default_fg))
            painter.drawText(
                rect,
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                "".join(run_chars),
            )

    def _paint_cursor(
        self,
        painter: QPainter,
        grid: Grid,
        cw: float,
        ch: float,
    ) -> None:
        """Reverse-block cursor pinned to its post-scroll viewport row.

        Cursor is drawn at `cur_row * ch`, pinned to its post-scroll
        viewport row. We do NOT subtract the live scroll animation
        position — see CLAUDE.md gotcha #11 ("Cursor is pinned to cur_row * ch")
        for the full rationale. Short version: for Ctrl-d/Ctrl-u/gg/G,
        nvim moves the cursor and topline by the same delta, so the
        cursor's visual row is unchanged and applying the offset draws it
        at the wrong row for the entire 300 ms animation.
        """
        cur_row = grid.cursor_row
        cur_col = grid.cursor_col
        if not (0 <= cur_row < grid.rows and 0 <= cur_col < grid.cols):
            return
        cursor_cell = grid.cells[cur_row][cur_col]
        cursor_rect = QRectF(
            cur_col * cw,
            cur_row * ch,
            cw,
            ch,
        )
        painter.fillRect(cursor_rect, _rgb_to_qcolor(grid.default_fg, 0xD0D0D0))
        painter.setPen(_rgb_to_qcolor(grid.default_bg, 0x1E1E1E))
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
