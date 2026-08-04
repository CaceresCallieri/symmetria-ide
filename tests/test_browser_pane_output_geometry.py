"""Structural tests for the nested output's geometry contract.

Same pattern and rationale as the sibling browser-pane tests: the invariant
lives in QML and C++, so it is asserted by reading the files.

WHAT IS BEING PROTECTED — the unit contract. Two numbers are set in different
places and must agree:

    the toplevel is configured in RAW pane units          (`sendMaximized`)
    the output mode is set in PANE units x scaleFactor    (`setModeSize`)

A client divides the mode by the advertised scale, so both land on the pane and
window == screen. Break either half and the two are measured in different
units, which is not hypothetical: it is the bug this replaced, measured live as
a 1311x868 Chrome window sitting on a 1273x733 screen. Nothing at runtime
checks the agreement, which is why it is checked here.

Why it matters at all — Chrome decides FOR ITSELF whether a dropdown fits, and
Qt places popups at `unconstrainedPosition` without constraining them, so the
advertised screen is the only lever. Full derivation:
`native/symmetria-compositor/symmetriaoutput.h`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _braced_block(text: str, marker: str) -> str:
    """The `{ ... }` block introduced by `marker`, by brace counting.

    Fixed-width slices were used here before and are a trap: grow the block past
    the magic length and the assertions silently move out of the inspected
    window, so the failure reads as a regression in the code rather than in the
    test.
    """
    start = text.index(marker)
    open_at = text.index("{", start)
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise AssertionError(f"unbalanced braces after {marker!r}")


def _call_args(text: str, call: str) -> list[str]:
    """Full argument text of every `call(...)`, to the MATCHING close paren.

    A naive `[^)]*\\)` regex stops at the first `)`, which for these calls is
    the end of a nested `Qt.size(...)` — so it never sees a later argument and
    an assertion over it proves nothing.
    """
    out: list[str] = []
    for match in re.finditer(re.escape(call) + r"\s*\(", text):
        depth = 0
        for i in range(match.end() - 1, len(text)):
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
                if depth == 0:
                    out.append(text[match.end() : i])
                    break
    return out


@pytest.fixture(scope="module")
def pane_qml() -> str:
    return (_REPO_ROOT / "qml" / "browser" / "BrowserPane.qml").read_text()


@pytest.fixture(scope="module")
def output_cpp() -> str:
    return (
        _REPO_ROOT / "native" / "symmetria-compositor" / "symmetriaoutput.cpp"
    ).read_text()


@pytest.fixture(scope="module")
def output_block(pane_qml: str) -> str:
    return _braced_block(pane_qml, "SymmetriaOutput {")


class TestOutputDescribesThePane:
    def test_output_is_our_subclass(self, pane_qml: str):
        """Stock `WaylandOutput` cannot express this at all: `geometry` is
        read-only and `setCurrentMode` is not Q_INVOKABLE, so its only sizing
        mode is `sizeFollowsWindow` — the whole host window, wrong rectangle.

        Asserted against the WHOLE file: checking inside the block that the
        fixture located by searching for this very type name could never fail.
        """
        assert "import Symmetria.Compositor" in pane_qml
        assert "SymmetriaOutput {" in pane_qml
        assert not re.search(r"^\s*WaylandOutput\s*\{", pane_qml, re.MULTILINE), (
            "the stock output can only track the host window"
        )

    def test_size_does_not_follow_the_window(self, output_block: str):
        """Left true, Qt overwrites our mode with the window's pixel size on
        the next resize and the fix silently reverts. Asserted on the SAME
        block as the mode push, so the two cannot drift onto different
        elements."""
        assert re.search(r"sizeFollowsWindow:\s*false", output_block)
        assert "setModeSize(" in output_block

    def test_mode_is_pane_size_times_the_advertised_scale(self, output_block: str):
        """The multiply IS the unit contract — clients divide the mode by the
        advertised scale. Drop it and Chrome sees a screen `scaleFactor` times
        too small, so every popup near an edge gets flipped."""
        wanted = _braced_block(output_block, "function wantedModeSize()")
        assert "pane._paneSize()" in wanted, "must derive from the shared pane rect"
        assert wanted.count("browserOutput.scaleFactor") == 2, (
            "both axes must be scaled"
        )
        assert wanted.count("Math.round") == 2, (
            "QML size is a QSizeF and the C++ boundary truncates"
        )

    def test_scale_is_rounded_up_from_the_host_ratio(self, output_block: str):
        """`wl_output` scale is an integer by protocol. Rounding DOWN (or
        flooring 1.6 to 1) makes Chrome draw 1x buffers that get stretched —
        the softness that prompted this whole thread. The binding SHAPE is
        asserted: a bare `Math.ceil` substring would still pass with the
        scaleFactor itself changed to `Math.floor`."""
        assert re.search(r"scaleFactor:\s*Math\.max\(1,\s*Math\.ceil\(", output_block)
        assert "devicePixelRatio" in output_block


class TestBothHalvesStayInTheSameUnits:
    def test_toplevels_are_configured_in_unmultiplied_pane_units(self, pane_qml: str):
        """The counterpart to the mode's multiply. Every configure must pass
        the pane size RAW: the client already divided the mode by the scale, so
        multiplying here too would double-apply it."""
        configures = _call_args(pane_qml, "sendMaximized") + _call_args(
            pane_qml, "sendConfigure"
        )
        assert len(configures) == 3, (
            "a new configure site must be covered here, not silently skipped"
        )
        for args in configures:
            assert "pane._paneSize()" in args, args
            assert "scaleFactor" not in args, args

    def test_the_pane_rect_has_one_definition(self, pane_qml: str):
        """Spelling the size out per call site is how the two halves drifted
        apart in the first place — a change to one was invisible from the
        others."""
        assert "function _paneSize()" in pane_qml
        # Exactly once — inside `_paneSize` itself. A second occurrence is a
        # call site that spelled the rect out again instead of asking for it.
        spelled_out = re.findall(r"Qt\.size\(pane\.width,\s*pane\.height\)", pane_qml)
        assert len(spelled_out) == 1, (
            "configure sites must go through _paneSize(), not respell the rect"
        )
        assert "Qt.size(pane.width, pane.height)" in _braced_block(
            pane_qml, "function _paneSize()"
        )


class TestModePushScheduling:
    def test_growth_is_never_debounced(self, pane_qml: str):
        """While the pane GROWS, the toplevel is configured per frame but a
        lagging mode leaves Chrome's window larger than its own screen — the
        exact state that flips the omnibox dropdown. Debouncing a shrink is
        harmless; debouncing a grow reinstates the bug for the whole drag."""
        block = _braced_block(pane_qml, "function _scheduleOutputMode()")
        assert "want.width > current.width" in block
        assert "want.height > current.height" in block
        assert "browserOutput.syncMode()" in block

    def test_the_first_push_bypasses_the_settle_timer(self, pane_qml: str):
        """With `sizeFollowsWindow` off, Qt's initialize() adds NO mode at all.
        Until the first push lands the output has no resolution, so deferring
        it leaves a window where a connecting client sees a broken screen."""
        block = _braced_block(pane_qml, "function _scheduleOutputMode()")
        assert "!browserOutput.hasMode()" in block, (
            "C++ must own this; a QML flag claims success for early returns"
        )
        # Ordering, not mere presence: inverting the branches would satisfy a
        # test that only checked both calls appear somewhere in the file.
        assert block.index("browserOutput.syncMode()") < block.index(
            "outputModeSettle.restart()"
        )

    def test_startup_pushes_a_mode_without_waiting_for_a_resize(self, pane_qml: str):
        """Delete this and the suite still passes while the output stays
        mode-less until a resize that may never come."""
        assert re.search(r"Component\.onCompleted:\s*_scheduleOutputMode\(\)", pane_qml)

    def test_a_scale_change_is_not_debounced(self, pane_qml: str):
        """Rare and never per-frame, so delaying it only leaves the advertised
        screen wrong for the settle."""
        block = _braced_block(pane_qml, "SymmetriaOutput {")
        assert re.search(r"onScaleFactorChanged:\s*browserOutput\.syncMode\(\)", block)


class TestOutputImplementation:
    """The C++ half of the contract. The QML tests above cannot see any of it,
    and each of these is a silent failure rather than a loud one."""

    def test_an_unset_size_is_ignored(self, output_cpp: str):
        """A pane still being laid out is 0-sized and `QWaylandOutputMode`
        rejects that, so without the guard this warns once per startup frame."""
        assert "if (size.isEmpty())" in output_cpp

    def test_an_unchanged_size_is_a_no_op(self, output_cpp: str):
        """`addMode` APPENDS and every change re-broadcasts the whole list, so
        without this every geometry change would cost a permanent entry."""
        assert "existing.size() == size" in output_cpp

    def test_the_mode_is_added_before_it_is_made_current(self, output_cpp: str):
        """Load-bearing order: `setCurrentMode` looks the mode up in the list
        and, per Qt, warns "Cannot set an unknown QWaylandOutput mode as
        current" and leaves the output unchanged. Reversed, every push is a
        silent no-op with only a Qt warning to show for it."""
        assert output_cpp.index("addMode(") < output_cpp.index("setCurrentMode(")

    def test_sizeFollowsWindow_being_left_on_is_reported(self, output_cpp: str):
        """It is one line in a different file and the only symptom of getting
        it wrong is a subtly misplaced Chrome popup — no error, no crash."""
        assert "sizeFollowsWindow()" in output_cpp
        assert "qWarning" in output_cpp

    def test_the_watchdog_is_wired_from_initialize(self, output_cpp: str):
        """The signals alone are NOT enough, measured. With the output declared
        inside the compositor in QML, `compositorChanged` never reached us, so a
        watch wired only from that signal stayed unarmed and the whole fix did
        nothing — with no error anywhere, because an unarmed watchdog looks
        exactly like a working one until an agent's screenshot hangs."""
        assert "void SymmetriaOutput::initialize()" in output_cpp
        block = _braced_block(output_cpp, "void SymmetriaOutput::initialize()")
        assert "QWaylandQuickOutput::initialize()" in block, (
            "skipping the base leaves the output uninitialised"
        )
        assert "rewireSurfaceWatch()" in block
        assert "rewireRenderWatch()" in block

    def test_frame_started_precedes_send_frame_callbacks(self, output_cpp: str):
        """The pair the render loop calls, in the order it calls them:
        `frameStarted` marks each surface as beginning a frame, and only then
        does `sendFrameCallbacks` release the clients waiting on one."""
        block = _braced_block(
            output_cpp, "void SymmetriaOutput::pumpFrameCallbacksIfStalled()"
        )
        assert block.index("frameStarted()") < block.index("sendFrameCallbacks()")

    def test_a_real_host_frame_suppresses_the_pump(self, output_cpp: str):
        """Without this the watchdog would double-send while the host renders
        normally, inviting the client to draw frames nobody will show."""
        block = _braced_block(
            output_cpp, "void SymmetriaOutput::pumpFrameCallbacksIfStalled()"
        )
        assert "m_renderedSinceLastTick" in block
        assert block.index("m_renderedSinceLastTick") < block.index("frameStarted()")

    def test_the_watchdog_runs_only_while_a_client_has_a_surface(self, output_cpp: str):
        """The pane's compositor is built at startup and never torn down, so an
        ungated timer would tick for the whole session in EVERY IDE — and the
        user runs about ten at once. A project that never browses must pay
        nothing, the same principle that makes Chrome lazy-spawned."""
        block = _braced_block(
            output_cpp, "void SymmetriaOutput::updateWatchdogEnabled()"
        )
        assert "surfaces().isEmpty()" in block
        assert ".start()" in block and ".stop()" in block

    def test_the_destroy_signal_is_queued(self, output_cpp: str):
        """`surfaceAboutToBeDestroyed` fires while the surface is STILL in
        `surfaces()`. Counted directly, the last close would see the doomed
        surface, conclude a client is present, and leave the watchdog ticking
        forever — the exact cost the gate exists to avoid."""
        block = _braced_block(output_cpp, "void SymmetriaOutput::rewireSurfaceWatch()")
        destroy_at = block.index("surfaceAboutToBeDestroyed")
        assert "Qt::QueuedConnection" in block[destroy_at:], (
            "a direct connection re-counts the surface before it is gone"
        )

    def test_the_refresh_rate_is_requeried_not_frozen(self, output_cpp: str):
        """Taking the previous mode's rate first freezes whatever the first
        push saw — including a value queried before the window had a screen —
        so moving to a different-Hz monitor could never correct it."""
        screen_at = output_cpp.index("screen->refreshRate()")
        fallback_at = output_cpp.index("existing.refreshRate()")
        assert screen_at < fallback_at, "the screen must be consulted first"
