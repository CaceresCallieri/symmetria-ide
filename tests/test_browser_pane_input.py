"""Structural tests for pointer input reaching the nested browser surface.

Same pattern as the sibling browser-pane tests: the invariant lives in QML, so
it is asserted by reading the file.

WHAT IS BEING PROTECTED. Pages in the nested browser did not scroll, and it
took TWO independent defects in Qt's stock items to explain it. Both are
guarded here because either one alone brings the symptom back.

1. **No pointer motion reaches the client.** `QWaylandQuickItem` implements
   `hoverEnterEvent`/`hoverMoveEvent` — the only two callers of
   `sendMouseMoveEvent`, which is what gives the seat's pointer a focused
   surface — but never sets `acceptHoverEvents`, so on a stock item neither
   fires. `wheelEvent` sends only an axis delta and no position, so with no
   focused surface it lands nowhere. Link hover, hover-opened menus, cursor
   shape and dropdown row highlighting die the same way.

2. **The wheel value is truncated to zero.** `sendMouseWheelEvent` ends in
   `wl_fixed_from_int(-delta / 12)` — truncating integer division — so any
   `angleDelta` under 12 becomes a zero-valued axis event. Only a classic
   detented wheel (120 per notch) survives it.

Both symptoms point away from themselves, which is the reason for the fuss.
`mousePressEvent` calls `sendMouseMoveEvent` itself, so #1 looks like scrolling
that "starts working after a click"; and dragging a scrollbar works under #2
because press and move never touch that arithmetic. Either reads as a focus
problem, and the wheel path looks innocent until you read the send.

Popups get the hover treatment but NOT the wheel one, and neither can be given
declaratively: Qt creates those items in C++ (`maybeCreateAutoPopup`), so hover
is reached by walking children and the item class cannot be substituted at all.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def pane_qml() -> str:
    repo_root = Path(__file__).resolve().parent.parent
    return (repo_root / "qml" / "browser" / "BrowserPane.qml").read_text()


class TestPointerReachesTheSurface:
    def test_surface_items_get_hover_enabled(self, pane_qml: str):
        """Without this the client gets no pointer motion: no scrolling, no
        link hover, no cursor shape."""
        assert "_enableHoverTree(surfaceItem)" in pane_qml
        assert "item.hoverEnabled = true" in pane_qml

    def test_popups_are_reached_by_walking_children(self, pane_qml: str):
        """Qt builds popup items in C++, so they cannot be given the property
        declaratively — and a popup can parent further popups, so the walk has
        to recurse or a submenu goes dead."""
        block = pane_qml[pane_qml.index("function _enableHoverTree") :][:900]
        assert "childrenChanged.connect" in block
        assert "pane._enableHoverTree(child)" in block, "the walk must recurse"

    def test_only_shell_surface_items_are_touched(self, pane_qml: str):
        """A ShellSurfaceItem's children are not all surfaces; `shellSurface`
        is what identifies one."""
        block = pane_qml[pane_qml.index("function _enableHoverTree") :][:900]
        assert "child.shellSurface !== undefined" in block


class TestWheelSurvivesTheTrip:
    def test_surface_items_are_our_wheel_fixing_subclass(self, pane_qml: str):
        """`QWaylandPointer::sendMouseWheelEvent` truncates any angleDelta
        under 12 to a ZERO-valued axis event. A classic detented wheel reports
        120 per notch and survives; a high-resolution wheel or a touchpad
        reports the same notch as fragments, all of which vanish — measured on
        hardware that advertises REL_WHEEL_HI_RES and NOT REL_WHEEL, so it
        cannot emit a 120-unit step at all. Revert to the stock
        `ShellSurfaceItem` and pages stop scrolling outright, while dragging
        their scrollbars keeps working — which points at focus, not at
        arithmetic."""
        assert "SymmetriaShellSurfaceItem {" in pane_qml
        # Word-anchored: a plain substring test matches inside our own type
        # name, so it would pass no matter what the file says.
        assert not re.search(r"(?<![A-Za-z])ShellSurfaceItem\s*\{", pane_qml), (
            "a stock ShellSurfaceItem drops sub-threshold wheel events"
        )


class TestClientsCannotPaintOverTheIde:
    def test_the_surface_host_clips(self, pane_qml: str):
        """Chrome positions popups against the screen we advertise and Qt
        honours that position unconstrained, so clipping is the only thing
        keeping an oversized popup off the file tree next door."""
        block = pane_qml[pane_qml.index("id: surfaceHost") :][:200]
        assert "clip: true" in block
