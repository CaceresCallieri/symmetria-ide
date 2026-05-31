"""Structural tests for the MinimapView QQuickPaintedItem (Phase 0).

Instantiating a `QQuickPaintedItem` subclass requires a `QGuiApplication`
(QQuickItem + QFontDatabase), which the session-scoped `qt_app` fixture
in `tests/conftest.py` only provides at `QCoreApplication` level.
Following the precedent set by `tests/test_terminal_view.py` and the
NvimView test modules, we use SOURCE-INSPECTION assertions plus a small
set of pure-function checks — they catch the same regressions a runtime
paint test would, at lower fixture cost and zero flakiness.

Disciplines pinned here (correspond 1:1 with the gotchas + project
standards referenced from `minimap_view.py`'s module docstring):

- gotcha #10  — paint() allocates no fresh QColor / QRectF
- gotcha #7   — QML registration constants + decoration intact
- §3.3        — Theme tokens are the single source of truth for chrome
                colour, drift between Theme.qml and the Python mirror
                is detected here (mirrors the ANSI palette test pattern)
- focus rule  — minimap is not a focus target (ItemIsFocusScope is NOT
                set; setActiveFocusOnTab(False) keeps Tab cycling away)
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

from PySide6.QtGui import QColor

from symmetria_ide.minimap_view import (
    _BACKGROUND_RGBA,
    _background_color,
    MinimapView,
)


# ---------------------------------------------------------------------------
# QML registration — the @QmlElement decorator + module-level constants
# are what make Main.qml's `MinimapView { … }` resolve. Skip one of them
# and the side-effect import in app.py silently fails to register.
# ---------------------------------------------------------------------------


def test_qml_import_constants():
    """QML_IMPORT_NAME and version must match the Symmetria.Ide 1.0
    namespace that Main.qml already imports, so MinimapView lands in
    the same module as NvimView and TerminalView."""
    from symmetria_ide import minimap_view

    assert minimap_view.QML_IMPORT_NAME == "Symmetria.Ide"
    assert minimap_view.QML_IMPORT_MAJOR_VERSION == 1


def test_minimap_view_has_qml_element_decoration():
    """The @QmlElement decorator registers the class with the QML engine.
    Without it, `MinimapView { … }` in QML raises 'is not a type'.
    Match the line-attached form so a refactor that detaches the
    decorator (blank line between @QmlElement and `class`) is caught.
    Pattern mirrors `test_terminal_view.py::test_terminal_view_has_
    qml_element_decoration`."""
    module_src = inspect.getsource(inspect.getmodule(MinimapView))
    assert "@QmlElement\nclass MinimapView" in module_src, (
        "@QmlElement decorator missing or detached from class line — "
        "QML registration will silently fail at engine load"
    )


def test_minimap_view_registered_in_app_module():
    """app.py must import MinimapView for the @QmlElement side-effect
    AND keep a `_ = MinimapView` reference inside the keep-imports
    function — without the reference, an automated import-pruner can
    strip the noqa: F401 import and break QML registration. Same
    two-layer protection pattern NvimView and TerminalView use."""
    from symmetria_ide import app as app_module

    app_src = inspect.getsource(app_module)
    assert "from .minimap_view import MinimapView" in app_src, (
        "MinimapView side-effect import missing from app.py — "
        "@QmlElement registration won't fire and Main.qml will raise"
    )
    assert "_ = MinimapView" in app_src, (
        "MinimapView keep-import reference missing — an import-pruner "
        "could silently drop the noqa: F401 import"
    )


# ---------------------------------------------------------------------------
# Gotcha #10 — paint hot path must not allocate PySide wrappers
# ---------------------------------------------------------------------------


def test_paint_does_not_construct_qcolor():
    """`paint()` MUST resolve colours through the module-level memoized
    `_background_color` — never `QColor(...)` directly. Per gotcha #10,
    every fresh shiboken wrapper inside paint is a GC/race hazard on
    Python 3.14. Phase 2's per-line block painter will extend this same
    method and must inherit the discipline."""
    src = inspect.getsource(MinimapView.paint)
    assert "QColor(" not in src, (
        "paint(): fresh QColor(...) in paint path — must use "
        "module-level memoized `_background_color` instead"
    )


def test_paint_does_not_construct_qrectf():
    """`paint()` MUST mutate the pooled `_paint_rect` via `setRect()`.
    Fresh QRectF(...) is the next-most-likely gotcha #10 resurface
    candidate after QColor (cf. TerminalView's _run_rect / _clip_rect /
    _cursor_rect pool)."""
    src = inspect.getsource(MinimapView.paint)
    assert "QRectF(" not in src, (
        "paint(): fresh QRectF(...) in paint path — must mutate the "
        "pooled `_paint_rect` via setRect()"
    )


def test_pooled_rect_allocated_in_init():
    """The pooled QRectF must be constructed in __init__ so paint can
    simply mutate it via setRect, never allocate."""
    init_src = inspect.getsource(MinimapView.__init__)
    assert "self._paint_rect = QRectF()" in init_src, (
        "MinimapView.__init__ must construct `_paint_rect = QRectF()` "
        "so paint can mutate it via setRect (gotcha #10)"
    )


def test_paint_uses_setrect_for_pooled_rect():
    """The fill path must `setRect` (not construct)."""
    src = inspect.getsource(MinimapView.paint)
    assert "self._paint_rect.setRect" in src, (
        "paint() must call self._paint_rect.setRect(...) — pooled "
        "QRectF mutation is the gotcha #10 contract"
    )


def test_background_color_memoized_at_module_load():
    """The background QColor MUST exist at module level (constructed
    once at import time) — paint must never allocate a fresh QColor
    per frame. Verify by checking the module attribute is a QColor
    and its alpha matches `_BACKGROUND_RGBA[3]`."""
    assert isinstance(_background_color, QColor)
    assert _background_color.alpha() == _BACKGROUND_RGBA[3]


def test_background_rgba_is_4_tuple():
    """The Theme drift-detection test relies on `_BACKGROUND_RGBA` being
    a 4-tuple (R, G, B, A). A future refactor to a 3-tuple would silently
    skip the alpha check."""
    assert len(_BACKGROUND_RGBA) == 4
    for channel in _BACKGROUND_RGBA:
        assert 0 <= channel <= 255


# ---------------------------------------------------------------------------
# Theme drift detection — Theme.color.minimap.background must match
# the Python-side _BACKGROUND_RGBA. Same dual-source-of-truth pattern as
# `test_ansi_palette_matches_theme_qml` in test_terminal_view.py.
# ---------------------------------------------------------------------------


def _read_theme_qml() -> str:
    """Load Theme.qml so palette tests can cross-check hex tokens.
    Same helper TerminalView's tests use."""
    repo_root = Path(__file__).resolve().parent.parent
    theme = repo_root / "qml" / "design" / "Theme.qml"
    return theme.read_text()


def test_background_matches_theme_qml():
    """`_BACKGROUND_RGBA` MUST equal the hex value in Theme.qml's
    `Theme.color.minimap.background`. Drift in either direction would
    visually desync the minimap from its Theme definition.

    The hex format in Theme.qml is `#AARRGGBB` (8-char form) for
    colours with alpha, where the first two chars are the alpha channel.
    For black @ 20% alpha, that's `#33000000`.
    """
    theme_src = _read_theme_qml()
    # Build the expected ARGB hex from the RGBA tuple.
    r, g, b, a = _BACKGROUND_RGBA
    expected_hex = f"#{a:02X}{r:02X}{g:02X}{b:02X}"
    # Theme.qml uses lowercase in its string literals — case-insensitive match.
    assert expected_hex.upper() in theme_src.upper(), (
        f"Theme.qml is missing the hex value {expected_hex} for "
        "Theme.color.minimap.background — either the Theme token was "
        "nudged out of sync, or _BACKGROUND_RGBA drifted away from it"
    )
    # And the property name must exist — protects against a refactor
    # that renames the token from `background` to e.g. `surface` and
    # leaves the test passing on stale hex coincidentally appearing
    # elsewhere.
    assert "minimap:" in theme_src or "minimap :" in theme_src, (
        "Theme.qml missing the `minimap` QtObject block under `color`"
    )
    assert re.search(r"background\s*:\s*\"#", theme_src), (
        "Theme.qml's minimap block must define a `background:` property"
    )


def test_minimap_width_token_exists_in_theme():
    """Theme.size.minimapWidth must be defined — Main.qml binds the
    minimap's `width` property to it. A missing token would silently
    collapse the minimap to zero width at engine load."""
    theme_src = _read_theme_qml()
    assert re.search(r"minimapWidth\s*:\s*\d+", theme_src), (
        "Theme.qml is missing `readonly property int minimapWidth: <n>` "
        "in the `size` block — Main.qml binding will fail at engine load"
    )


# ---------------------------------------------------------------------------
# Focus discipline — minimap must NOT be a focus target
# ---------------------------------------------------------------------------


def test_minimap_does_not_set_focus_scope_flag():
    """ItemIsFocusScope is set on focus-target panes (NvimView,
    TerminalView). The minimap is a VISUALISATION, not a focus
    target — setting the flag would let keyboard focus land here
    on Tab cycling and trap the user with no visible cursor.

    Look for the actual `setFlag(...)` API call rather than a bare
    string match, because the explanatory comment in __init__
    legitimately mentions the flag name to document the deliberate
    omission.
    """
    init_src = inspect.getsource(MinimapView.__init__)
    assert "setFlag(QQuickPaintedItem.Flag.ItemIsFocusScope" not in init_src, (
        "MinimapView must NOT call setFlag(...ItemIsFocusScope, True) — "
        "it's a visualisation, not a focus target"
    )
    # Also verify the helpful comment about the omission survives —
    # without the comment, a future reader has no signal that the
    # missing setFlag call was deliberate and not an oversight.
    assert "ItemIsFocusScope deliberately NOT set" in init_src, (
        "The intentional-omission comment about ItemIsFocusScope must "
        "remain so future readers don't 'fix' the oversight"
    )


def test_minimap_disables_active_focus_on_tab():
    """`setActiveFocusOnTab(False)` keeps Tab cycling away from the
    minimap pane. A regression that omits this would route Tab into
    the minimap as if it were a regular widget."""
    init_src = inspect.getsource(MinimapView.__init__)
    assert "setActiveFocusOnTab(False)" in init_src, (
        "MinimapView must call setActiveFocusOnTab(False) so Tab cycling skips the pane"
    )


def test_minimap_has_no_mouse_buttons():
    """Phase 0 has no mouse handling — Phase 3 wires click/drag through
    a sibling QML MouseArea (cleaner than QQuickPaintedItem mouse).
    Verify the Python class declares it accepts no buttons so we don't
    silently start receiving mouse events in Phase 0."""
    init_src = inspect.getsource(MinimapView.__init__)
    assert "setAcceptedMouseButtons(Qt.MouseButton.NoButton)" in init_src


def test_minimap_uses_transparent_backing_store():
    """The backing store fill must be transparent so the background
    colour's alpha (~20% black) composites cleanly over the
    wallpaper-blend underneath. A regression that fills the backing
    store with a solid colour would break the wallpaper-blend
    contract — same hazard NvimView and TerminalView call out."""
    init_src = inspect.getsource(MinimapView.__init__)
    assert "setFillColor(QColor(0, 0, 0, 0))" in init_src


# ---------------------------------------------------------------------------
# QML-visible properties — names, types, notify signals
# ---------------------------------------------------------------------------


def test_scroll_position_property_exists():
    """`scrollPosition` is the binding target for Main.qml's
    `MinimapView { scrollPosition: editor.scrollAnimationPosition }`.
    A missing or renamed property silently makes the binding a no-op."""
    assert hasattr(MinimapView, "scrollPosition")
    assert hasattr(MinimapView, "scrollPositionChanged")


def test_buffer_row_count_property_exists():
    """`bufferRowCount` is wired in Phase 0 (defaults to 0) so the
    Main.qml binding site exists for Phase 1's content-channel wiring.
    Phase 1 connects it to a future MinimapModel."""
    assert hasattr(MinimapView, "bufferRowCount")
    assert hasattr(MinimapView, "bufferRowCountChanged")


def test_scroll_position_setter_short_circuits_on_equality():
    """The setter must skip the notify emit when the new value equals
    the current value. Without this, NvimView emits per-frame during
    settled spring state would trigger redundant QML binding
    re-evaluations on the minimap side. Same equality-short-circuit
    pattern NvimView.backend uses."""
    src = inspect.getsource(MinimapView._set_scroll_position)
    assert "if value == self._scroll_position:" in src, (
        "MinimapView._set_scroll_position must short-circuit on equality"
    )
    assert "return" in src, "...and return without emitting the signal"


def test_nvim_view_emits_scroll_animation_position_changed():
    """The NvimView side must emit `scrollAnimationPositionChanged`
    from `_on_frame_swapped`. The signal feeds MinimapView's
    `scrollPosition` binding via Main.qml. Without the emit, the
    binding stays at its initial value forever and the minimap
    viewport indicator (Phase 3) will be silently broken."""
    from symmetria_ide.nvim_view import NvimView

    src = inspect.getsource(NvimView._on_frame_swapped)
    assert "self.scrollAnimationPositionChanged.emit()" in src, (
        "NvimView._on_frame_swapped must emit "
        "scrollAnimationPositionChanged so MinimapView.scrollPosition "
        "actually receives updates from the scroll spring"
    )


def test_nvim_view_has_scroll_animation_position_property():
    """NvimView must expose `scrollAnimationPosition` as a Q_PROPERTY.
    Main.qml's `MinimapView { scrollPosition: editor.scrollAnimationPosition }`
    binding fails silently at engine load if this property is missing."""
    from symmetria_ide.nvim_view import NvimView

    assert hasattr(NvimView, "scrollAnimationPosition")
    assert hasattr(NvimView, "scrollAnimationPositionChanged")


# ---------------------------------------------------------------------------
# Main.qml wiring — verifies the QML side picks up MinimapView correctly
# ---------------------------------------------------------------------------


def _read_main_qml() -> str:
    repo_root = Path(__file__).resolve().parent.parent
    main = repo_root / "qml" / "Main.qml"
    return main.read_text()


def test_main_qml_instantiates_minimap_view():
    """`MinimapView { id: minimap … }` must appear in Main.qml. A
    refactor that drops the declaration would silently remove the
    minimap with no other test catching it."""
    main_src = _read_main_qml()
    assert "MinimapView {" in main_src, (
        "Main.qml is missing the MinimapView declaration"
    )
    assert "id: minimap" in main_src, (
        "Main.qml's MinimapView must have `id: minimap` — referenced "
        "by NvimView's anchors.rightMargin binding"
    )


def test_main_qml_binds_scroll_position():
    """The Main.qml side must bind `scrollPosition:
    editor.scrollAnimationPosition` — without it, the scroll spring's
    updates never reach the minimap."""
    main_src = _read_main_qml()
    assert re.search(
        r"scrollPosition\s*:\s*editor\.scrollAnimationPosition", main_src
    ), "Main.qml MinimapView must bind scrollPosition: editor.scrollAnimationPosition"


def test_main_qml_minimap_visibility_matches_editor():
    """Minimap visibility MUST match NvimView's visibility gate
    (`!agentVisible && !fmVisible && editorVisible`). A regression
    that simplifies the gate could leave the minimap visible during
    agent/terminal/FM mode, which violates the Phase 0 contract."""
    main_src = _read_main_qml()
    # Find the MinimapView block — line-based extraction.
    minimap_match = re.search(r"MinimapView\s*\{(.*?)\n\s{16}\}", main_src, re.DOTALL)
    assert minimap_match is not None, "Could not locate MinimapView block"
    minimap_block = minimap_match.group(1)
    assert "!controller.agentVisible" in minimap_block
    assert "!controller.fmVisible" in minimap_block
    assert "controller.editorVisible" in minimap_block


def test_main_qml_nvim_view_reserves_minimap_width():
    """NvimView's `anchors.rightMargin` must reserve `minimap.width`
    when the minimap is visible. Without this, the grid would paint
    UNDER the minimap, which is the exact same regression class as
    gotcha #11's "viewport leak" symptom."""
    main_src = _read_main_qml()
    assert re.search(
        r"anchors\.rightMargin\s*:\s*minimap\.visible\s*\?\s*minimap\.width\s*:\s*0",
        main_src,
    ), (
        "NvimView must reserve minimap.width via anchors.rightMargin "
        "when minimap.visible — otherwise grid_resize won't account "
        "for the ribbon and the grid silently overlaps it"
    )


def test_main_qml_minimap_width_uses_theme_token():
    """The minimap's `width` must come from `Theme.size.minimapWidth`,
    not a literal — same project-standards §3.3 rule the rest of the
    chrome follows. A literal would let the value drift across
    files (Theme.qml + Main.qml + future Phase 5 cell-size code)."""
    main_src = _read_main_qml()
    minimap_match = re.search(r"MinimapView\s*\{(.*?)\n\s{16}\}", main_src, re.DOTALL)
    assert minimap_match is not None
    minimap_block = minimap_match.group(1)
    assert "Theme.size.minimapWidth" in minimap_block, (
        "Main.qml's MinimapView must bind `width: Theme.size.minimapWidth` "
        "— no literal pixel values per §3.3"
    )
