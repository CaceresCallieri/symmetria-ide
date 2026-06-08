"""Structural tests for the MinimapView QQuickPaintedItem (Phase 2).

Instantiating a `QQuickPaintedItem` subclass requires a `QGuiApplication`
(QQuickItem + QFontDatabase), which the session-scoped `qt_app` fixture
in `tests/conftest.py` only provides at `QCoreApplication` level.
Following the precedent set by `tests/test_terminal_view.py` and the
NvimView test modules, we use SOURCE-INSPECTION assertions plus a small
set of pure-function checks — they catch the same regressions a runtime
paint test would, at lower fixture cost and zero flakiness.

Disciplines pinned here (correspond 1:1 with the gotchas + project
standards referenced from `minimap_view.py`'s module docstring):

- gotcha #10  — paint() allocates no fresh QColor / QRectF; indent
                palette memoized at module load (_indent_colors tuple)
- gotcha #7   — QML registration constants + decoration intact
- §3.3        — Theme tokens are the single source of truth for chrome
                colour, drift between Theme.qml and the Python mirror
                is detected here (mirrors the ANSI palette test pattern)
- focus rule  — minimap is not a focus target (ItemIsFocusScope is NOT
                set; setActiveFocusOnTab(False) keeps Tab cycling away)
- model wiring — MinimapView.model setter connects linesChanged → update()
                following the NvimView.backend / TerminalView.backend pattern
- PRD §6 R2.2 — paint() reads cached indent_level(), never str.lstrip()
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

from PySide6.QtGui import QColor

from symmetria_ide.minimap_view import (
    _BACKGROUND_RGBA,
    _INDENT_RGBA,
    _INDENT_STEP_PX,
    _MIN_ROW_HEIGHT_PX,
    _background_color,
    _indent_colors,
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


# NOTE: the former NvimView scroll-spring tests (scrollAnimationPosition
# property + its per-frame emit) were removed when the custom grid renderer
# was deleted — nvim now renders in the terminal and the minimap's viewport
# indicator is driven by the `minimap_viewport` rpcnotify channel instead of
# a pixel scroll spring.


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
    """The scroll spring is gone (nvim renders in the terminal), so the
    minimap's `scrollPosition` is pinned to 0; its viewport indicator is
    driven by the `minimap_viewport` rpcnotify channel via the model. The
    binding must NOT reference the deleted `editor.scrollAnimationPosition`."""
    main_src = _read_main_qml()
    assert re.search(r"scrollPosition\s*:\s*0", main_src), (
        "Main.qml MinimapView must bind scrollPosition: 0 (spring removed)"
    )
    assert "scrollAnimationPosition" not in main_src, (
        "Main.qml must not reference the deleted scrollAnimationPosition"
    )


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


def test_main_qml_editor_reserves_minimap_width():
    """The editor TerminalView's `anchors.rightMargin` must reserve
    `minimap.width` when the minimap is visible. Without this, the
    editor would paint UNDER the minimap — same regression class as
    gotcha #11's "viewport leak" symptom."""
    main_src = _read_main_qml()
    assert re.search(
        r"anchors\.rightMargin\s*:\s*minimap\.visible\s*\?\s*minimap\.width\s*:\s*0",
        main_src,
    ), (
        "editor TerminalView must reserve minimap.width via anchors.rightMargin "
        "when minimap.visible — otherwise the editor silently overlaps it"
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


# ---------------------------------------------------------------------------
# Phase 2 — block-mode painter, indent palette + memoization, model wiring
# ---------------------------------------------------------------------------


def test_indent_rgba_has_four_levels():
    """The four-rung indent scale is the canonical contract — Theme.qml
    defines level0..level3 and the painter indexes the tuple directly
    with `_indent_colors[level]`. Diverging here would either
    out-of-bounds the index (palette shrank) or leave a dead colour
    (palette grew without a matching Theme.qml change)."""
    assert len(_INDENT_RGBA) == 4
    for rgb in _INDENT_RGBA:
        assert len(rgb) == 3, "indent palette entries are 3-tuple RGB"
        for channel in rgb:
            assert 0 <= channel <= 255


def test_indent_colors_memoized_at_module_load():
    """Per gotcha #10 — the indent QColors must exist at module import
    time so paint() never allocates fresh ones per row. A regression
    that lazily constructs them inside paint() would blow the GC budget
    immediately on any non-tiny buffer."""
    assert len(_indent_colors) == 4
    for color in _indent_colors:
        assert isinstance(color, QColor)


def test_indent_palette_matches_theme_qml():
    """Drift detection: every hex in `_INDENT_RGBA` MUST appear inside
    Theme.qml's `Theme.color.minimap.indent` block. Same dual-source-of-truth
    pattern `test_background_matches_theme_qml` uses — until Theme is
    piped through Python via a context property, this is how we keep
    the two sides aligned.
    """
    theme_src = _read_theme_qml()
    for level, rgb in enumerate(_INDENT_RGBA):
        r, g, b = rgb
        expected_hex = f"#{r:02X}{g:02X}{b:02X}"
        assert expected_hex.upper() in theme_src.upper(), (
            f"Theme.qml is missing the hex value {expected_hex} for "
            f"`Theme.color.minimap.indent.level{level}` — either the Theme "
            "token was nudged out of sync, or _INDENT_RGBA drifted away"
        )
    # The QtObject block must exist — protects against a refactor that
    # renames the token nesting (e.g. `indent` → `depth`) but leaves
    # the hex values incidentally appearing elsewhere.
    assert re.search(r"indent\s*:\s*QtObject\s*\{", theme_src), (
        "Theme.qml is missing the `indent: QtObject {` block under "
        "`Theme.color.minimap` — the level tokens have no home"
    )


def test_layout_constants_within_sane_bounds():
    """The min-row-height floor and indent-step affect every paint;
    pin them so a future tune doesn't silently land outside the
    legibility range documented in the module header."""
    # _MIN_ROW_HEIGHT_PX below 2 makes the silhouette mush; above 4
    # defeats the "zoom out" purpose.
    assert 2.0 <= _MIN_ROW_HEIGHT_PX <= 4.0
    # _INDENT_STEP_PX below 3 makes deep indents indistinguishable;
    # above 6 leaves too little room for the bar on an 80-px minimap.
    assert 3.0 <= _INDENT_STEP_PX <= 6.0


def test_paint_indent_palette_lookup_does_not_construct_qcolor():
    """The Phase 2 paint loop must index `_indent_colors[level]` — a
    fresh `QColor(...)` per row would defeat gotcha #10 just as much
    as the background fill would."""
    src = inspect.getsource(MinimapView.paint)
    # Already covered by test_paint_does_not_construct_qcolor above,
    # but pin the positive assertion too — the tuple lookup must
    # actually be there.
    assert "_indent_colors[" in src, (
        "paint() must index _indent_colors[level] — without it, the "
        "indent palette is unused and every row paints in default"
    )


def test_paint_reads_indent_level_from_model_not_lstrip():
    """PRD §6 R2.2 — paint() MUST read pre-cached indent levels via
    `model.indent_level(i)`. A regression that calls `str.lstrip()`
    or `str.startswith()` per row inside paint() would allocate a
    fresh str per row — at 50k+ lines that's a gotcha #10 disaster.

    Strip the docstring before inspecting so the assertion sees only
    executable code — otherwise comments / module-header references
    to `.lstrip()` (e.g. "PRD §6 R2.2 says no str.lstrip per row")
    trip the substring check.
    """
    src = inspect.getsource(MinimapView.paint)
    # Strip the function's docstring — paint() opens with a triple-quoted
    # docstring, so split on the closing `"""` of that docstring and
    # examine only the body.
    body = src.split('"""', 2)[-1] if src.count('"""') >= 2 else src
    assert "indent_level" in body, (
        "paint() must call model.indent_level(i) — the cached array "
        "is the only str-allocation-free way to know indent depth"
    )
    assert ".lstrip(" not in body, (
        "paint() must NOT call str.lstrip — that allocates a fresh "
        "str per row, blowing the gotcha #10 budget on large buffers"
    )
    assert ".startswith(" not in body, (
        "paint() must NOT call str.startswith inside the loop — "
        "use the cached indent_level array instead"
    )


def test_paint_pool_stayed_at_one_rect():
    """Phase 2's painter reuses Phase 0's single _paint_rect for the
    background fill AND every per-line block. The pool size MUST NOT
    have grown — if Phase 3+ legitimately needs another rect (e.g.
    for the viewport indicator overlaid on top of blocks), the test
    needs to be updated AND the new rect must be allocated in __init__,
    never inside paint."""
    init_src = inspect.getsource(MinimapView.__init__)
    # Count QRectF constructions in __init__. Should be exactly one.
    n = init_src.count("QRectF()")
    assert n == 1, (
        f"MinimapView.__init__ allocates {n} QRectFs — Phase 2 contract "
        "requires exactly one (`_paint_rect`); adding more without "
        "updating this test is a hygiene regression waiting to happen"
    )


def test_model_property_uses_setter_pattern():
    """The MinimapView.model setter must (a) disconnect from any prior
    model's linesChanged, (b) connect the new model's linesChanged →
    _on_lines_changed, (c) emit modelChanged, (d) trigger update().
    Mirrors NvimView.backend / TerminalView.backend lifecycle."""
    setter_src = inspect.getsource(MinimapView._set_model)
    assert "linesChanged.disconnect" in setter_src, (
        "setter must disconnect prior model's linesChanged signal"
    )
    assert "linesChanged.connect" in setter_src, (
        "setter must connect new model's linesChanged → _on_lines_changed"
    )
    assert "modelChanged.emit" in setter_src, (
        "setter must emit modelChanged for QML binding"
    )
    assert "self.update()" in setter_src, (
        "setter must trigger an immediate repaint when a model arrives"
    )


def test_main_qml_binds_model_property():
    """Main.qml MinimapView must include `model: minimapModel` —
    without it the painter never gets a content reference and stays
    in the Phase 0 background-only fallback path."""
    main_src = _read_main_qml()
    minimap_match = re.search(r"MinimapView\s*\{(.*?)\n\s{16}\}", main_src, re.DOTALL)
    assert minimap_match is not None
    minimap_block = minimap_match.group(1)
    assert re.search(r"model\s*:\s*minimapModel", minimap_block), (
        "Main.qml's MinimapView must bind `model: minimapModel` — "
        "Phase 2 painter reads indent_level / line_count via the model"
    )


# ---------------------------------------------------------------------------
# Phase 3 — viewport indicator painter + Theme drift
# ---------------------------------------------------------------------------


def test_viewport_palette_constants_present():
    """The viewport indicator palette must exist as module-level
    constants AND memoized QColors, parallel to the indent palette."""
    from symmetria_ide.minimap_view import (
        _VIEWPORT_FILL_RGBA,
        _VIEWPORT_FRAME_RGBA,
        _viewport_fill_color,
        _viewport_frame_color,
    )

    assert len(_VIEWPORT_FILL_RGBA) == 4
    assert len(_VIEWPORT_FRAME_RGBA) == 4
    assert isinstance(_viewport_fill_color, QColor)
    assert isinstance(_viewport_frame_color, QColor)
    # Alpha channels must survive — fill is ~10%, frame ~40%.
    assert _viewport_fill_color.alpha() == _VIEWPORT_FILL_RGBA[3]
    assert _viewport_frame_color.alpha() == _VIEWPORT_FRAME_RGBA[3]


def test_viewport_palette_matches_theme_qml():
    """Drift detection: viewport hex values in `_VIEWPORT_*_RGBA` MUST
    appear inside Theme.qml's `Theme.color.minimap.viewportFill /
    viewportFrame` declarations. Same dual-source-of-truth pattern as
    the indent palette test."""
    from symmetria_ide.minimap_view import (
        _VIEWPORT_FILL_RGBA,
        _VIEWPORT_FRAME_RGBA,
    )

    theme_src = _read_theme_qml()
    for label, rgba in [
        ("viewportFill", _VIEWPORT_FILL_RGBA),
        ("viewportFrame", _VIEWPORT_FRAME_RGBA),
    ]:
        r, g, b, a = rgba
        expected_hex = f"#{a:02X}{r:02X}{g:02X}{b:02X}"
        assert expected_hex.upper() in theme_src.upper(), (
            f"Theme.qml is missing the hex {expected_hex} for {label} — "
            "drift between Python mirror and the Theme token"
        )


def test_paint_draws_viewport_when_count_positive():
    """Source-inspect the paint() body for the viewport drawing
    sequence: read viewport_count, skip if <= 0, otherwise read
    viewport_first and emit 3 fill_rect calls (fill + top + bottom)."""
    src = inspect.getsource(MinimapView.paint)
    body = src.split('"""', 2)[-1] if src.count('"""') >= 2 else src
    assert "viewport_count" in body, (
        "paint() must read model.viewport_count() to know whether to draw the indicator"
    )
    assert "viewport_first" in body, (
        "paint() must read model.viewport_first() to position the indicator"
    )
    assert "_viewport_fill_color" in body, (
        "paint() must paint the fill overlay using the memoized _viewport_fill_color"
    )
    assert "_viewport_frame_color" in body, (
        "paint() must paint the top/bottom hairlines using the memoized "
        "_viewport_frame_color"
    )


def test_paint_skips_viewport_when_count_zero():
    """The painter must SKIP the viewport pass when viewport_count <= 0
    (terminal-first startup before any apply_viewport). The guard may be
    an early-return OR a conditional block — the gutter step (Phase 4)
    must remain reachable regardless, so the conditional-block form is
    preferred."""
    src = inspect.getsource(MinimapView.paint)
    # Guard must be present — either inverted ("if count > 0:") or direct
    # ("if count <= 0: return / skip").
    assert (
        "viewport_count > 0" in src
        or "viewport_count <= 0" in src
        or "viewport_count == 0" in src
    ), "paint() must guard the viewport draw step on viewport_count > 0"
    # The gutter pass must NOT be nested inside the viewport guard — it
    # must run independently so diagnostics/git display at terminal-first
    # startup before apply_viewport has fired.
    # Verify by checking that "diagnostic_count" appears AFTER "viewport_count"
    # in the source but is NOT indented inside the viewport conditional.
    assert src.index("diag_count = self._model.diagnostic_count()") > src.index(
        "viewport_count = self._model.viewport_count()"
    ), "gutter pass must appear after the viewport block in paint()"
    # The gutter block must be reachable without the viewport guard having
    # a path to skip it — the "diag_count == 0" check is the ONLY gutter
    # early-exit that's acceptable.
    gutter_start = src.index("diag_count = self._model.diagnostic_count()")
    viewport_block = src[
        src.index("viewport_count = self._model.viewport_count()") : gutter_start
    ]
    # The viewport block must NOT contain a bare "return" that would skip
    # the gutter entirely — any return must be INSIDE a "if viewport_count > 0:"
    # or similar conditional sub-block, not at the top level of paint().
    # Structural check: if "return" appears before the gutter, it must be
    # after an "if" that bounds it (i.e. the line count before gutter_start
    # that has a top-level "        return" must be 0).
    for line in viewport_block.split("\n"):
        # A top-level return in paint() has 8 spaces of indent (2 levels: class + def)
        assert line != "        return", (
            "paint() must not have a top-level early-return between the "
            "viewport block and the gutter pass — use a conditional block "
            "so the gutter renders even when viewport_count == 0"
        )


def test_paint_pool_still_one_rect_after_phase_3():
    """Phase 3 reuses the same single _paint_rect for the viewport
    fill + both frame hairlines (3 fill_rect calls, all mutating the
    same pool entry). The pool MUST NOT have grown."""
    init_src = inspect.getsource(MinimapView.__init__)
    n = init_src.count("QRectF()")
    assert n == 1, (
        f"MinimapView.__init__ allocates {n} QRectFs — Phase 3 contract "
        "preserves the single-pool discipline; viewport drawing reuses "
        "the same `_paint_rect` mutated via setRect()"
    )


def test_model_setter_also_wires_viewport_changed():
    """The MinimapView.model setter must connect viewportChanged on the
    new model so the painter repaints when bounds shift. Missing this
    connection leaves the indicator frozen at its initial position."""
    setter_src = inspect.getsource(MinimapView._set_model)
    assert "viewportChanged.connect" in setter_src
    assert "viewportChanged.disconnect" in setter_src, (
        "disconnect on handover for symmetry with linesChanged"
    )


def test_main_qml_has_scrubber_mouse_area():
    """Main.qml's MinimapView block must include a MouseArea sibling
    routing onPressed / onPositionChanged / onReleased to
    controller.seek_to_row via a throttled Timer."""
    main_src = _read_main_qml()
    minimap_match = re.search(r"MinimapView\s*\{(.*?)\n\s{16}\}", main_src, re.DOTALL)
    assert minimap_match is not None
    block = minimap_match.group(1)
    assert "MouseArea" in block, (
        "Main.qml MinimapView must include a MouseArea scrubber"
    )
    assert "controller.seek_to_row" in block, (
        "Scrubber must call controller.seek_to_row(...)"
    )
    assert "Timer" in block, "Scrubber must throttle via a Timer (PRD §7.3 R3.2)"


# ---------------------------------------------------------------------------
# Phase 4 — gutter painter + diagnostic/git Theme drift
# ---------------------------------------------------------------------------


def test_diagnostic_palette_constants_present():
    """4-rung diagnostic palette + memoized QColors + key→index map."""
    from symmetria_ide.minimap_view import (
        _DIAGNOSTIC_RGBA,
        _DIAGNOSTIC_KEY_TO_COLOR_INDEX,
        _diagnostic_colors,
    )

    assert len(_DIAGNOSTIC_RGBA) == 4
    assert len(_diagnostic_colors) == 4
    for c in _diagnostic_colors:
        assert isinstance(c, QColor)
    assert _DIAGNOSTIC_KEY_TO_COLOR_INDEX == {
        "error": 0,
        "warn": 1,
        "info": 2,
        "hint": 3,
    }


def test_gitdiff_palette_constants_present():
    """3-rung git-diff palette + memoized QColors + key→index map."""
    from symmetria_ide.minimap_view import (
        _GITDIFF_RGBA,
        _GITDIFF_KEY_TO_COLOR_INDEX,
        _gitdiff_colors,
    )

    assert len(_GITDIFF_RGBA) == 3
    assert len(_gitdiff_colors) == 3
    for c in _gitdiff_colors:
        assert isinstance(c, QColor)
    assert _GITDIFF_KEY_TO_COLOR_INDEX == {
        "added": 0,
        "modified": 1,
        "deleted": 2,
    }


def test_gutter_width_constant():
    """4 px is the floor that's perceptible as a coloured stripe at
    minimap scale. Pin the value."""
    from symmetria_ide.minimap_view import _GUTTER_WIDTH_PX

    assert _GUTTER_WIDTH_PX == 4.0


def test_diagnostic_palette_matches_theme_qml():
    """Drift detection: every hex in _DIAGNOSTIC_RGBA MUST appear in
    Theme.qml (either directly as the minimap.diagnostic.* aliases'
    target tokens, since those alias mode.*/text.* values)."""
    from symmetria_ide.minimap_view import _DIAGNOSTIC_RGBA

    theme_src = _read_theme_qml()
    for level, rgb in enumerate(_DIAGNOSTIC_RGBA):
        r, g, b = rgb
        expected = f"#{r:02X}{g:02X}{b:02X}"
        assert expected.upper() in theme_src.upper(), (
            f"Theme.qml missing hex {expected} for diagnostic level {level} — "
            "alias target drifted"
        )


def test_gitdiff_palette_matches_theme_qml():
    """Same drift detection for git-diff palette."""
    from symmetria_ide.minimap_view import _GITDIFF_RGBA

    theme_src = _read_theme_qml()
    for level, rgb in enumerate(_GITDIFF_RGBA):
        r, g, b = rgb
        expected = f"#{r:02X}{g:02X}{b:02X}"
        assert expected.upper() in theme_src.upper(), (
            f"Theme.qml missing hex {expected} for gitDiff level {level}"
        )


def test_theme_qml_has_diagnostic_and_gitdiff_blocks():
    """The QtObject sub-blocks must exist under minimap — protects
    against renames that leave the hex coincidentally elsewhere."""
    theme_src = _read_theme_qml()
    assert re.search(r"diagnostic\s*:\s*QtObject\s*\{", theme_src), (
        "Theme.qml missing diagnostic: QtObject { block under minimap"
    )
    assert re.search(r"gitDiff\s*:\s*QtObject\s*\{", theme_src), (
        "Theme.qml missing gitDiff: QtObject { block under minimap"
    )


def test_paint_reads_diagnostic_and_git_accessors():
    """Source-inspect paint() for the Phase 4 gutter pass — it must
    read both model.diagnostic_at and model.git_at and use the
    memoized palette tuples."""
    src = inspect.getsource(MinimapView.paint)
    assert "diagnostic_at" in src, (
        "paint() must call model.diagnostic_at for the gutter dot"
    )
    assert "git_at" in src, "paint() must call model.git_at for the gutter bar"
    assert "_diagnostic_colors[" in src
    assert "_gitdiff_colors[" in src


def test_paint_skips_gutter_when_both_counts_zero():
    """If diagnostic_count() and git_count() are both zero, the gutter
    pass should early-return — paint zero gutter rects rather than
    iterate the loop."""
    src = inspect.getsource(MinimapView.paint)
    assert "diagnostic_count" in src
    assert "git_count" in src
    # The early-return after the both-zero check.
    assert "diag_count == 0 and git_count == 0" in src, (
        "paint() must early-return on both-zero gutter state"
    )


def test_silhouette_inset_by_gutter_width():
    """The block-mode painter MUST inset its x_offset by _GUTTER_WIDTH_PX
    so the silhouette doesn't overlap the gutter column."""
    src = inspect.getsource(MinimapView.paint)
    assert "gutter_inset" in src or "_GUTTER_WIDTH_PX +" in src, (
        "paint() must inset the silhouette by _GUTTER_WIDTH_PX so the "
        "gutter column has room to draw"
    )


def test_paint_pool_still_one_rect_after_phase_4():
    """Phase 4's gutter pass reuses the same single _paint_rect."""
    init_src = inspect.getsource(MinimapView.__init__)
    n = init_src.count("QRectF()")
    assert n == 1, (
        f"MinimapView.__init__ allocates {n} QRectFs — Phase 4 must "
        "preserve the single-pool discipline (gutter draws reuse _paint_rect)"
    )


def test_model_setter_wires_diagnostics_and_git_signals():
    """The Phase 4 setter additions: diagnosticsChanged + gitChanged
    must be connected on attach and disconnected on detach."""
    setter_src = inspect.getsource(MinimapView._set_model)
    assert "diagnosticsChanged.connect" in setter_src
    assert "diagnosticsChanged.disconnect" in setter_src
    assert "gitChanged.connect" in setter_src
    assert "gitChanged.disconnect" in setter_src


def test_on_diagnostics_changed_has_slot_decorator():
    """§4 P1 — signal receivers go through Qt's metaobject dispatch."""
    src = inspect.getsource(MinimapView._on_diagnostics_changed)
    # @Slot() decoration is detected via the line just above the def.
    module_src = inspect.getsource(inspect.getmodule(MinimapView))
    assert "@Slot()\n    def _on_diagnostics_changed" in module_src
    assert "@Slot()\n    def _on_git_changed" in module_src
    assert "self.update()" in src


def test_gutter_renders_independently_of_viewport_state():
    """Phase 4 structural regression: the gutter pass (Step 6) in paint()
    must NOT be nested inside the Step 5 viewport conditional block.
    A viewport_count == 0 guard that uses an early return would silently
    suppress all diagnostic + git gutter markers at terminal-first startup
    (before any apply_viewport fires).

    Verified by confirming there is no bare top-level `return` between the
    viewport_count read and the diag_count read in paint() source — any
    returns that skip the viewport drawing must be inside a conditional."""
    src = inspect.getsource(MinimapView.paint)
    # Locate the viewport block and the gutter block.
    vp_idx = src.index("viewport_count = self._model.viewport_count()")
    gutter_idx = src.index("diag_count = self._model.diagnostic_count()")
    assert gutter_idx > vp_idx, "gutter block must come after viewport block in paint()"
    between = src[vp_idx:gutter_idx]
    # No top-level (8-space-indent) return should appear between the two blocks.
    for line in between.split("\n"):
        assert line != "        return", (
            "A top-level `return` between the viewport block and the gutter pass "
            "causes the gutter to be silently suppressed when viewport_count == 0. "
            "Use a conditional `if viewport_count > 0:` block instead of an "
            "early-return so both features operate independently."
        )


# ---------------------------------------------------------------------------
# Phase 4.5 — neutral gray palette + content-length-aware width clipping
# ---------------------------------------------------------------------------


def test_indent_palette_is_neutral_gray():
    """Phase 4.5 shifted from warm-amber to neutral gray. The brightest
    rung must equal Theme.text.emphasis (#E8E8E8); the dimmest rung (#5A5A5A) steps slightly
    below text.dim (#7A7A7A) intentionally — it remains perceptible
    against the semi-transparent minimap background.
    """
    from symmetria_ide.minimap_view import _INDENT_RGBA

    # Level 0 = brightest.
    assert _INDENT_RGBA[0] == (0xE8, 0xE8, 0xE8)
    # Monotonically decreasing brightness (R == G == B at every level,
    # gray ramp — no warm/cool bias).
    for r, g, b in _INDENT_RGBA:
        assert r == g == b, (
            f"_INDENT_RGBA entry ({r:#x},{g:#x},{b:#x}) is NOT neutral gray — "
            "Phase 4.5 requires R == G == B"
        )
    # Strictly decreasing from level 0 to level 3.
    grays = [rgb[0] for rgb in _INDENT_RGBA]
    assert grays == sorted(grays, reverse=True), (
        "Indent palette must be brightest → dimmest from level 0 to 3"
    )


def test_char_width_px_constant_in_sensible_range():
    """Phase 4.5 introduces _CHAR_WIDTH_PX as the chars→pixels ratio
    for content-length-aware block widths. 0.5 to 1.0 covers typical
    code line widths within the 80-px minimap budget."""
    from symmetria_ide.minimap_view import _CHAR_WIDTH_PX

    assert 0.4 <= _CHAR_WIDTH_PX <= 1.0


def test_paint_reads_content_length_and_clips_block_width():
    """Paint() MUST call model.content_length and multiply by
    _CHAR_WIDTH_PX (not draw a full-width block per line). Without
    this clip the silhouette returns to the Phase 4.0 wall-of-bars
    look — a visible regression."""
    src = inspect.getsource(MinimapView.paint)
    body = src.split('"""', 2)[-1] if src.count('"""') >= 2 else src
    assert "content_length" in body, (
        "paint() must read model.content_length() to clip per-row block width"
    )
    assert "_CHAR_WIDTH_PX" in body or "char_width" in body, (
        "paint() must scale content_length by _CHAR_WIDTH_PX for the clip"
    )


def test_paint_skips_zero_content_rows():
    """A row with content_length == 0 (blank / pure-whitespace) MUST
    be skipped — no fillRect — so blanks render as gaps in the
    silhouette rather than full-width bars."""
    src = inspect.getsource(MinimapView.paint)
    # The early-continue guard must be present in the loop body.
    assert "content_len <= 0" in src or "content_len == 0" in src, (
        "paint() must skip rows with content_length <= 0 (blank lines "
        "should render as gaps, not full-width bars)"
    )
