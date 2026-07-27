"""Structural tests for the nested output's geometry contract.

Same pattern and rationale as `test_main_qml_browser_wiring.py`: the invariant
lives entirely in QML, so it is asserted by reading the file.

WHAT IS BEING PROTECTED. The nested output is the "screen" Chrome believes it
is on, and Chrome decides FOR ITSELF whether an omnibox dropdown fits below the
omnibox or has to flip up over it. Qt does not second-guess that — its
`XdgPopupIntegration` places every popup at `unconstrainedPosition` under a
literal `//TODO check positioner constraints etc... sliding, flipping`. So the
output description is the only lever there is over popup placement, and getting
it wrong has no other symptom than "the browser looks broken".

Two numbers have to agree, and they are set in different places:

    the toplevel is configured in PANE units          (`sendMaximized`)
    the output mode is set in PANE units x scaleFactor (`setModeSize`)

A client divides the mode by the advertised scale, so both land on the pane and
window == screen. Break either half and they are measured in different units.
That is not hypothetical: it is the bug this replaced, measured live as a
1311x868 Chrome window sitting on a 1273x733 screen — a window 135px taller
than its own screen, which is exactly when the dropdown flips up over the
omnibox.
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
def output_block(pane_qml: str) -> str:
    """The `SymmetriaOutput { ... }` body."""
    start = pane_qml.index("SymmetriaOutput {")
    return pane_qml[start : pane_qml.index("XdgShell {", start)]


class TestOutputDescribesThePane:
    def test_output_is_our_subclass(self, output_block: str):
        """Stock `WaylandOutput` cannot express this at all: `geometry` is
        read-only and `setCurrentMode` is not Q_INVOKABLE, so its only sizing
        mode is `sizeFollowsWindow` — the whole host window, wrong rectangle."""
        assert "SymmetriaOutput {" in output_block

    def test_size_does_not_follow_the_window(self, output_block: str):
        """Left true, Qt overwrites our mode with the window's pixel size on
        the next resize and the fix silently reverts."""
        assert re.search(r"sizeFollowsWindow:\s*false", output_block)

    def test_mode_is_pane_size_times_the_advertised_scale(self, output_block: str):
        """The multiply IS the unit contract — clients divide the mode by the
        advertised scale. Drop it and Chrome sees a screen `scaleFactor` times
        too small, so every popup near an edge gets flipped."""
        call = re.search(r"setModeSize\(([^;]*?)\)\)\s*$", output_block, re.MULTILINE)
        assert call, "setModeSize is not called with a Qt.size(...)"
        args = call.group(1)
        assert "pane.width * scaleFactor" in args
        assert "pane.height * scaleFactor" in args

    def test_scale_is_rounded_up_from_the_host_ratio(self, output_block: str):
        """`wl_output` scale is an integer by protocol. Rounding DOWN (or
        flooring 1.6 to 1) makes Chrome draw 1x buffers that get stretched —
        the softness that prompted this whole thread."""
        assert "Math.ceil" in output_block
        assert "devicePixelRatio" in output_block


class TestBothHalvesStayInTheSameUnits:
    def test_toplevels_are_configured_in_unmultiplied_pane_units(self, pane_qml: str):
        """The counterpart to the mode's multiply. Every configure must pass
        the pane size RAW: the client already divided the mode by the scale, so
        multiplying here too would double-apply it."""
        configures = re.findall(r"send(?:Maximized|Configure)\(\s*([^)]*\))", pane_qml)
        assert configures, "no toplevel configure calls found"
        for args in configures:
            assert "pane.width, pane.height" in args, args
            assert "scaleFactor" not in args, args


class TestFirstPushIsNotDeferred:
    def test_the_initial_mode_bypasses_the_settle_timer(self, pane_qml: str):
        """With `sizeFollowsWindow` off, Qt's initialize() adds NO mode at all.
        Until the first push lands the output has no resolution, so deferring
        it behind the debounce leaves a window where a connecting client sees a
        broken screen."""
        block = pane_qml[pane_qml.index("function _scheduleOutputMode()") :][:400]
        assert "if (!browserOutput.modeApplied)" in block
        assert "browserOutput.syncMode()" in block

    def test_resize_goes_through_the_settle_timer(self, pane_qml: str):
        """`addMode` APPENDS — Qt's in-place mode setter is private — and every
        change re-broadcasts the whole list to every client. One entry per
        settled size is affordable; one per frame of a drag is not."""
        assert "outputModeSettle.restart()" in pane_qml
        for handler in ("onWidthChanged", "onHeightChanged"):
            block = pane_qml[pane_qml.index(handler) :][:200]
            assert "_scheduleOutputMode()" in block
