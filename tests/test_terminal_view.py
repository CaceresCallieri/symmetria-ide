"""Structural tests for the TerminalView QQuickPaintedItem renderer.

Instantiating TerminalView requires a `QGuiApplication` (QQuickItem +
QFontDatabase), which the session-scoped `qt_app` fixture in conftest.py
only provides at QCoreApplication level. Following the precedent set by
the NvimView test modules (test_paint_pool.py, test_transparent_mode.py,
test_default_font.py), we use source-inspection assertions — they catch
the same regressions a runtime test would, at lower fixture cost and
no flakiness.

Disciplines pinned here (these correspond 1:1 with the gotchas listed
in terminal_view.py's module docstring):

- gotcha #10  — paint() allocates no fresh QColor / QRectF
- gotcha #11  — clip is grid-exact, not boundingRect
- gotcha #23  — font cascade via setFamilies, shared with NvimView
- §4 P2       — backend signals connected via Qt.QueuedConnection
- frozenset   — screen_dirty payload type matches the backend contract
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from symmetria_ide.terminal_view import (
    _ANSI_PALETTE,
    _CURSOR_RGB,
    _DEFAULT_FG_RGB,
    _COLOR_NAME_TO_INDEX,
    _qcolor_cache,
    _resolve_color,
    TerminalView,
)


# ---------------------------------------------------------------------------
# QML registration — the @QmlElement decorator + module-level constants
# are what make Main.qml's `TerminalView { backend: terminalBackend }`
# resolve. Skip one of them and the import in app.py silently fails to
# register the type.
# ---------------------------------------------------------------------------


def test_qml_import_constants():
    """QML_IMPORT_NAME and version must match NvimView's namespace so the
    new TerminalView lands in the same `Symmetria.Ide 1.0` module Main.qml
    already imports."""
    from symmetria_ide import terminal_view

    assert terminal_view.QML_IMPORT_NAME == "Symmetria.Ide"
    assert terminal_view.QML_IMPORT_MAJOR_VERSION == 1


def test_terminal_view_has_qml_element_decoration():
    """The @QmlElement decorator registers the class with the QML engine.
    Without it, `TerminalView { … }` in QML raises 'is not a type'.
    Detect by reading the class's source for the decorator line."""
    src = inspect.getsource(TerminalView)
    # The decorator appears one line above `class TerminalView`.
    # Read the file directly to assert decoration order.
    module_src = inspect.getsource(inspect.getmodule(TerminalView))
    assert "@QmlElement\nclass TerminalView" in module_src, (
        "@QmlElement decorator missing or detached from class line — "
        "QML registration will silently fail"
    )
    # Keep the class-source variable referenced so a future refactor
    # that drops the decoration via partial re-write surfaces here too.
    assert "class TerminalView" in src


# ---------------------------------------------------------------------------
# Gotcha #10 — paint hot path must not allocate PySide wrappers
# ---------------------------------------------------------------------------


def test_paint_does_not_construct_qcolor():
    """`paint()` and its helpers MUST resolve colors through the memoized
    `_resolve_color` — never `QColor(...)` directly. Per gotcha #10, every
    fresh shiboken wrapper inside paint is a GC/race hazard on Python 3.14.
    """
    for method in (
        TerminalView.paint,
        TerminalView._paint_row,
        TerminalView._flush_run,
        TerminalView._paint_cursor,
    ):
        src = inspect.getsource(method)
        assert "QColor(" not in src, (
            f"{method.__name__}: fresh QColor(...) in paint path — "
            "must use _resolve_color memoized cache"
        )


def test_paint_does_not_construct_qrectf():
    """Paint loop MUST mutate pooled `_run_rect` / `_clip_rect` /
    `_cursor_rect` via `setRect()`. Fresh QRectF(...) inside paint is
    the next-most-likely gotcha #10 resurface candidate after QColor."""
    for method in (
        TerminalView.paint,
        TerminalView._paint_row,
        TerminalView._flush_run,
        TerminalView._paint_cursor,
    ):
        src = inspect.getsource(method)
        assert "QRectF(" not in src, (
            f"{method.__name__}: fresh QRectF(...) in paint path — "
            "must mutate pooled _run_rect / _clip_rect / _cursor_rect"
        )


def test_pooled_rects_allocated_in_init():
    """The three pooled QRectFs must be constructed in __init__ so paint
    can simply mutate them via setRect."""
    init_src = inspect.getsource(TerminalView.__init__)
    assert "self._run_rect = QRectF()" in init_src
    assert "self._clip_rect = QRectF()" in init_src
    assert "self._cursor_rect = QRectF()" in init_src


def test_paint_uses_setrect_for_pooled_rects():
    """The flush + clip + cursor paths must `setRect` (not construct)."""
    paint_src = inspect.getsource(TerminalView.paint)
    flush_src = inspect.getsource(TerminalView._flush_run)
    cursor_src = inspect.getsource(TerminalView._paint_cursor)
    assert "self._clip_rect.setRect" in paint_src
    assert "self._run_rect.setRect" in flush_src
    assert "self._cursor_rect.setRect" in cursor_src


# ---------------------------------------------------------------------------
# Gotcha #11 — clip to exact grid dimensions, not boundingRect
# ---------------------------------------------------------------------------


def test_clip_uses_grid_exact_dimensions_not_bounding_rect():
    """`setClipRect` MUST be called with `(0, 0, cols * cw, rows * ch)`,
    NOT `boundingRect()` — defense-in-depth against QML float-sized
    bounds leaking stale content past the cell grid (gotcha #11).
    """
    paint_src = inspect.getsource(TerminalView.paint)
    assert "setClipRect" in paint_src, (
        "paint() must call setClipRect for grid-exact clipping (gotcha #11)"
    )
    # The clip-rect setRect uses cols * cw / rows * ch.
    assert re.search(r"setRect\([^)]*cols\s*\*\s*cw", paint_src), (
        "Clip rect dims must be (cols * cw, rows * ch), not boundingRect"
    )


# ---------------------------------------------------------------------------
# Gotcha #23 — font cascade shared with NvimView
# ---------------------------------------------------------------------------


def test_font_resolved_via_nvim_view_default_font():
    """TerminalView reuses `NvimView._default_font()` so the editor and
    terminal share the same primary family + Nerd Font / emoji fallback
    chain — and cell metrics line up exactly. A regression that diverges
    the two fonts would break the visual continuity Q2-d topology demands.
    """
    init_src = inspect.getsource(TerminalView.__init__)
    assert "NvimView._default_font()" in init_src


def test_font_variants_pre_built():
    """Bold/italic variants must be built in __init__ — the paint loop
    never allocates a transient QFont."""
    init_src = inspect.getsource(TerminalView.__init__)
    assert "self._font_variants" in init_src
    # Loop over (bold, italic) combinations builds the dict.
    assert "for bold in" in init_src
    assert "for italic in" in init_src


# ---------------------------------------------------------------------------
# §4 P2 — cross-thread signal connects use QueuedConnection
# ---------------------------------------------------------------------------


def test_backend_signals_use_queued_connection():
    """`screen_dirty` and `screen_resized` cross from the reader thread
    to the GUI thread — both connects MUST specify Qt.QueuedConnection
    explicitly per project-standards §4 P2."""
    setter_src = inspect.getsource(TerminalView._set_backend)
    assert setter_src.count("Qt.ConnectionType.QueuedConnection") >= 2, (
        "Both screen_dirty and screen_resized connects need explicit "
        "Qt.QueuedConnection (§4 P2)"
    )


def test_screen_dirty_slot_accepts_frozenset():
    """The slot type must match the backend's `Signal(frozenset)` contract.
    PySide6 raises at emit-time if the types mismatch — catching it here
    means the failure shows up as a test, not a deferred KeyError at
    first repaint."""
    sig = inspect.signature(TerminalView._on_screen_dirty)
    # 'dirty' is the second param after self; annotation should reference frozenset.
    params = list(sig.parameters.values())
    assert len(params) == 2
    assert "frozenset" in str(params[1].annotation)


# ---------------------------------------------------------------------------
# ANSI palette — drift detection against Theme.qml
# ---------------------------------------------------------------------------


def test_ansi_palette_has_16_slots():
    """Standard ANSI palette has exactly 16 colors (8 normal + 8 bright)."""
    assert len(_ANSI_PALETTE) == 16


def test_color_name_to_index_maps_to_valid_slots():
    """Every pyte-emitted color name must map to a valid slot index."""
    for name, idx in _COLOR_NAME_TO_INDEX.items():
        assert 0 <= idx < 16, f"{name} → {idx} out of range"


def _read_theme_qml() -> str:
    """Load Theme.qml so palette tests can cross-check hex tokens."""
    repo_root = Path(__file__).resolve().parent.parent
    theme = repo_root / "qml" / "design" / "Theme.qml"
    return theme.read_text()


def test_ansi_palette_matches_theme_qml_explicit_hex():
    """The bright variants 9–14 + slot 15 carry explicit hex in Theme.qml
    (vs slots 1–8 which alias `mode.*` / `text.*` — those are equivalent
    by aliasing rather than by hex equality). Assert the explicit hex
    values match the `_ANSI_PALETTE` tuple here — drift on either side
    will fail this test, which is the contract we have until Theme is
    wired through Python via context property.
    """
    theme_src = _read_theme_qml()
    # Map: ANSI slot index → expected hex string (lowercase, no #)
    expected_hex_for_slot = {
        9: "e58b5c",  # bright red
        10: "86d666",  # bright green
        11: "e5b142",  # bright yellow
        12: "9cb6f0",  # bright blue
        13: "e69bf0",  # bright magenta
        14: "8ae9e4",  # bright cyan
    }
    for slot, expected in expected_hex_for_slot.items():
        py_hex = f"{_ANSI_PALETTE[slot]:06x}"
        assert py_hex == expected, (
            f"_ANSI_PALETTE[{slot}] = 0x{py_hex} but expected 0x{expected}"
        )
        # And it must appear in Theme.qml's terminal block.
        assert expected.upper() in theme_src.upper(), (
            f"Theme.qml is missing the hex value {expected} for color{slot}"
        )


def test_default_fg_matches_theme_text_normal():
    """_DEFAULT_FG_RGB must equal Theme.color.text.normal (#b0b0b0)."""
    theme_src = _read_theme_qml()
    assert f"{_DEFAULT_FG_RGB:06x}".upper() == "B0B0B0"
    assert "#b0b0b0" in theme_src.lower()


def test_cursor_color_matches_theme_accent_bright():
    """_CURSOR_RGB must equal Theme.color.accent.bright (#e8ab6f)."""
    theme_src = _read_theme_qml()
    assert f"{_CURSOR_RGB:06x}".upper() == "E8AB6F"
    assert "#e8ab6f" in theme_src.lower()


# ---------------------------------------------------------------------------
# _resolve_color behavior — pure-function tests, no Qt instantiation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_qcolor_cache():
    """Reset the module-level cache between tests so memoization state
    doesn't leak across cases."""
    _qcolor_cache.clear()
    yield
    _qcolor_cache.clear()


def test_default_bg_resolves_to_none():
    """The wallpaper-blend invariant: default-bg cells must NOT paint a
    fill (returns None so the caller skips fillRect)."""
    assert _resolve_color("default", is_bg=True) is None


def test_default_fg_resolves_to_default_color():
    """default fg returns a real QColor (the Theme.text.normal fallback)."""
    color = _resolve_color("default", is_bg=False)
    assert color is not None
    assert color.red() == 0xB0
    assert color.green() == 0xB0
    assert color.blue() == 0xB0


def test_named_color_resolves_to_ansi_slot():
    """A pyte color name maps to its ANSI slot's hex."""
    red = _resolve_color("red", is_bg=False)
    assert red is not None
    assert red.red() == 0xD2
    assert red.green() == 0x60
    assert red.blue() == 0x2D


def test_hex_string_resolves():
    """6-char hex string (typically from 256-color cube) resolves."""
    color = _resolve_color("a8e088", is_bg=False)
    assert color is not None
    assert color.red() == 0xA8
    assert color.green() == 0xE0
    assert color.blue() == 0x88


def test_unknown_name_falls_back_to_default():
    """Defensive: an unrecognised color name yields the default fg
    rather than crashing the paint loop."""
    color = _resolve_color("aubergine", is_bg=False)
    assert color is not None
    assert color.red() == 0xB0


def test_qcolor_is_memoized():
    """Calling _resolve_color twice with the same name returns the SAME
    QColor object (identity, not equality). This is the gotcha #10
    keystone — without identity-stability the paint loop allocates a
    fresh shiboken wrapper per cell."""
    first = _resolve_color("red", is_bg=False)
    second = _resolve_color("red", is_bg=False)
    assert first is second


# ---------------------------------------------------------------------------
# Misc: ambient tint pre-allocated, geometry clamps sane
# ---------------------------------------------------------------------------


def test_ambient_tint_pre_allocated_in_init():
    """The ambient-tint QColor (Ghostty-parity black @ 60% alpha) must
    be constructed in __init__ — paint must NOT allocate it per frame."""
    init_src = inspect.getsource(TerminalView.__init__)
    assert "self._ambient_tint_color = QColor(" in init_src


def test_push_current_size_clamps_to_minimums():
    """A degenerate QML geometry must not push pyte into a 0×0 state."""
    src = inspect.getsource(TerminalView._push_current_size)
    assert "max(20" in src and "max(5" in src, (
        "_push_current_size must clamp cols/rows to sane minimums"
    )
