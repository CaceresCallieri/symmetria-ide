"""Structural tests for the rounded canvas corners (2026-08-13).

The corners are painted OVER the central surfaces rather than clipped out of
them, because neither QMLTermWidget nor the nested Wayland compositor can be
given a radius (see `qml/CanvasCorners.qml`). That implementation choice makes
two things fragile in ways a screenshot review would not catch:

  * the overlay must out-rank every sibling in `mainContent` on `z`. Add a pane
    with a higher `z` and the wedges disappear behind it — on that surface
    only, so the corners look rounded until you switch to the new one;
  * the arc is a cubic Bézier whose handle length is the quarter-circle
    constant. A rounder or flatter constant still draws a plausible corner, so
    the failure is a curve that no longer matches a `Rectangle { radius }` of
    the same size beside it — invisible on its own, wrong next to Hyprland's.

It also pins the corollary the corners forced: NO straight line may cross one.
That retired the full-width hairline under AgentTopBar and over StatusBar, and
inset the canvas/sidebar separator by the radius. Each looks like an omission
to a future reader adding "definition" back to a boundary.

None of it is expressible as a comment somebody must remember to read.
"""

from __future__ import annotations

import re
from pathlib import Path

_QML = Path(__file__).resolve().parent.parent / "qml"


def _without_comments(source: str) -> str:
    """Blank out `//` comments, keeping every line and column position.

    Not cosmetic. `Main.qml` documents a retired overlay as "Was a Window-root
    Loader at z:100", and a raw scan reads that prose as a live declaration —
    which failed the z test on its first run against code that was correct.
    Every scan below therefore runs on stripped source, and the brace counter
    needs it too: a `{` inside a comment would throw the depth off. Replacing
    with spaces rather than deleting keeps offsets aligned with the real file.

    ⚠ Known limitation: `//` is blanked unconditionally, INCLUDING inside a
    string literal. No QML file here contains a `://` today, so this is
    latent; if one gains a url, the rest of that line disappears and the brace
    counter can go out of balance. `_main_content_span` names this in its
    failure message rather than guessing, because a lookbehind that skips
    `://` would still miss the general case (any `//` in any string).
    """
    return re.sub(r"//[^\n]*", lambda m: " " * len(m.group(0)), source)


_MAIN = _without_comments((_QML / "Main.qml").read_text(encoding="utf-8"))
# Raw and stripped. Every scan for CODE must use the stripped copy: the
# component carries a ~45-line comment header that quotes its own code, so a
# raw search can match the explanation instead of the thing explained — the
# same trap `_without_comments` exists for. The raw copy is only for the
# pragma check, which is positional.
_CORNERS = (_QML / "CanvasCorners.qml").read_text(encoding="utf-8")
_CORNERS_CODE = _without_comments(_CORNERS)


def _main_content_span() -> tuple[int, int]:
    """Return the `[start, end)` offsets of `Item { id: mainContent ... }`.

    Offsets rather than the substring, because one test needs what comes AFTER
    the block (the sidebar separator is its sibling). Brace-counted rather than
    regex-matched: the block is ~1100 lines and holds every central surface, so
    any non-counting extraction stops at the first nested closing brace.
    """
    marker = _MAIN.index("id: mainContent")
    start = _MAIN.rindex("Item {", 0, marker)
    depth = 0
    for i in range(start, len(_MAIN)):
        if _MAIN[i] == "{":
            depth += 1
        elif _MAIN[i] == "}":
            depth -= 1
            if depth == 0:
                return start, i + 1
    raise AssertionError(
        "mainContent block is unbalanced — the likely cause is a `//` inside a "
        "string literal (a url) that `_without_comments` blanked along with the "
        "rest of its line; see that function's known limitation"
    )


def _main_content_block() -> str:
    start, end = _main_content_span()
    return _MAIN[start:end]


def test_overlay_is_mounted_inside_main_content() -> None:
    """Mounted on the central surface, not at the Window root.

    At the Window root it would round the WINDOW — whose corners Hyprland
    already rounds — and leave the canvas square, which is the thing being
    fixed.
    """
    assert "CanvasCorners {" in _main_content_block()


def test_overlay_outranks_every_sibling_z() -> None:
    block = _main_content_block()
    overlay = re.search(r"CanvasCorners \{[^}]*?\bz:\s*(\d+)", block, re.S)
    assert overlay is not None, "the CanvasCorners overlay declares no z"
    overlay_z = int(overlay.group(1))

    # Every `z:` in mainContent EXCEPT the overlay's own — the whole block, not
    # just what precedes the mount. Scanning only the prefix would miss a pane
    # appended after it, which is the natural place to add one and therefore
    # the regression this test exists for.
    #
    # A nested item's z orders it only among ITS siblings, so including the
    # nested ones over-approximates. That is deliberate and cheap: the two here
    # (the minimap's 10, and WhichKeyOverlay's 20 inside the editor) are both
    # below 50 anyway, and a nested z climbing past this overlay means the pane
    # is being restructured — worth a look either way.
    span = overlay.span()
    others = [
        int(m.group(1))
        for m in re.finditer(r"\bz:\s*(\d+)", block)
        if not (span[0] <= m.start() < span[1])
    ]
    # `default` rather than an `assert others`: the two z declarations are both
    # removable by a legitimate refactor (the minimap is already gated off), and
    # a missing precondition should not read as a failure of this contract.
    highest = max(others, default=-1)
    assert overlay_z > highest, (
        f"CanvasCorners z={overlay_z} does not out-rank every sibling "
        f"(highest is {highest}); the wedges will hide behind that pane"
    )


def test_overlay_binds_theme_tokens_not_literals() -> None:
    # `[^{}]*` rather than a `\n {16}\}` terminator: the mount body holds no
    # nested braces, and pinning the indentation would turn a re-indent — or
    # wrapping the mount in a Loader — into a failure whose message points at
    # this test instead of at the change.
    entry = re.search(r"CanvasCorners \{[^{}]*\}", _main_content_block(), re.S)
    assert entry is not None, "could not isolate the CanvasCorners entry"
    body = entry.group(0)
    assert "cornerRadius: Theme.radius.canvas" in body
    # `bg.bar` is the rung of the two chrome bars the canvas sits between, and
    # since 2026-08-13 of the side panel too — so the wedge colour is exact at
    # all four corners. See the comment at the mount site.
    assert "cornerColor: Theme.color.bg.bar" in body


def test_four_wedges_one_rotated_shape() -> None:
    assert re.search(r"Repeater\s*\{\s*model:\s*4", _CORNERS_CODE) is not None
    assert "rotation: wedge.index * 90" in _CORNERS_CODE


def test_arc_uses_the_quarter_circle_handle() -> None:
    """4/3 * (sqrt(2) - 1), the constant Qt's own rounded rects use."""
    match = re.search(r"handle:\s*([0-9.]+)", _CORNERS_CODE)
    assert match is not None, "the Bézier handle constant is gone"
    expected = 4.0 / 3.0 * (2.0**0.5 - 1.0)
    assert abs(float(match.group(1)) - expected) < 1e-9


def test_overlay_is_inert() -> None:
    """`enabled: false` — the overlay must never take input.

    It covers the corners of EVERY central surface, one of which is the nested
    Wayland compositor hosting real Chrome. Pointer delivery into that surface
    already depends on three non-default things (hover acceptance, wheel
    re-quantisation, a manual `wl_pointer.frame` — see CLAUDE.md), so an
    overlay that accepted hover on top of it would break Chrome's pointer
    handling in a way that reads as a compositor bug, not as a corner.
    """
    assert "enabled: false" in _CORNERS_CODE


def test_no_full_width_hairline_brackets_the_content() -> None:
    """The bars carry no 1px divider along the edge they share with the canvas.

    A full-width line cuts across both of the arcs it passes, and strikes the
    side panel as a hard tick exactly where the bar should just become the
    panel. The surface ladder's lightness step already marks that boundary, so
    the line was a second answer to an answered question. "Add a divider back
    for definition" is the instinct this guards against.
    """
    for name in ("AgentTopBar.qml", "StatusBar.qml"):
        source = _without_comments((_QML / name).read_text(encoding="utf-8"))
        # Any `height: 1`, not the removed shape specifically. The first cut of
        # this test matched `width: root.width` AND `height: 1` together, on the
        # reasoning that a narrow pattern leaves non-full-width 1px accents
        # legal — but neither file contains `root.width` any more, so the
        # assertion could not fire for ANY reason. It passed by being
        # unfalsifiable, which is the same defect as an assertion that matches
        # its own comment. Both files now hold zero `height: 1`, so the blunt
        # form is enforceable and cannot go quietly true.
        offenders = re.findall(r"\bheight:\s*1\b", source)
        assert not offenders, (
            f"{name} grew a 1px divider. If it is genuinely NOT full-width "
            f"(an accent under one control), write it as `implicitHeight: 1` "
            f"to say so deliberately; a full-width one cuts the canvas's "
            f"rounded corners and must not come back."
        )


def test_sidebar_separator_stops_at_the_corner() -> None:
    """The canvas/sidebar rule is inset by the corner radius at both ends.

    It is the brightest line on that seam (`outlineVariant`, above both
    surfaces it divides), so at full height it runs past the point where the
    canvas has curved away and stands alone beside the corner wedge.
    """
    # The separator is the sibling AFTER mainContent, so search past the block
    # rather than inside it.
    tail = _MAIN[_main_content_span()[1] :]
    separator = re.search(r"Rectangle \{[^}]*?implicitWidth:\s*1\b[^}]*?\}", tail, re.S)
    assert separator is not None, "could not find the 1px sidebar separator"
    body = separator.group(0)
    assert "Layout.topMargin: Theme.radius.canvas" in body
    assert "Layout.bottomMargin: Theme.radius.canvas" in body


def test_delegate_context_is_bound() -> None:
    """`ComponentBehavior: Bound` is what lets the delegate read the outer id.

    Dropping it does not break rendering — it re-introduces five `unqualified`
    findings that the lint baseline would then absorb as new debt.
    """
    assert _CORNERS.lstrip().startswith("pragma ComponentBehavior: Bound")
