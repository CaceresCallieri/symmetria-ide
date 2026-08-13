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
    """
    return re.sub(r"//[^\n]*", lambda m: " " * len(m.group(0)), source)


_MAIN = _without_comments((_QML / "Main.qml").read_text(encoding="utf-8"))
_CORNERS = (_QML / "CanvasCorners.qml").read_text(encoding="utf-8")


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
    raise AssertionError("mainContent block is unbalanced")


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
    corners_at = block.index("CanvasCorners {")
    # `z:` values declared anywhere in mainContent, including inside the panes.
    # A nested item's z only orders it among ITS siblings, so comparing against
    # all of them is stricter than needed — deliberately, because the cheap
    # over-approximation costs nothing and a nested z that climbs above this
    # overlay is a sign the pane is being restructured anyway.
    others = [int(m.group(1)) for m in re.finditer(r"\bz:\s*(\d+)", block[:corners_at])]
    overlay = re.search(r"CanvasCorners \{[^}]*?\bz:\s*(\d+)", block, re.S)
    assert overlay is not None, "the CanvasCorners overlay declares no z"
    overlay_z = int(overlay.group(1))
    assert others, "expected sibling z values in mainContent"
    assert overlay_z > max(others), (
        f"CanvasCorners z={overlay_z} does not out-rank every sibling "
        f"(highest is {max(others)}); the wedges will hide behind that pane"
    )


def test_overlay_binds_theme_tokens_not_literals() -> None:
    entry = re.search(r"CanvasCorners \{.*?\n {16}\}", _main_content_block(), re.S)
    assert entry is not None, "could not isolate the CanvasCorners entry"
    body = entry.group(0)
    assert "cornerRadius: Theme.radius.canvas" in body
    # `bg.bar` is the rung of the two chrome bars the canvas sits between —
    # see the comment at the mount site for why the right-hand corners are
    # knowingly one rung off against the side panel.
    assert "cornerColor: Theme.color.bg.bar" in body


def test_four_wedges_one_rotated_shape() -> None:
    assert re.search(r"Repeater\s*\{\s*model:\s*4", _CORNERS) is not None
    assert "rotation: wedge.index * 90" in _CORNERS


def test_arc_uses_the_quarter_circle_handle() -> None:
    """4/3 * (sqrt(2) - 1), the constant Qt's own rounded rects use."""
    match = re.search(r"handle:\s*([0-9.]+)", _CORNERS)
    assert match is not None, "the Bézier handle constant is gone"
    expected = 4.0 / 3.0 * (2.0**0.5 - 1.0)
    assert abs(float(match.group(1)) - expected) < 1e-9


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
        # The removed pair was `width: root.width` + `height: 1`. Matching that
        # exact shape keeps the assertion narrow: 1px accents that are NOT
        # full-width (a focus bar, an underline under one control) stay legal.
        offenders = re.findall(
            r"width:\s*root\.width[^}]*?height:\s*1\b|height:\s*1\b[^}]*?width:\s*root\.width",
            source,
            re.S,
        )
        assert not offenders, f"{name} grew a full-width 1px divider again"


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
