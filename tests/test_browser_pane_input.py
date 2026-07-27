"""Structural tests for pointer input reaching the nested browser surface.

Same pattern as the sibling browser-pane tests: the invariant lives in QML, so
it is asserted by reading the file.

WHAT IS BEING PROTECTED. `QWaylandQuickItem` implements `hoverEnterEvent` and
`hoverMoveEvent` — the only two places that call `sendMouseMoveEvent`, which is
what gives the seat's pointer a focused surface — but it never sets
`acceptHoverEvents`. On a stock item neither handler ever fires, so the nested
client receives no pointer motion at all.

The loudest casualty is scrolling, and its symptom actively misleads:
`wheelEvent` sends only the axis delta and no position, so with no pointer
focus it lands nowhere and pages do not scroll — EXCEPT that
`mousePressEvent` calls `sendMouseMoveEvent` itself, so scrolling appears to
start working after a click. Anyone diagnosing that will chase the wheel path
and find nothing wrong with it.

Popups need the same treatment and cannot get it declaratively: Qt creates them
in C++ (`maybeCreateAutoPopup`) as items we never declare, so they are reached
by walking children. Without that, hovering a row in the omnibox dropdown
highlights nothing.
"""

from __future__ import annotations

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


class TestClientsCannotPaintOverTheIde:
    def test_the_surface_host_clips(self, pane_qml: str):
        """Chrome positions popups against the screen we advertise and Qt
        honours that position unconstrained, so clipping is the only thing
        keeping an oversized popup off the file tree next door."""
        block = pane_qml[pane_qml.index("id: surfaceHost") :][:200]
        assert "clip: true" in block
