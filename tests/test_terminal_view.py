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
    _PADDING_PX,
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


def test_font_resolved_via_shared_default_font():
    """TerminalView uses the shared `editor_font.default_font()` so every
    surface + the chrome's editorFontFamily share the same primary family
    + Nerd Font / emoji fallback chain, and cell metrics line up exactly.
    (Relocated out of the deleted NvimView._default_font.)"""
    init_src = inspect.getsource(TerminalView.__init__)
    assert "default_font()" in init_src


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


def test_ansi_palette_matches_theme_qml():
    """Every ANSI slot's `_ANSI_PALETTE` hex must match the resolved hex
    in Theme.qml. Slots 0–8 and slot 15 alias `mode.*` / `text.*` in the
    QML side (e.g. `color1: theme.color.mode.replace`); the assertion
    here pins the RESOLVED hex value (`#D2602D` for slot 1) so a future
    nudge to either side surfaces as a test failure.

    Pattern matches `test_default_fg_matches_theme_text_normal` /
    `test_cursor_color_matches_theme_accent_bright` — assert the hex
    literal appears somewhere in Theme.qml's text (it will, either
    inline in the `color1: ...` declaration if explicit, or in the
    aliased `mode.replace: "#D2602D"` declaration).

    Drift is detected in either direction:
      - If `_ANSI_PALETTE[1]` changes here, py_hex no longer matches `expected`.
      - If Theme.qml's `mode.replace` (or any aliased token) drifts away
        from #D2602D, the `in theme_src` assertion fails.

    The dual-source-of-truth pain is real, but bounded: the v2 refactor
    that wires Theme through Python via context property removes it.
    """
    theme_src = _read_theme_qml()
    # Map: ANSI slot index → expected hex string (lowercase, no #).
    # Slots 0–8 + 15 are aliased in Theme.qml; slots 9–14 are explicit hex.
    # The aliased values match the resolved `mode.*` / `text.*` literals.
    expected_hex_for_slot = {
        0: "131313",  # black           ← Theme.mode.badgeLabel
        1: "d2602d",  # red             ← Theme.mode.replace
        2: "62ba46",  # green           ← Theme.mode.insert
        3: "c28b12",  # yellow          ← Theme.mode.normal
        4: "6d94e9",  # blue            ← Theme.mode.command
        5: "d86de9",  # magenta         ← Theme.mode.visual
        6: "5bdfd8",  # cyan            ← Theme.mode.terminal
        7: "b0b0b0",  # white           ← Theme.text.normal
        8: "7a7a7a",  # bright black    ← Theme.text.dim
        9: "e58b5c",  # bright red      (explicit hex in Theme.qml)
        10: "86d666",  # bright green   (explicit hex in Theme.qml)
        11: "e5b142",  # bright yellow  (explicit hex in Theme.qml)
        12: "9cb6f0",  # bright blue    (explicit hex in Theme.qml)
        13: "e69bf0",  # bright magenta (explicit hex in Theme.qml)
        14: "8ae9e4",  # bright cyan    (explicit hex in Theme.qml)
        15: "f5f5f5",  # bright white   ← Theme.text.selected
    }
    for slot, expected in expected_hex_for_slot.items():
        py_hex = f"{_ANSI_PALETTE[slot]:06x}"
        assert py_hex == expected, (
            f"_ANSI_PALETTE[{slot}] = 0x{py_hex} but expected 0x{expected}"
        )
        # And it must appear in Theme.qml — either as an explicit hex
        # in the `Theme.color.terminal.*` block (slots 9–14) or via an
        # aliased mode.*/text.* token declaration (slots 0–8, 15).
        assert expected.upper() in theme_src.upper(), (
            f"Theme.qml is missing the hex value {expected} for color{slot} — "
            "either the explicit terminal-block hex drifted, OR the aliased "
            "mode.*/text.* token was nudged out of sync"
        )


def test_default_fg_matches_theme_text_normal():
    """_DEFAULT_FG_RGB must equal Theme.color.text.normal (#b0b0b0)."""
    theme_src = _read_theme_qml()
    assert f"{_DEFAULT_FG_RGB:06x}".upper() == "B0B0B0"
    assert "#b0b0b0" in theme_src.lower()


def test_cursor_color_is_white():
    """_CURSOR_RGB must equal `0xF5F5F5` (same hex as ANSI slot 15 /
    Theme.text.selected). The original choice (`0xE8AB6F`, the editor's
    warm-amber accent) read as a yellow-orange tint against the
    wallpaper-blend bg rather than as a cursor — kitty / alacritty /
    ghostty all default to a near-white cursor for the same readability
    reason. If a future refactor pins this back to the amber accent,
    the visual symptom returns instantly.
    """
    assert f"{_CURSOR_RGB:06x}".upper() == "F5F5F5"
    # Same hex appears as bright-white slot 15 in the ANSI palette and
    # as Theme.text.selected in Theme.qml; cross-check the latter as
    # drift-protection in case Theme.qml drifts independently.
    theme_src = _read_theme_qml()
    assert "#f5f5f5" in theme_src.lower()


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


def test_integer_rgb_resolves():
    """_resolve_color accepts raw 24-bit RGB integers — the primary
    consumer is `_paint_cursor`, which passes `_CURSOR_RGB` (an int)
    directly instead of going through the string-keyed name table."""
    color = _resolve_color(0xD2602D, is_bg=False)
    assert color is not None
    assert color.red() == 0xD2
    assert color.green() == 0x60
    assert color.blue() == 0x2D


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


# ---------------------------------------------------------------------------
# Padding constant — drift detection + _push_current_size accounting
# ---------------------------------------------------------------------------


def test_padding_px_constant_value():
    """_PADDING_PX must equal 20 — mirrors Ghostty's
    `window-padding-x = 20` / `window-padding-y = 20` so the terminal
    pane reads with the same framed composition as standalone Ghostty
    windows. If this drifts, the visual contract silently breaks.
    """
    assert _PADDING_PX == 20


def test_push_current_size_subtracts_padding():
    """_push_current_size MUST subtract `2 * _PADDING_PX` from width
    and height before floor-dividing by cell metrics. Without this,
    pyte thinks the terminal is wider/taller than the actual inset
    paint region, causing the rightmost column to fall outside the clip.
    """
    src = inspect.getsource(TerminalView._push_current_size)
    # The subtraction can appear as either `2 * _PADDING_PX` or
    # `_PADDING_PX * 2`; the `inner_w` / `inner_h` locals are what
    # the existing codebase uses.
    assert "inner_w" in src and "inner_h" in src, (
        "_push_current_size must derive inner_w/inner_h by subtracting "
        "the padding ring before computing cols/rows"
    )
    assert "_PADDING_PX" in src, (
        "_push_current_size must reference _PADDING_PX for the subtraction "
        "so the constant stays the single source of truth"
    )


def test_paint_uses_save_restore_around_translate():
    """paint() MUST bracket the `painter.translate(_PADDING_PX, …)` call
    with `painter.save()` and `painter.restore()`. Without `restore()`,
    the translation accumulates across frames (no fresh QPainter per
    frame guarantee from QQuickPaintedItem), causing subsequent ambient-
    tint fills to drift by the padding offset — a silent visual bug.
    """
    paint_src = inspect.getsource(TerminalView.paint)
    assert "painter.save()" in paint_src, (
        "paint() must call painter.save() before translate() so the "
        "padding transform is bounded to the cell-grid region"
    )
    assert "painter.restore()" in paint_src, (
        "paint() must call painter.restore() after painting rows+cursor "
        "to undo the translate() applied for the padding inset"
    )
    save_idx = paint_src.find("painter.save()")
    translate_idx = paint_src.find("painter.translate(")
    restore_idx = paint_src.find("painter.restore()")
    assert save_idx < translate_idx < restore_idx, (
        "save() must come before translate(), and restore() must come after "
        "all cell painting — order: save → translate → paint rows+cursor → restore"
    )


# ---------------------------------------------------------------------------
# Ctrl+Shift+V paste (PR 6) — xterm-class convention: bare Ctrl+V keeps
# its terminal meaning (sends SYN / `\x16`, used by bash readline's
# quoted-insert), Ctrl+Shift+V routes through the clipboard.
# ---------------------------------------------------------------------------


def test_paste_chord_intercepted_in_key_press():
    """keyPressEvent MUST intercept Ctrl+Shift+V before falling through
    to translate_key. Without the intercept, terminal_keys.translate
    would forward `\\x16` (the SYN char Qt encodes Ctrl+V as) to the
    shell — the user would see no paste, just a literal control byte."""
    src = inspect.getsource(TerminalView.keyPressEvent)
    assert "Qt.Key.Key_V" in src, (
        "Paste chord detection missing — keyPressEvent does not check Key_V"
    )
    # _PASTE_MODS is now a module-level constant (ControlModifier | ShiftModifier)
    # so keyPressEvent references it by name rather than spelling out both
    # modifiers inline. Verify the module constant carries both flags.
    import symmetria_ide.terminal_view as tv
    from PySide6.QtCore import Qt

    assert tv._PASTE_MODS == (
        Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
    ), "Paste chord must require BOTH Ctrl AND Shift"
    assert "_PASTE_MODS" in src, (
        "keyPressEvent must reference _PASTE_MODS for the paste chord check"
    )
    # The modifier check MUST be bitwise containment, not equality.
    # An equality check (`== (Ctrl | Shift)`) silently breaks for users
    # with NumLock on (KeypadModifier) or AltGr keyboards
    # (GroupSwitchModifier) — Qt ORs those state modifiers in.
    assert re.search(
        r"event\.modifiers\(\)\s*&\s*_PASTE_MODS\s*==\s*_PASTE_MODS", src
    ), (
        "Modifier check must be bitwise containment (`& _PASTE_MODS == _PASTE_MODS`), "
        "not equality — equality breaks Ctrl+Shift+V for users with NumLock/AltGr"
    )


def test_paste_uses_qgui_application_clipboard():
    """The paste path MUST read from QGuiApplication.clipboard().text()
    and forward the UTF-8 encoding to backend.write(). A regression
    that drops the clipboard read (or uses an empty literal) leaves
    Ctrl+Shift+V as a silent no-op."""
    src = inspect.getsource(TerminalView.keyPressEvent)
    assert "QGuiApplication.clipboard()" in src, (
        "Paste path must read from QGuiApplication.clipboard()"
    )
    assert ".text()" in src, "Paste path must call clipboard.text() to read the string"
    assert 'encode("utf-8")' in src, (
        "Paste payload must be UTF-8 encoded before backend.write()"
    )
    # Whitespace-only clipboard content (spaces, tabs, blank lines) is
    # intentional paste content — the `if text:` guard was removed so
    # that whitespace pastes are written rather than silently swallowed.
    assert "if text:" not in src, (
        "Paste path must not guard on `if text:` — whitespace-only "
        "clipboard content is intentional and must be forwarded to the backend"
    )


def test_paste_returns_early_so_translate_key_skipped():
    """After handling the paste chord, keyPressEvent MUST return
    early so the regular translate_key path doesn't ALSO process the
    same event — would result in double-paste (clipboard text + the
    SYN byte that translate_key would produce for Ctrl+V)."""
    src = inspect.getsource(TerminalView.keyPressEvent)
    # Use re.search so the assertion is robust against indentation
    # changes — a fixed-indent find() would give a false -1 failure
    # if the paste block is reformatted.
    paste_match = re.search(r"event\.accept\(\)\s*\n\s+return", src)
    translate_idx = src.find("data = translate_key")
    assert paste_match is not None, "Paste path must `event.accept()` + `return`"
    assert translate_idx >= 0, "translate_key fallthrough must still exist"
    assert paste_match.start() < translate_idx, (
        "Paste path must return BEFORE the translate_key fallthrough; "
        "otherwise paste also sends the SYN byte"
    )


def test_paste_imports_qgui_application():
    """terminal_view.py must import QGuiApplication — without it, the
    paste path is a NameError at runtime (silent until first chord press)."""
    import symmetria_ide.terminal_view as module

    src = inspect.getsource(module)
    assert "QGuiApplication" in src
    # Use a DOTALL regex to match the multi-line parenthesised import
    # block — a fixed char-window would silently pass if QGuiApplication
    # happened to appear after the 200-char boundary.
    assert re.search(
        r"from PySide6\.QtGui import[^)]*QGuiApplication", src, re.DOTALL
    ), "QGuiApplication must be imported from PySide6.QtGui"


# ---------------------------------------------------------------------------
# Underline / strikethrough (0.D) — decorations nvim relies on (LSP
# diagnostics, spell). Source-inspection per the module discipline.
# ---------------------------------------------------------------------------


def test_paint_row_run_key_includes_decorations():
    """The run-coalescing key MUST include underscore + strikethrough, or
    adjacent cells with differing decoration coalesce and the line is
    drawn across the wrong span (the gotcha _paint_row warns about)."""
    src = inspect.getsource(TerminalView._paint_row)
    assert "cell.underscore" in src, "must read underscore from the cell"
    assert "cell.strikethrough" in src, "must read strikethrough from the cell"
    # The run-break comparison must carry both, else coalescing is wrong.
    assert "run_underscore" in src
    assert "run_strike" in src


def test_flush_run_draws_decorations_without_qlinef():
    """_flush_run draws underline/strikethrough via the integer drawLine
    overload — NO QLineF/QLine wrapper allocation in the paint hot path
    (gotcha #10)."""
    src = inspect.getsource(TerminalView._flush_run)
    assert "drawLine" in src, "must draw decoration lines"
    assert "QLineF(" not in src, "no QLineF allocation in paint hot path"
    assert "QLine(" not in src, "no QLine allocation in paint hot path"
    assert "underscore" in src and "strikethrough" in src
