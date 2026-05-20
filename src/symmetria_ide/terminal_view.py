"""Terminal renderer, exposed to QML as `TerminalView`.

Subclasses `QQuickPaintedItem` — same shape as `NvimView` but for the
pyte cell-grid instead of the nvim grid. Paint loop reads
`backend._screen.buffer` and produces one `fillRect` + `drawText` per
coalesced highlight run. Key events translate to terminal escape
sequences via `terminal_keys.translate` and forward to
`backend.write()`. Geometry changes compute cols/rows and call
`backend.resize()`.

Disciplines enforced (CLAUDE.md gotchas #10, #11, #23):

- **gotcha #10 — paint hot path allocates no PySide wrappers.** All
  QColors come from `_resolve_color`'s memoized cache (no fresh
  `QColor(...)` inside `paint`). Pooled `QRectF` (`_run_rect`,
  `_clip_rect`, `_cursor_rect`) are mutated via `setRect()` —
  paint never instantiates a fresh QRectF. Bold/italic QFont
  variants pre-built in `__init__` and reused. `_run_chars` list
  is cleared + reused per row.

- **gotcha #11 — clip to exact grid dimensions, NOT `boundingRect()`.**
  QML's float-sized bounds can leak past the cell grid; clipping
  to `(cols*cw, rows*ch)` exactly is defense-in-depth.

- **gotcha #23 — font cascade via `QFont.setFamilies([primary,
  *fallbacks])`.** Reused from `NvimView._default_font()` so editor
  and terminal share the same primary family + Nerd Font / emoji
  fallback chain, and cell metrics line up exactly.

ANSI palette: the 16-color table here MUST stay in sync with
`Theme.color.terminal.color0..color15` in `qml/design/Theme.qml`.
Until we wire the Theme palette through Python via context property
(a v2 refactor — would mean an extra constructor arg or a setter
slot), drift is prevented by `test_terminal_view.py::
test_ansi_palette_matches_theme_qml` which reads Theme.qml and
asserts the bright-variant hex values match.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from PySide6.QtCore import Property, QObject, QRectF, Qt, Signal, Slot
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QGuiApplication,
    QKeyEvent,
    QPainter,
)
from PySide6.QtQml import QmlElement
from PySide6.QtQuick import QQuickPaintedItem

from .nvim_view import NvimView
from .terminal_backend import TerminalBackend
from .terminal_keys import translate as translate_key


log = logging.getLogger(__name__)


QML_IMPORT_NAME = "Symmetria.Ide"
QML_IMPORT_MAJOR_VERSION = 1


# 16-slot ANSI palette as 24-bit RGB integers, mirroring the explicit
# hex values in `qml/design/Theme.qml`'s `Theme.color.terminal.color0..15`.
# Slots that ALIAS `mode.*` / `text.*` in the QML side are duplicated here
# as concrete hex — the QML aliasing is a design-token relationship, but
# Python doesn't have access to the singleton at module load time.
#
# Drift prevention: `test_terminal_view.py::test_ansi_palette_matches_
# theme_qml` reads Theme.qml and cross-checks the explicit hex tokens
# (slots 9-14 + slot 15 alias) against this tuple. If a palette nudge
# in Theme.qml isn't mirrored here, the test fails.
_ANSI_PALETTE: tuple[int, ...] = (
    0x131313,  # 0  black           (Theme.mode.badgeLabel)
    0xD2602D,  # 1  red             (Theme.mode.replace)
    0x62BA46,  # 2  green           (Theme.mode.insert)
    0xC28B12,  # 3  yellow          (Theme.mode.normal)
    0x6D94E9,  # 4  blue            (Theme.mode.command)
    0xD86DE9,  # 5  magenta         (Theme.mode.visual)
    0x5BDFD8,  # 6  cyan            (Theme.mode.terminal)
    0xB0B0B0,  # 7  white           (Theme.text.normal)
    0x7A7A7A,  # 8  bright black    (Theme.text.dim)
    0xE58B5C,  # 9  bright red
    0x86D666,  # 10 bright green
    0xE5B142,  # 11 bright yellow
    0x9CB6F0,  # 12 bright blue
    0xE69BF0,  # 13 bright magenta
    0x8AE9E4,  # 14 bright cyan
    0xF5F5F5,  # 15 bright white    (Theme.text.selected)
)

# Default foreground = Theme.text.normal. Default background is
# transparent (alpha=0, handled via setFillColor + skipping default-bg
# fills — same wallpaper-blend pattern NvimView uses).
_DEFAULT_FG_RGB = 0xB0B0B0

# Cursor block fill = pure-ish white (same hex as ANSI slot 15 /
# Theme.text.selected). The warm-amber accent (`0xE8AB6F`) was tried
# first to match the editor's cursor color, but on the dim
# wallpaper-blend background it read as a yellow-orange tint rather
# than a cursor — the eye lost track of it against amber-toned cells.
# White contrasts cleanly with both dark and light foregrounds and
# matches user expectation for terminal cursors (kitty/alacritty/
# ghostty all default to a near-white cursor).
_CURSOR_RGB = 0xF5F5F5

# Inner padding around the cell grid, in pixels. Mirrors Ghostty's
# `window-padding-x = 20` / `window-padding-y = 20` so the IDE's
# terminal pane reads with the same "framed" composition as the
# user's standalone Ghostty windows. The ambient tint paints the
# full boundingRect (including this padding ring) so the dim
# wallpaper-blend extends right to the pane edges; only the cell
# grid + cursor are inset by this amount.
#
# Defined here (not in `qml/design/Theme.qml`) because paint() reads
# it directly; piping a Theme token through a QML context property
# is a v2 refactor — same dual-source pattern the ANSI palette uses.
_PADDING_PX = 20

# pyte's color-name vocabulary. pyte uses 'brown' for slot 3 (the
# historical name on real DEC terminals); 'yellow' is accepted as an
# alias. Bright variants are 'brightblack' / 'brightred' / etc.
# Unknown names + unrecognised hex strings fall back to default fg.
_COLOR_NAME_TO_INDEX: dict[str, int] = {
    "black": 0,
    "red": 1,
    "green": 2,
    "brown": 3,
    "yellow": 3,
    "blue": 4,
    "magenta": 5,
    "cyan": 6,
    "white": 7,
    "brightblack": 8,
    "brightred": 9,
    "brightgreen": 10,
    "brightbrown": 11,
    "brightyellow": 11,
    "brightblue": 12,
    "brightmagenta": 13,
    "brightcyan": 14,
    "brightwhite": 15,
}


# Memoized QColor cache. Keyed by RGB integer (not by name) so identical
# colors arriving via different routes (e.g. "red" vs explicit 0xD2602D
# hex from a 256-color cube hit) share the same wrapper. Same gotcha #10
# rationale as `NvimView._qcolor_cache` — palettes are small, the cache
# saturates quickly and stays small.
_qcolor_cache: dict[int, QColor] = {}


def _resolve_color(name: Any, *, is_bg: bool) -> QColor | None:
    """Resolve a pyte fg/bg value to a memoized QColor.

    Returns None for 'default' bg (caller skips the fill, preserving
    the wallpaper-blend invariant).

    pyte's fg/bg can be:
      - 'default' (literal string) — default fg/bg
      - one of the named colors (`_COLOR_NAME_TO_INDEX`)
      - a 6-character hex string (256-color cube result or true-color)
      - an integer (extremely uncommon, but handled defensively)

    Anything else falls back to the default fg color (white-ish neutral)
    so a paint never crashes on a surprise value from a future pyte bump.
    """
    if name == "default":
        if is_bg:
            return None
        rgb = _DEFAULT_FG_RGB
    elif isinstance(name, int):
        rgb = name
    elif isinstance(name, str):
        lowered = name.lower()
        idx = _COLOR_NAME_TO_INDEX.get(lowered)
        if idx is not None:
            rgb = _ANSI_PALETTE[idx]
        elif len(name) == 6:
            try:
                rgb = int(name, 16)
            except ValueError:
                rgb = _DEFAULT_FG_RGB
        else:
            rgb = _DEFAULT_FG_RGB
    else:
        rgb = _DEFAULT_FG_RGB

    cached = _qcolor_cache.get(rgb)
    if cached is not None:
        return cached
    color = QColor((rgb >> 16) & 0xFF, (rgb >> 8) & 0xFF, rgb & 0xFF)
    _qcolor_cache[rgb] = color
    return color


# Pre-computed text alignment flag — same micro-optimization as NvimView
# (avoids re-resolving two enum attrs per run inside the paint loop).
_TEXT_ALIGN_FLAGS = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)


@QmlElement
class TerminalView(QQuickPaintedItem):
    """Paints the backend's pyte screen into the QML scene.

    The QML side sets `backend` once (from `Main.qml`, wired in PR 5);
    the view connects to `backend.screen_dirty` and
    `backend.screen_resized` and calls `update()` to request a repaint.
    Key events translate to terminal escape sequences and forward via
    `backend.write()`. Geometry changes compute cols/rows from cell
    metrics and call `backend.resize()`.
    """

    backendChanged = Signal()
    cellMetricsChanged = Signal()

    def __init__(self, parent=None) -> None:  # noqa: ANN001
        # Untyped `parent` matches NvimView.__init__ — PySide6 stubs
        # are stricter (QQuickItem|None) than reality. Both surfaces
        # accept any QObject in practice; keeping untyped sidesteps
        # the stub mismatch without breaking Qt's metaobject system
        # (CLAUDE.md gotcha #7).
        super().__init__(parent)

        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        # Transparent fill: wallpaper-blend per Q2-d topology decision.
        # Same setFillColor(0,0,0,0) NvimView uses — both panes share
        # the same visual surface contract.
        self.setFillColor(QColor(0, 0, 0, 0))

        # Item flags — copied from NvimView so focus + IME behavior is
        # identical between the two central-slot surfaces.
        self.setFlag(QQuickPaintedItem.Flag.ItemHasContents, True)
        self.setFlag(QQuickPaintedItem.Flag.ItemIsFocusScope, True)
        self.setFlag(QQuickPaintedItem.Flag.ItemAcceptsInputMethod, True)
        self.setActiveFocusOnTab(True)

        # Font cascade — reuse NvimView._default_font() so cell metrics
        # (cw, ch) match between the editor and terminal panes exactly.
        # `font.families` cascade for Nerd Font + emoji is inherited
        # from there. gotcha #23 — the font must be a single resolved
        # QFont with setFamilies(), NOT a comma-separated family string.
        self._font = NvimView._default_font()
        self._metrics = QFontMetricsF(self._font)
        self._cell_w = max(1.0, self._metrics.horizontalAdvance("M"))
        self._cell_h = max(1.0, self._metrics.height())

        # Bold/italic font variants pre-built so the paint loop doesn't
        # allocate a transient QFont per run. Same pool pattern as
        # NvimView._font_variants.
        self._font_variants: dict[tuple[bool, bool], QFont] = {}
        for bold in (False, True):
            for italic in (False, True):
                if bold or italic:
                    variant = QFont(self._font)
                    variant.setBold(bold)
                    variant.setItalic(italic)
                    self._font_variants[(bold, italic)] = variant

        # Pooled QRectF objects — paint never instantiates a fresh one
        # (gotcha #10). _run_rect is mutated per coalesced run, _clip_rect
        # is set per frame to the grid-exact bounds (gotcha #11),
        # _cursor_rect is sized once per frame for the cursor block.
        self._run_rect = QRectF()
        self._clip_rect = QRectF()
        self._cursor_rect = QRectF()

        # Reusable char buffer for run-coalescing. Same pool pattern as
        # NvimView._run_chars — clear + reuse per row instead of fresh
        # list per run.
        self._run_chars: list[str] = []

        # Ambient tint — Ghostty-parity dim over wallpaper. Pre-allocated
        # so paint() doesn't construct it per frame (gotcha #10).
        self._ambient_tint_color = QColor(0, 0, 0, 153)

        self._backend: TerminalBackend | None = None
        self._cols = 0
        self._rows = 0

    # --- QML-visible properties ----------------------------------------

    # Named-functions Property form — same pyright rationale as
    # NvimView.backend (the @Property / @setter decorator pair trips
    # reportRedeclaration; the named form reads as a class attribute).

    def _get_backend(self) -> TerminalBackend | None:
        return self._backend

    def _set_backend(self, value: TerminalBackend | None) -> None:
        if value is self._backend:
            return
        if self._backend is not None:
            try:
                self._backend.screen_dirty.disconnect(self._on_screen_dirty)
            except (RuntimeError, TypeError):
                pass
            try:
                self._backend.screen_resized.disconnect(self._on_screen_resized)
            except (RuntimeError, TypeError):
                pass
        self._backend = value
        if value is not None:
            # Qt.QueuedConnection — reader thread emits screen_dirty
            # cross-thread to the GUI thread (§4 P2). Same explicit
            # connect-type comment pattern AppController uses for the
            # session host signal hop.
            value.screen_dirty.connect(
                self._on_screen_dirty, Qt.ConnectionType.QueuedConnection
            )
            value.screen_resized.connect(
                self._on_screen_resized, Qt.ConnectionType.QueuedConnection
            )
            self._push_current_size()
        self.backendChanged.emit()

    backend = Property(QObject, _get_backend, _set_backend, notify=backendChanged)

    @Property(float, notify=cellMetricsChanged)
    def cellWidth(self) -> float:
        return self._cell_w

    @Property(float, notify=cellMetricsChanged)
    def cellHeight(self) -> float:
        return self._cell_h

    # --- Backend → view ------------------------------------------------

    @Slot(frozenset)
    def _on_screen_dirty(self, dirty: frozenset) -> None:
        """Trigger repaint when the reader emits new content.

        The payload (set of dirty row indices) is advisory only — v1
        repaints in full via `update()`. A v2 partial-repaint via
        `update(QRect)` would convert dirty row indices to per-row
        rects, but the bookkeeping cost only pays off when dirty rows
        are a small fraction of the total (typical shell output churns
        many rows on scroll).
        """
        del dirty
        self.update()

    @Slot(int, int)
    def _on_screen_resized(self, cols: int, rows: int) -> None:
        """Repaint after the backend's pyte screen reflows.

        The dimensions are advisory at this layer — paint() reads
        screen.columns/lines directly from the live pyte instance.
        """
        del cols, rows
        self.update()

    # --- Geometry ------------------------------------------------------

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
        # Subtract the inner padding ring (both sides) BEFORE floor-div,
        # so pyte's column/row count reflects only the cell-paintable
        # region — otherwise pyte would think the screen is wider/taller
        # than what we actually paint and the right/bottom rows would
        # silently fall outside the clip.
        inner_w = max(0.0, w - 2 * _PADDING_PX)
        inner_h = max(0.0, h - 2 * _PADDING_PX)
        # Floor-div the pixel dims by cell dims, clamp to sane minimums
        # (20×5) so a too-small QML geometry doesn't push pyte into a
        # degenerate state. Same clamps as NvimView._push_current_size.
        cols = max(20, int(inner_w // self._cell_w))
        rows = max(5, int(inner_h // self._cell_h))
        if (cols, rows) != (self._cols, self._rows):
            self._cols = cols
            self._rows = rows
            self._backend.resize(cols, rows)

    # --- Painting ------------------------------------------------------

    def paint(self, painter: QPainter) -> None:
        if self._backend is None or self._backend._screen is None:
            # No fill: leave the backing store as the transparent clear
            # from setFillColor(). The compositor shows the wallpaper.
            return

        screen = self._backend._screen
        cw = self._cell_w
        ch = self._cell_h
        cols = screen.columns
        rows = screen.lines

        painter.setFont(self._font)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        # Ambient tint over the wallpaper — same Ghostty-parity dim as
        # NvimView so both surfaces share the visual base layer.
        # Painted in untranslated coords so the dim extends through the
        # padding ring; only the cell grid + cursor are inset.
        painter.fillRect(self.boundingRect(), self._ambient_tint_color)

        # Inset the cell grid + cursor by `_PADDING_PX` on all sides.
        # `translate()` chains into every subsequent painter op (clip,
        # fillRect, drawText) so _paint_row / _flush_run / _paint_cursor
        # stay 0-relative — no per-method offset plumbing needed.
        # Pairs with `_push_current_size`'s `inner_w/inner_h` math so
        # pyte's column/row count matches what we actually paint.
        painter.save()
        painter.translate(_PADDING_PX, _PADDING_PX)

        # Grid-exact clip (gotcha #11). QML's float-sized boundingRect
        # can extend marginally past the cell grid; clipping to the
        # exact (cols*cw, rows*ch) prevents stale-content leaks at the
        # edges. Pooled _clip_rect — no fresh QRectF allocation.
        # In translated coords this clip projects to
        # (pad, pad, pad+cols*cw, pad+rows*ch) in widget space.
        self._clip_rect.setRect(0, 0, cols * cw, rows * ch)
        painter.setClipRect(self._clip_rect)

        buf = screen.buffer
        for row_idx in range(rows):
            self._paint_row(painter, buf[row_idx], row_idx, cw, ch, cols)

        # Cursor painted last so it overlays the row content. v1 is a
        # solid block with no blink and no shape-modes — terminals
        # canonically use steady block; vim's mode-aware cursor lives
        # inside the shell, not in our chrome.
        if not screen.cursor.hidden:
            self._paint_cursor(painter, screen, cw, ch)

        painter.restore()

    def _paint_row(
        self,
        painter: QPainter,
        row: Any,
        row_idx: int,
        cw: float,
        ch: float,
        cols: int,
    ) -> None:
        """Paint one row, coalescing adjacent same-style cells.

        Algorithm mirrors NvimView._paint_row's run-coalescing — read
        cells left-to-right, accumulate chars into `_run_chars` until
        the highlight tuple `(fg, bg, bold, italic)` changes, then
        flush via `_flush_run` (one fillRect + one drawText per run).

        pyte `.reverse` is handled by swapping fg/bg at the read site —
        the rest of the paint flow stays unaware. This matches how
        every terminal emulator handles reverse-video.

        Run key omissions (v1 intentional deferrals):
        - `underscore` and `strikethrough` — not rendered in v1. Before
          adding drawLine-based decorators in v2, add these fields to the
          run key `(fg, bg, bold, italic, underscore, strikethrough)` so
          attribute-boundary breaks coalesce correctly.
        - `blink` — not animated in v1 (would need a frame clock). Same
          key extension applies when blink support is added.
        """
        y = row_idx * ch

        run_start = 0
        run_fg: Any = None
        run_bg: Any = None
        run_bold = False
        run_italic = False
        self._run_chars.clear()

        for col_idx in range(cols):
            cell = row[col_idx]
            fg = cell.fg
            bg = cell.bg
            bold = bool(cell.bold)
            italic = bool(cell.italics)
            if cell.reverse:
                fg, bg = bg, fg

            if col_idx == 0:
                run_fg = fg
                run_bg = bg
                run_bold = bold
                run_italic = italic
            elif (fg, bg, bold, italic) != (run_fg, run_bg, run_bold, run_italic):
                self._flush_run(
                    painter,
                    run_start,
                    col_idx,
                    y,
                    cw,
                    ch,
                    run_fg,
                    run_bg,
                    run_bold,
                    run_italic,
                )
                self._run_chars.clear()
                run_start = col_idx
                run_fg = fg
                run_bg = bg
                run_bold = bold
                run_italic = italic

            self._run_chars.append(cell.data or " ")

        if self._run_chars:
            self._flush_run(
                painter,
                run_start,
                cols,
                y,
                cw,
                ch,
                run_fg,
                run_bg,
                run_bold,
                run_italic,
            )
            self._run_chars.clear()

    def _flush_run(
        self,
        painter: QPainter,
        start_col: int,
        end_col: int,
        y: float,
        cw: float,
        ch: float,
        fg: Any,
        bg: Any,
        bold: bool,
        italic: bool,
    ) -> None:
        """Paint one coalesced run: fillRect (if non-default bg) + drawText.

        `bg != "default"` gate preserves the wallpaper-blend invariant —
        cells with default bg show the ambient-tinted wallpaper through;
        only explicit-bg cells (selection, diff highlights inside an
        editor running in the terminal, etc.) paint opaquely.

        Pooled QRectF (`_run_rect`) mutated via setRect(). Memoized
        QColor via `_resolve_color`. No allocation inside this method.
        """
        x = start_col * cw
        w = (end_col - start_col) * cw
        self._run_rect.setRect(x, y, w, ch)

        bg_color = _resolve_color(bg, is_bg=True)
        if bg_color is not None:
            painter.fillRect(self._run_rect, bg_color)

        if bold or italic:
            painter.setFont(self._font_variants.get((bold, italic), self._font))
        else:
            painter.setFont(self._font)

        # _resolve_color with is_bg=False always returns a QColor (never None) —
        # only is_bg=True + "default" returns None. No guard needed.
        fg_color = _resolve_color(fg, is_bg=False)
        painter.setPen(fg_color)
        painter.drawText(self._run_rect, _TEXT_ALIGN_FLAGS, "".join(self._run_chars))

    def _paint_cursor(
        self,
        painter: QPainter,
        screen: Any,
        cw: float,
        ch: float,
    ) -> None:
        """Solid block cursor — no blink, no mode-shape variants in v1.

        The cell's glyph stays painted behind; the cursor fills over it
        with `Theme.accent.bright`. Reverse-video readability is good
        enough for v1 because the amber-bright cursor color contrasts
        with both light text and dark text rows. v2 could repaint the
        cell's glyph in inverted fg over the cursor block for a more
        accurate selection-style appearance.
        """
        cur_x = screen.cursor.x * cw
        cur_y = screen.cursor.y * ch
        self._cursor_rect.setRect(cur_x, cur_y, cw, ch)
        cursor_color = _resolve_color(_CURSOR_RGB, is_bg=False)
        if cursor_color is not None:
            painter.fillRect(self._cursor_rect, cursor_color)

    # --- Input ---------------------------------------------------------

    def keyPressEvent(self, event: QKeyEvent) -> None:
        # Diagnostic: SYMMETRIA_IDE_KEY_TRACE=1 logs every key event that
        # reaches us. Used to triage cases like Ctrl+E silently doing
        # nothing — answers (a) does the event reach keyPressEvent at all,
        # (b) what Qt.Key value, (c) what text(). No-cost when unset.
        if os.environ.get("SYMMETRIA_IDE_KEY_TRACE"):
            log.info(
                "key event: key=0x%x text=%r mods=%r",
                event.key(),
                event.text(),
                event.modifiers(),
            )

        # Intercept Ctrl+Shift+V for clipboard paste — the modern
        # xterm-class convention shared by kitty / alacritty / wezterm
        # / gnome-terminal. Bare Ctrl+V keeps its terminal meaning
        # (sends `\x16` / SYN, which bash readline uses as
        # quoted-insert / literal-next-key), so this interception
        # only fires when Shift is ALSO held.
        #
        # WARNING — v2 deferral: bracketed-paste protocol (DECSET 2004).
        # bash and zsh do NOT enable DECSET 2004 by default; fish does.
        # Without it, every \n in the pasted text is sent raw to the
        # shell, which interprets it as an immediate Return — i.e. each
        # line in a multi-line paste executes as a separate command.
        # The bracketed-paste fix (~20 lines: wrap bytes in
        # `\x1b[200~ ... \x1b[201~` when DECSET 2004 is active) is
        # the correct v2 follow-up.
        #
        # Modifier check — bitwise containment, NOT equality: some
        # keyboards / XKB layouts silently OR in state modifiers
        # (KeypadModifier when NumLock is on, GroupSwitchModifier on
        # AltGr keyboards). An equality check breaks the chord for
        # those users. The containment form fires iff Ctrl AND Shift
        # are both held, regardless of other active modifiers.
        _PASTE_MODS = (
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
        )
        if (
            event.modifiers() & _PASTE_MODS == _PASTE_MODS
            and event.key() == Qt.Key.Key_V
        ):
            # Always accept+return to suppress the SYN byte translate_key
            # would produce for Ctrl+V — even when backend is absent or
            # clipboard text is empty. QGuiApplication.clipboard() cannot
            # return None here: TerminalView is a QQuickPaintedItem, which
            # can only exist after QGuiApplication is live.
            if self._backend is not None:
                text = QGuiApplication.clipboard().text()
                self._backend.write(text.encode("utf-8"))
            event.accept()
            return

        data = translate_key(event.key(), event.text(), event.modifiers())
        if data is None:
            event.ignore()
            return
        if self._backend is not None:
            self._backend.write(data)
        event.accept()
