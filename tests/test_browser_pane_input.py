"""Structural tests for pointer input reaching the nested browser surface.

Same pattern as the sibling browser-pane tests: the invariant lives in QML, so
it is asserted by reading the file.

WHAT IS BEING PROTECTED. Pages in the nested browser did not scroll, and it
took THREE independent defects to explain it. All three are guarded here
because any one alone brings the symptom back.

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

3. **Chromium never dispatches on the axis.** `OnPointerAxisEvent` only
   accumulates into `pointer_scroll_data_`; the single call site that flushes
   it, `ProcessPointerScrollData()`, runs inside `OnPointerFrameEvent()`. Qt
   never sends `wl_pointer.frame`, so correct axis values piled into a buffer
   nothing emptied. This one is invisible from our side entirely — the events
   look sent and correct.

All three point away from themselves, which is the reason for the fuss.
`mousePressEvent` calls `sendMouseMoveEvent` itself, so #1 looks like scrolling
that "starts working after a click"; and dragging a scrollbar works under #2
and #3 alike, because press and move touch neither the arithmetic nor the
frame. Every one of them reads as a focus problem.

Popups get the hover treatment but NEITHER the wheel quantisation nor the
frame, and none of it can be given declaratively: Qt creates those items in C++
(`maybeCreateAutoPopup`), so hover is reached by walking children and the item
class cannot be substituted at all.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def pane_qml() -> str:
    repo_root = Path(__file__).resolve().parent.parent
    return (repo_root / "qml" / "browser" / "BrowserPane.qml").read_text()


@pytest.fixture(scope="module")
def wheel_cpp() -> str:
    repo_root = Path(__file__).resolve().parent.parent
    return (
        repo_root / "native" / "symmetria-compositor" / "symmetriashellsurfaceitem.cpp"
    ).read_text()


@pytest.fixture(scope="module")
def hover_block(pane_qml: str) -> str:
    """The hover walk, delimited structurally rather than by a fixed width — a
    character count silently moves the assertions out of the window as the
    function grows, and the failure then reads as a regression in the code."""
    start = pane_qml.index("function _hoverSweep")
    return pane_qml[start : pane_qml.index("Component {", start)]


class TestPointerReachesTheSurface:
    def test_surface_items_get_hover_enabled(self, pane_qml: str):
        """Without this the client gets no pointer motion: no scrolling, no
        link hover, no cursor shape."""
        assert "_enableHoverTree(surfaceItem)" in pane_qml
        assert "item.hoverEnabled = true" in pane_qml

    def test_popups_are_reached_by_walking_children(self, hover_block: str):
        """Qt builds popup items in C++, so they cannot be given the property
        declaratively — and a popup can parent further popups, so the walk has
        to recurse or a submenu goes dead."""
        assert "childrenChanged.connect" in hover_block
        assert "pane._enableHoverTree(child)" in hover_block, "the walk must recurse"

    def test_existing_children_are_swept_not_only_future_ones(self, hover_block: str):
        """`childrenChanged` reports only what arrives AFTER the connect, so a
        popup that parents a sub-popup during its own construction is missed
        and the walk stops one level short — silently, since a dead submenu
        reads as a Chrome quirk."""
        assert "_hoverSweep(item)" in hover_block
        connect_at = hover_block.index("childrenChanged.connect")
        assert "_hoverSweep(item)" in hover_block[:connect_at], (
            "the immediate sweep must happen before/independently of the connect"
        )

    def test_only_shell_surface_items_are_touched(self, hover_block: str):
        """A ShellSurfaceItem's children are not all surfaces; `shellSurface`
        is what identifies one."""
        assert "child.shellSurface !== undefined" in hover_block


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

    def test_the_remainder_is_carried_not_dropped(self, wheel_cpp: str):
        """Quantising without a carry would discard every fragment below the
        grain — the same failure as Qt's truncation, just relocated."""
        assert "kWaylandAngleStep = 12" in wheel_cpp
        assert "m_carry += event->angleDelta()" in wheel_cpp
        assert "m_carry -= step" in wheel_cpp


class TestChromiumDispatchesTheScroll:
    def test_a_pointer_frame_follows_the_axis(self, wheel_cpp: str):
        """The defect with NO symptom on our side: axis events look sent and
        correct, and Chromium buffers every one of them because
        `ProcessPointerScrollData()` runs only from its frame handler. Delete
        the frame send and every structural test here still passes while
        scrolling is dead again."""
        assert "send_frame(" in wheel_cpp
        body = wheel_cpp[
            wheel_cpp.index("void SymmetriaShellSurfaceItem::wheelEvent") :
        ]
        assert "sendPointerFrame(" in body, (
            "the frame must be sent from the wheel path, not merely defined"
        )

    def test_a_refused_forward_sends_no_frame_and_keeps_the_carry(self, wheel_cpp: str):
        """The base refuses the event (no surface, or outside the input region)
        without sending any axis. Treating that as success spends the carry on
        a scroll that never happened and emits an empty frame group."""
        body = wheel_cpp[
            wheel_cpp.index("void SymmetriaShellSurfaceItem::wheelEvent") :
        ]
        assert "forwarded.isAccepted()" in body
        assert "m_carry += step" in body, "a refused forward must restore the carry"


class TestClientsCannotPaintOverTheIde:
    def test_the_surface_host_clips(self, pane_qml: str):
        """Chrome positions popups against the screen we advertise and Qt
        honours that position unconstrained, so clipping is the only thing
        keeping an oversized popup off the file tree next door."""
        start = pane_qml.index("id: surfaceHost")
        block = pane_qml[start : pane_qml.index("}", start)]
        assert "clip: true" in block
