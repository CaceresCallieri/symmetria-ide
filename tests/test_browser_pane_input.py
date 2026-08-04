"""Structural tests for pointer input reaching the nested browser surface.

Same pattern as the sibling browser-pane tests: the invariant lives in QML and
C++, so it is asserted by reading the files.

WHAT IS BEING PROTECTED. Three separate things Qt does not do by default. (2)
and (3) are each independently fatal to SCROLLING; (1) is fatal to pointer
MOTION — hover states, hover-opened menus, cursor shape. All three are guarded
here.

The split is not theoretical: (1) was written in QML against a property that
does not exist, so it never ran for weeks while scrolling worked fine. An
earlier version of this docstring called all three fatal to scrolling.

1. **No pointer motion reaches the client.** `QWaylandQuickItem` implements
   `hoverEnterEvent`/`hoverMoveEvent` — the only two callers of
   `sendMouseMoveEvent`, which is what gives the seat's pointer a focused
   surface — but never sets `acceptHoverEvents`; verified against Qt 6.8 source
   that neither it nor `QWaylandQuickShellSurfaceItem` ever does. `wheelEvent`
   sends only an axis delta and no position, so with no focused surface it
   lands nowhere. Link hover, hover-opened menus, cursor shape and dropdown row
   highlighting die the same way.

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
`mousePressEvent` calls `sendMouseMoveEvent` itself, so under #1 the client
learns where the pointer is only when something is clicked — hover states come
and go for no apparent reason. And dragging a scrollbar works under #2 and #3
alike, because press and move touch neither the arithmetic nor the frame. Every
one of them reads as a focus problem.

Popups get the hover treatment but NEITHER the wheel quantisation nor the
frame, and none of it can be given declaratively: Qt creates those items in C++
(`maybeCreateAutoPopup`), so hover is reached by walking children and the item
class cannot be substituted at all.

All three fixes live in `symmetriashellsurfaceitem.{h,cpp}` — hover moved there
from QML, where the attempt had never worked. `hoverEnabled` is not a property
of any Wayland item class, so the assignment threw and the walk never ran; the
real API, `setAcceptHoverEvents`, is a C++ method with no QML equivalent.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from conftest import braced_block


@pytest.fixture(scope="module")
def pane_qml() -> str:
    repo_root = Path(__file__).resolve().parent.parent
    return (repo_root / "qml" / "browser" / "BrowserPane.qml").read_text()


@pytest.fixture(scope="module")
def item_cpp() -> str:
    repo_root = Path(__file__).resolve().parent.parent
    return (
        repo_root / "native" / "symmetria-compositor" / "symmetriashellsurfaceitem.cpp"
    ).read_text()


@pytest.fixture(scope="module")
def hover_block(item_cpp: str) -> str:
    return braced_block(item_cpp, "void SymmetriaShellSurfaceItem::enableHoverTree")


class TestPointerReachesTheSurface:
    def test_hover_is_enabled_in_cpp_not_qml(self, item_cpp: str, pane_qml: str):
        """`setAcceptHoverEvents` is a C++ METHOD with no QML equivalent, and
        that is the whole point of this assertion.

        The first attempt lived in BrowserPane.qml and assigned
        `item.hoverEnabled = true` — a property that exists on `MouseArea` and
        three QtQuickTemplates2 types, and on none of the Wayland item classes.
        It threw on its first line, which aborted the function before it could
        recurse or connect its signal, so the walk never ran for the surface OR
        the popups. Its only trace was one "Cannot assign to non-existent
        property" per session, and hover looked broken for reasons nobody could
        find. Guarded from both sides so it cannot come back."""
        assert "setAcceptHoverEvents(true)" in item_cpp
        # Comment lines stripped first: the file explains this trap on purpose,
        # and a bare substring check would forbid documenting the very thing it
        # is guarding against — which is how a well-meant guard gets deleted.
        code = "\n".join(
            line for line in pane_qml.splitlines() if not line.lstrip().startswith("//")
        )
        assert "hoverEnabled" not in code, (
            "hoverEnabled is not a property of any Wayland item class"
        )

    def test_the_constructor_enables_it(self, item_cpp: str):
        """Enabled at construction rather than on first use: the seat has no
        focused surface until motion arrives, so a client that has not been
        hovered yet does not know where the pointer is."""
        start = item_cpp.index("SymmetriaShellSurfaceItem::SymmetriaShellSurfaceItem")
        body = item_cpp[start : item_cpp.index("\n}", start)]
        assert "enableHoverTree(this)" in body

    def test_popups_are_reached_by_recursing(self, hover_block: str):
        """Qt builds popup items in C++ (`maybeCreateAutoPopup`) as stock items
        we never get to declare, so they cannot be handled by substituting the
        class — and a popup can parent further popups, so this has to recurse or
        a submenu goes dead."""
        assert "childItems()" in hover_block
        assert "enableHoverTree(child)" in hover_block, "the walk must recurse"

    def test_existing_children_are_swept_not_only_future_ones(self, hover_block: str):
        """`childrenChanged` reports only what arrives AFTER the connect, so a
        popup that parents a sub-popup during its own construction is missed
        and the walk stops one level short — silently, since a dead submenu
        reads as a Chrome quirk."""
        connect_at = hover_block.index("childrenChanged")
        assert "enableHoverTree(child)" in hover_block[:connect_at], (
            "the immediate sweep must happen before/independently of the connect"
        )

    def test_the_children_watch_cannot_stack_connections(self, hover_block: str):
        """This function re-runs over the whole subtree on every children
        change, so without `UniqueConnection` each sweep would add another
        connection per item and grow without bound. That in turn forbids a
        lambda, which `UniqueConnection` does not support — hence a member
        slot."""
        assert "Qt::UniqueConnection" in hover_block
        assert "onChildrenChanged" in hover_block, "a lambda cannot be unique"


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

    def test_the_remainder_is_carried_not_dropped(self, item_cpp: str):
        """Quantising without a carry would discard every fragment below the
        grain — the same failure as Qt's truncation, just relocated."""
        assert "kWaylandAngleStep = 12" in item_cpp
        assert "m_carry += event->angleDelta()" in item_cpp
        assert "m_carry -= step" in item_cpp


class TestChromiumDispatchesTheScroll:
    def test_a_pointer_frame_follows_the_axis(self, item_cpp: str):
        """The defect with NO symptom on our side: axis events look sent and
        correct, and Chromium buffers every one of them because
        `ProcessPointerScrollData()` runs only from its frame handler. Delete
        the frame send and every structural test here still passes while
        scrolling is dead again."""
        assert "send_frame(" in item_cpp
        body = item_cpp[item_cpp.index("void SymmetriaShellSurfaceItem::wheelEvent") :]
        assert "sendPointerFrame(" in body, (
            "the frame must be sent from the wheel path, not merely defined"
        )

    def test_a_refused_forward_sends_no_frame_and_keeps_the_carry(self, item_cpp: str):
        """The base refuses the event (no surface, or outside the input region)
        without sending any axis. Treating that as success spends the carry on
        a scroll that never happened and emits an empty frame group."""
        body = item_cpp[item_cpp.index("void SymmetriaShellSurfaceItem::wheelEvent") :]
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
