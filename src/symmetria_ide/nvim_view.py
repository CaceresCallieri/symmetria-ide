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
from typing import Any

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

# Cursor animation tunables. Start from Neovide's defaults
# (`vim.g.neovide_cursor_animation_length = 0.075`, trail disabled) and
# slow by 20% — user-calibrated for the decoupled two-spring cadence
# introduced by `_update_cursor_destination`: once the cursor chases a
# moving scroll target instead of sitting still, 75 ms read as too
# snappy for large jumps. 90 ms / 48 ms give a calmer glide while
# still leading the 300 ms scroll spring.
# Ported from `neovide/src/renderer/cursor_renderer/mod.rs` @ main.
CURSOR_ANIMATION_LENGTH = 0.09        # seconds — main settle time
CURSOR_SHORT_ANIMATION_LENGTH = 0.048  # speedup for ≤2-cell horizontal jumps
# Neovide's spring early-out threshold is 0.01 in the spring's own
# units. Our cursor spring stores pixel deltas, so 0.01 px is fine.
_SPRING_EPSILON = 0.01

# Default editor font size in points. Not yet user-configurable —
# change here to tweak globally.
DEFAULT_FONT_POINT_SIZE = 9


def _spring_step(
    position: float,
    velocity: float,
    dt: float,
    animation_length: float,
) -> tuple[float, float]:
    """Critically-damped spring integrator — shared by scroll + cursor.

    Closed-form analytic step from Neovide's
    `CriticallyDampedSpringAnimation::update`
    (`src/renderer/animation_utils.rs` lines 88-123 at commit 04fcd7ac).
    Target is always 0.0; the caller either stores a displacement (scroll)
    or a remaining delta to destination (cursor).

    If `animation_length <= dt`, snaps to settled. Returns
    `(new_position, new_velocity)` — active/settled decision is the
    caller's to make (so thresholds can differ between units).
    """
    if animation_length <= dt:
        return 0.0, 0.0
    omega = 4.0 / animation_length
    a = position
    b = position * omega + velocity
    c = math.exp(-omega * dt)
    return (a + b * dt) * c, c * (-a * omega - b * dt * omega + b)


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

        Delegates the integrator math to the shared `_spring_step`; this
        class's contribution is unit (lines) and the 0.01-line settle
        threshold appropriate to that unit.
        """
        if not self.active:
            return False
        self.position, self.velocity = _spring_step(
            self.position, self.velocity, dt, SCROLL_ANIMATION_LENGTH
        )
        if abs(self.position) < _SPRING_EPSILON and abs(self.velocity) < _SPRING_EPSILON:
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


class CursorAnimation:
    """Two-axis spring animator that slides the cursor to its destination.

    Port of Neovide's cursor position spring, simplified to a single
    rectangle (the `trail_size = 0` case the user has configured). Unit
    is **pixels** — grid coords are converted via cell width/height by
    the caller before arriving here.

    Neovide-specific quirk we faithfully preserve
    (`neovide/src/renderer/cursor_renderer/mod.rs` lines 124-145 @ main):
    the spring stores the *remaining delta toward destination*, not an
    absolute position. When a new destination arrives mid-flight we
    re-seed `position_x/y` with the live remaining delta — velocity
    carries over automatically through the analytic formula, so
    chained cursor moves feel smooth rather than snappy.
    """

    __slots__ = (
        "_animation_length",
        "_current_x",
        "_current_y",
        "_destination_x",
        "_destination_y",
        "_position_x",
        "_position_y",
        "_velocity_x",
        "_velocity_y",
        "_seeded",
    )

    def __init__(self) -> None:
        # `current_x/y` is the live painted position in pixels.
        self._current_x: float = 0.0
        self._current_y: float = 0.0
        # `destination_x/y` is where the cursor should end up.
        self._destination_x: float = 0.0
        self._destination_y: float = 0.0
        # `position_x/y` / `velocity_x/y` are spring state — the REMAINING
        # delta toward destination (decays to 0).
        self._position_x: float = 0.0
        self._position_y: float = 0.0
        self._velocity_x: float = 0.0
        self._velocity_y: float = 0.0
        # Current animation length in seconds — varies with short-jump
        # detection. Set lazily by `set_destination`.
        self._animation_length: float = CURSOR_ANIMATION_LENGTH
        # Whether `_current_{x,y}` is trustworthy. False until the first
        # `set_destination` call so the first paint snaps rather than
        # sliding from 0,0.
        self._seeded: bool = False

    @property
    def current_x(self) -> float:
        return self._current_x

    @property
    def current_y(self) -> float:
        return self._current_y

    @property
    def active(self) -> bool:
        return (
            abs(self._position_x) >= _SPRING_EPSILON
            or abs(self._position_y) >= _SPRING_EPSILON
            or abs(self._velocity_x) >= _SPRING_EPSILON
            or abs(self._velocity_y) >= _SPRING_EPSILON
        )

    @property
    def seeded(self) -> bool:
        """True after the first set_destination call; False after reset()."""
        return self._seeded

    def set_destination(
        self,
        dest_x: float,
        dest_y: float,
        cell_w: float,
        cell_h: float,
    ) -> None:
        """Register a new destination in pixels.

        `cell_w` / `cell_h` are passed so we can classify the jump as
        "short" (≤ 2 cells horizontal AND 0 cells vertical → typing a
        word) and fall back to `CURSOR_SHORT_ANIMATION_LENGTH` for a
        snappier feel. This matches Neovide's rank-based speedup without
        the trail-specific split logic.

        Idempotent on destination: if the cursor is already heading to
        this exact pixel, we don't re-seed (which would zero velocity
        even though position hasn't changed). This keeps per-frame
        paints that happen to re-call `set_destination` from stuttering.

        Short/long classification only runs when the spring is at rest
        (`not self.active`). Per-frame retargets during an active scroll
        must not reclassify — the remaining delta shrinks toward zero as
        the cursor chases the moving target, so the short-jump condition
        triggers spuriously and biases the decay rate mid-flight.
        """
        if not self._seeded:
            # First ever destination — snap to it so we don't animate
            # from (0, 0) at startup.
            self._current_x = dest_x
            self._current_y = dest_y
            self._destination_x = dest_x
            self._destination_y = dest_y
            self._position_x = 0.0
            self._position_y = 0.0
            self._velocity_x = 0.0
            self._velocity_y = 0.0
            self._seeded = True
            return
        if dest_x == self._destination_x and dest_y == self._destination_y:
            return
        # Capture active state BEFORE re-seeding. After we write _position_x/y
        # below, `self.active` always evaluates True — we need the pre-seed
        # state to distinguish "freshly started jump" from "mid-flight retarget".
        was_active = self.active
        # Re-seed spring with the delta from the current PAINTED position
        # (not previous destination) so mid-flight redirects are smooth.
        self._position_x = dest_x - self._current_x
        self._position_y = dest_y - self._current_y
        self._destination_x = dest_x
        self._destination_y = dest_y
        # Short-jump speedup: same row (within half a cell), ≤ 2 cells
        # horizontal. Typing a letter scrolls the cursor right by one
        # cell — we want that to feel instantaneous rather than laggy.
        #
        # Only classify on a freshly-started jump (spring was at rest).
        # Per-frame retargets from `_update_cursor_destination` arrive
        # while the spring is already mid-flight (was_active=True) — the
        # remaining delta shrinks as the cursor approaches the moving
        # target, so the short-jump condition (`|pos_y| < cell_h*0.5`)
        # would trigger mid-flight and switch to the faster decay, biasing
        # the trajectory by up to ~4px for ~300ms. Preserving the
        # original classification during flight keeps the cadence stable.
        if not was_active:
            if (
                abs(self._position_y) < cell_h * 0.5
                and abs(self._position_x) <= cell_w * 2.0
            ):
                self._animation_length = CURSOR_SHORT_ANIMATION_LENGTH
            else:
                self._animation_length = CURSOR_ANIMATION_LENGTH

    def tick(self, dt: float) -> bool:
        """Advance spring one frame. Returns True iff still animating.

        Updates `_current_x/y` so the view can paint at the animated
        position. Returns False when we've settled on destination.
        """
        if not self.active:
            # Defensive snap: in normal flow current == destination when
            # active is False, so this is a no-op. Corrects silently if
            # they ever diverge (e.g. reset race). Also avoids a needless
            # spring step on the already-settled fast path.
            self._current_x = self._destination_x
            self._current_y = self._destination_y
            return False
        self._position_x, self._velocity_x = _spring_step(
            self._position_x, self._velocity_x, dt, self._animation_length
        )
        self._position_y, self._velocity_y = _spring_step(
            self._position_y, self._velocity_y, dt, self._animation_length
        )
        # current = destination - remaining_delta (invert the spring's
        # "decay toward 0" into "approach destination").
        self._current_x = self._destination_x - self._position_x
        self._current_y = self._destination_y - self._position_y
        if not self.active:
            self._current_x = self._destination_x
            self._current_y = self._destination_y
            return False
        return True

    def reset(self) -> None:
        self._current_x = 0.0
        self._current_y = 0.0
        self._destination_x = 0.0
        self._destination_y = 0.0
        self._position_x = 0.0
        self._position_y = 0.0
        self._velocity_x = 0.0
        self._velocity_y = 0.0
        self._seeded = False
        self._animation_length = CURSOR_ANIMATION_LENGTH


class CursorBlink:
    """Smooth-blink state machine for the cursor's opacity envelope.

    Port of Neovide's `BlinkStateMachine`
    (`neovide/src/renderer/cursor_renderer/blink.rs`). Opacity is a
    *linear* triangle wave (not a spring) sampled from wall-clock at
    paint time — sampling from `dt` would stair-step when the frame
    clock stalls.

    Lifecycle:
      - `set_timings(wait, on, off)` initializes or refreshes the state
        machine when nvim's `mode_info_set` changes (new shape brings
        new timings from `guicursor`).
      - `opacity_at(now)` advances internal phase and returns [0, 1].
      - Any of `wait / on / off == 0` disables blinking entirely and
        the cursor stays fully opaque (matches Neovide's
        `is_static_cursor`).

    Neovide intentionally does NOT reset blink on cursor *move* — only
    on cursor *shape* change. This keeps the cursor visible while you're
    holding `j` (it doesn't restart the blink timer every keystroke).
    The user opted into this behavior explicitly in design review.
    """

    __slots__ = (
        "_static",
        "_wait_s",
        "_on_s",
        "_off_s",
        "_phase_start",
        "_phase",
    )

    # Phase constants. Kept small-int so `__slots__` storage is tight.
    PHASE_WAITING = 0  # blinkwait period at the start; opacity = 1
    PHASE_ON = 1       # fading 1 -> 0 across blinkon
    PHASE_OFF = 2      # fading 0 -> 1 across blinkoff

    def __init__(self) -> None:
        self._static: bool = True
        self._wait_s: float = 0.0
        self._on_s: float = 0.0
        self._off_s: float = 0.0
        self._phase_start: float = 0.0
        self._phase: int = CursorBlink.PHASE_WAITING

    def set_timings(
        self,
        blinkwait_ms: int,
        blinkon_ms: int,
        blinkoff_ms: int,
        now: float,
    ) -> None:
        """Refresh from a mode_info entry's blinkwait/on/off (milliseconds).

        NeoVim exposes these as integer ms; Neovide treats any zero as
        "disable blinking for this mode" (matches `:h guicursor`).
        """
        new_static = (
            blinkwait_ms <= 0 or blinkon_ms <= 0 or blinkoff_ms <= 0
        )
        # Skip state reset if the timings haven't actually changed — the
        # mode_info_set event fires at startup and on `:set guicursor`
        # but our phase tracking should survive mode-to-mode switches
        # that happen to share timings.
        if (
            new_static == self._static
            and blinkwait_ms / 1000.0 == self._wait_s
            and blinkon_ms / 1000.0 == self._on_s
            and blinkoff_ms / 1000.0 == self._off_s
        ):
            return
        self._static = new_static
        self._wait_s = blinkwait_ms / 1000.0
        self._on_s = blinkon_ms / 1000.0
        self._off_s = blinkoff_ms / 1000.0
        self._phase = CursorBlink.PHASE_WAITING
        self._phase_start = now

    @property
    def is_static(self) -> bool:
        return self._static

    @property
    def active(self) -> bool:
        """True iff this blink state will produce opacity changes over time."""
        return not self._static

    def opacity_at(self, now: float) -> float:
        """Return cursor opacity in [0, 1] sampled at wall-clock `now`.

        Advances internal phase as needed (waiting → on → off → on → ...).
        Linear ramp during ON/OFF; constant 1.0 during WAITING.
        """
        if self._static:
            return 1.0
        # Advance phases until `now` lands inside the current one. Guards
        # against loop-runaway if clock jumps backward by breaking on
        # zero/negative phase durations.
        while True:
            if self._phase == CursorBlink.PHASE_WAITING:
                duration = self._wait_s
            elif self._phase == CursorBlink.PHASE_ON:
                duration = self._on_s
            else:
                duration = self._off_s
            if duration <= 0.0:
                # Degenerate — treat as fully opaque.
                return 1.0
            elapsed = now - self._phase_start
            if elapsed < duration:
                break
            # Advance phase. After a long gap (tab away and come back)
            # `elapsed` can be many cycles long — fast-forward by subtracting
            # the phase duration rather than snapping to `now`, so we stay
            # in sync with wall clock.
            self._phase_start += duration
            if self._phase == CursorBlink.PHASE_WAITING:
                self._phase = CursorBlink.PHASE_ON
            elif self._phase == CursorBlink.PHASE_ON:
                self._phase = CursorBlink.PHASE_OFF
            else:
                self._phase = CursorBlink.PHASE_ON
            # Safety valve: if we've fallen catastrophically behind
            # (elapsed > 10× the current phase), rebase so we don't burn
            # CPU iterating. Matches Neovide's "lagging badly" guard.
            if now - self._phase_start > max(self._wait_s, self._on_s, self._off_s) * 10:
                self._phase_start = now
        # Compute opacity within the current phase.
        elapsed = now - self._phase_start
        if self._phase == CursorBlink.PHASE_WAITING:
            return 1.0
        if self._phase == CursorBlink.PHASE_ON:
            # Fading out across the ON window (Neovide's inversion —
            # the "on" period ends with opacity approaching 0 so the
            # transition into OFF is continuous).
            remaining = self._on_s - elapsed
            return max(0.0, min(1.0, remaining / self._on_s))
        # PHASE_OFF — fading in from 0 to 1.
        return max(0.0, min(1.0, elapsed / self._off_s))

    def reset_phase(self, now: float) -> None:
        """Restart blink cycle (e.g. on mode/shape change)."""
        self._phase = CursorBlink.PHASE_WAITING
        self._phase_start = now


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
        # Pre-allocate the ambient tint color so paint() does not create a
        # fresh shiboken wrapper every frame. Per CLAUDE.md gotcha #10,
        # any PySide6 wrapper allocated inside paint() is a GC/race hazard
        # on Python 3.14 — cache here, reference in paint().
        self._ambient_tint_color = QColor(0, 0, 0, 153)
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

        # --- Cursor animation state (GUI-thread-only) -------------------
        # Position spring — slides cursor in pixel space to its target
        # grid cell. Unseeded until the first paint so the cursor snaps
        # to its initial position rather than animating in from (0, 0).
        self._cursor_anim = CursorAnimation()
        self._cursor_blink = CursorBlink()
        # Last mode descriptor received from the backend. Defaults read
        # "solid block, no blink" so startup before mode_info_set still
        # paints a sensible cursor.
        self._cursor_mode: dict[str, Any] = {}

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
                font.setPointSize(DEFAULT_FONT_POINT_SIZE)
                font.setStyleHint(QFont.StyleHint.Monospace)
                font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
                return font
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        font.setPointSize(DEFAULT_FONT_POINT_SIZE)
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
            try:
                self._backend.cursor_mode_updated.disconnect(self._on_cursor_mode_updated)
            except (RuntimeError, TypeError):
                pass
        self._backend = value
        if value is not None:
            value.redraw_flushed.connect(self._on_redraw_flushed)
            value.cursor_mode_updated.connect(self._on_cursor_mode_updated)
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

    @Slot(dict)
    def _on_cursor_mode_updated(self, descriptor: dict) -> None:
        """Store the current mode's cursor descriptor and refresh blink.

        We swallow the descriptor dict here and read it from
        `_paint_cursor` — the view doesn't need to know about any
        individual field until paint time.

        Blink timings come from `guicursor`. A change in shape/mode
        without timing changes leaves the blink phase alone (matches
        Neovide's "don't restart blink on cursor move" behavior).
        """
        self._cursor_mode = descriptor or {}
        blinkwait = int(self._cursor_mode.get("blinkwait", 0) or 0)
        blinkon = int(self._cursor_mode.get("blinkon", 0) or 0)
        blinkoff = int(self._cursor_mode.get("blinkoff", 0) or 0)
        self._cursor_blink.set_timings(blinkwait, blinkon, blinkoff, time.perf_counter())
        # Any blink state change may require the frame driver to keep
        # ticking even when cursor/scroll position are settled.
        self._maybe_start_frame_driver()
        self.update()

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
            self._update_cursor_destination()
        except Exception:  # noqa: BLE001
            log.exception("scroll-delta application failed; animation reset")
            self._reset_animation_state_after_error()
        self.update()

    def _update_cursor_destination(self) -> None:
        """Retarget the cursor spring at the scroll-adjusted cell position.

        Ported from Neovide's `CursorRenderer::update_cursor_destination`
        (`src/renderer/cursor_renderer/mod.rs:294-330` @ main). The
        cursor's target is NOT just `cur_row * cell_h` — it's offset by
        the LIVE scroll spring value:

            dest_y = (cur_row - scroll_anim.position) * cell_h

        Rationale: nvim reports `grid_cursor_goto` in viewport-relative
        coords, so for Ctrl-d/Ctrl-u/gg/G, `cur_row` is unchanged (both
        cursor buffer-line and topline moved by the same delta). But
        during the animation, the rendered content is *shifted* by the
        scroll spring — the buffer line the cursor actually lives on is
        currently painted at row `(cur_row - position)`. Pointing the
        cursor spring at that shifted position makes the cursor follow
        its buffer line as the scroll animates, arriving at its final
        screen row as the scroll settles.

        Two springs, different time constants: scroll settles in ~300 ms,
        cursor in ~90 ms. Because the cursor spring is ~3.3× faster than
        the target's decay, the cursor converges on the moving target
        quickly and visually "leads" the scroll — the layered cadence
        effect Neovide is known for. See CLAUDE.md gotcha #11 for the
        history (this formula previously failed when there was no cursor
        spring to animate around the shifted target).

        Called from two places:
        1. `_on_redraw_flushed` — after a scroll batch lands, so the
           cursor spring is seeded with the full delta at the moment
           the scroll spring is shifted.
        2. `_on_frame_swapped` (every frame while animating) — so the
           target tracks the scroll spring's decay and the cursor
           spring chases a moving destination with preserved velocity.
        """
        if self._backend is None:
            return
        grid = self._backend.grid
        if grid.rows <= 0 or grid.cols <= 0:
            return
        cur_row = grid.cursor_row
        cur_col = grid.cursor_col
        if not (0 <= cur_row < grid.rows and 0 <= cur_col < grid.cols):
            return
        dest_x = cur_col * self._cell_w
        dest_y = (cur_row - self._scroll_anim.position) * self._cell_h
        self._cursor_anim.set_destination(dest_x, dest_y, self._cell_w, self._cell_h)
        if self._cursor_anim.active:
            self._maybe_start_frame_driver()

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
        self._cursor_anim.reset()
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

    def _animation_is_active(self) -> bool:
        """Any source of per-frame repaint demand.

        Scroll spring mid-flight, cursor spring mid-flight, or a
        non-static blink state all require `frameSwapped` ticks to keep
        the paint loop advancing. If all three are inactive we disconnect
        to return to idle (zero CPU cost).
        """
        return (
            self._scroll_anim.active
            or self._cursor_anim.active
            or self._cursor_blink.active
        )

    def _maybe_start_frame_driver(self) -> None:
        """Connect to frameSwapped if any animation source is active.

        Idempotent and defensive: if the item isn't attached to a
        QQuickWindow yet, defer — the windowChanged connection in
        __init__ will retry once the window becomes known.
        """
        if self._driver_connected or not self._animation_is_active():
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
            # Tick BOTH springs — each returns True iff still animating.
            # We still repaint while blink is active (opacity sampling is
            # wall-clock-based inside _paint_cursor, so we only need a
            # frame to land there).
            #
            # ORDER MATTERS. Scroll ticks first to produce the post-tick
            # `scroll_anim.position`; we then retarget the cursor spring
            # at `(cur_row - position) * cell_h` so the cursor chases the
            # scroll's *live* decay (Neovide's decoupled-springs feel —
            # see `_update_cursor_destination` for the full rationale).
            # Finally the cursor spring ticks toward the new target with
            # velocity preserved across the reseed.
            self._scroll_anim.tick(dt)
            self._update_cursor_destination()
            self._cursor_anim.tick(dt)
            self.update()
            if not self._animation_is_active():
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
            if self._animation_is_active():
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
            # Resize changes cell pixel dimensions; cached cursor pixel
            # coords become meaningless. Reset so the next paint re-seeds.
            self._cursor_anim.reset()
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
        painter.fillRect(self.boundingRect(), self._ambient_tint_color)

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
        """Animated cursor with shape and smooth blink.

        Draws at the spring's animated pixel position (see
        `_update_cursor_destination` for the target formula — during an
        active scroll animation the target is offset by the live scroll
        spring value so the cursor chases a moving destination, which
        produces Neovide's layered-cadence feel).

        Shape comes from `_cursor_mode["cursor_shape"]` + `cell_percentage`
        (NeoVim `mode_info_set`):
            - "block": full reverse-video cell, glyph visible.
            - "vertical": thin bar at leading edge, `cell_percentage`% of
              cell width (insert mode's `ver25`).
            - "horizontal": thin bar at cell bottom, `cell_percentage`%
              of cell height (cmdline/replace `hor20`, etc.).

        Opacity comes from the blink state machine. Static cursors
        (any of blinkwait/on/off == 0) are always fully opaque.
        """
        cur_row = grid.cursor_row
        cur_col = grid.cursor_col
        if not (0 <= cur_row < grid.rows and 0 <= cur_col < grid.cols):
            return
        # Bootstrap: if the spring hasn't been seeded yet (first paint
        # before the first flush-driven `set_destination`), snap to the
        # cell now so we don't draw at (0, 0).
        if not self._cursor_anim.seeded:
            self._cursor_anim.set_destination(
                cur_col * cw, cur_row * ch, cw, ch,
            )
        x = self._cursor_anim.current_x
        y = self._cursor_anim.current_y

        # Resolve shape / cell_percentage / opacity. Defaults match a
        # plain block cursor when mode_info_set hasn't arrived yet.
        shape = self._cursor_mode.get("cursor_shape", "block")
        cell_pct = int(self._cursor_mode.get("cell_percentage", 100) or 100)
        opacity = self._cursor_blink.opacity_at(time.perf_counter())

        # Qt doesn't have a painter-wide opacity on fills/text directly;
        # setOpacity() works but it's a state change — save/restore so we
        # don't leak this into the next paint call (which handles the
        # grid and doesn't want a dimmed pass).
        painter.save()
        try:
            painter.setOpacity(opacity)
            if shape == "vertical":
                # Bar at the cell's leading edge. `cell_percentage` is
                # thickness as a fraction of cell width (25 → 25% → a
                # 1.75-px bar at 7px-wide cells; Qt handles sub-pixel).
                bar_w = max(1.0, cw * cell_pct / 100.0)
                rect = QRectF(x, y, bar_w, ch)
                painter.fillRect(
                    rect, _rgb_to_qcolor(grid.default_fg, 0xD0D0D0)
                )
                # No glyph overlay for vertical bars — the underlying
                # grid paint already shows the character at that cell.
            elif shape == "horizontal":
                # Underline at the cell's bottom edge. Thickness is
                # `cell_percentage`% of cell height.
                bar_h = max(1.0, ch * cell_pct / 100.0)
                rect = QRectF(x, y + ch - bar_h, cw, bar_h)
                painter.fillRect(
                    rect, _rgb_to_qcolor(grid.default_fg, 0xD0D0D0)
                )
            else:
                # Block (default) — reverse-video the cell: fg-colored
                # background with the cell glyph in bg color on top.
                cursor_cell = grid.cells[cur_row][cur_col]
                rect = QRectF(x, y, cw, ch)
                painter.fillRect(
                    rect, _rgb_to_qcolor(grid.default_fg, 0xD0D0D0)
                )
                painter.setPen(_rgb_to_qcolor(grid.default_bg, 0x1E1E1E))
                painter.drawText(
                    rect,
                    int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                    cursor_cell.char,
                )
        finally:
            painter.restore()

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
