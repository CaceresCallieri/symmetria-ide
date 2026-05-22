"""Tests for the Phase 2.5 central-surface state machine on AppController.

Covers the swap-state machine introduced in PR 4:

- `_central_surface` is the source of truth ("terminal" | "editor").
- `centralSurface`, `editorVisible`, `terminalVisible` are derived
  `@Property` over the same notify signal — they can never drift.
- `swap_to_*` slots are idempotent (no spurious signal on repeat).
- `focus_terminal` emits `focusTerminalRequested` for QML to handle.
- `start()` pre-warms the terminal backend AFTER nvim, per the Q1-1b
  pattern.
- `shutdown()` stops the terminal BEFORE nvim so the shell's process
  group is reaped before the event loop tears down.

Hermetic shape: monkeypatch `NvimBackend.start/stop` and
`TerminalBackend.start/stop` so AppController.start() / shutdown()
exercise the orchestration logic without spawning real subprocesses.
"""

from __future__ import annotations

import pytest

from symmetria_ide.app import AppController
from symmetria_ide.nvim_backend import NvimBackend
from symmetria_ide.terminal_backend import TerminalBackend


# ---------------------------------------------------------------------------
# Subprocess-free fixtures — patch start/stop on both backends so a bare
# AppController() + start() + shutdown() cycle is hermetic.
# ---------------------------------------------------------------------------


@pytest.fixture
def patched_backends(monkeypatch):
    """Make NvimBackend.start and TerminalBackend.start no-op, with
    capture lists so tests can assert ordering / call args."""
    nvim_starts: list[None] = []
    nvim_stops: list[None] = []
    terminal_starts: list[str] = []
    terminal_stops: list[None] = []
    call_order: list[str] = []

    def fake_nvim_start(self):
        nvim_starts.append(None)
        call_order.append(f"nvim_start#{len(call_order)}")

    def fake_nvim_stop(self):
        nvim_stops.append(None)
        call_order.append(f"nvim_stop#{len(call_order)}")

    def fake_terminal_start(self, cwd):
        terminal_starts.append(cwd)
        call_order.append(f"terminal_start#{len(call_order)}")

    def fake_terminal_stop(self):
        terminal_stops.append(None)
        call_order.append(f"terminal_stop#{len(call_order)}")

    monkeypatch.setattr(NvimBackend, "start", fake_nvim_start)
    monkeypatch.setattr(NvimBackend, "stop", fake_nvim_stop)
    monkeypatch.setattr(TerminalBackend, "start", fake_terminal_start)
    monkeypatch.setattr(TerminalBackend, "stop", fake_terminal_stop)

    # Guard against SYMMETRIA_IDE_AGENT_PROMPT / _VIEW set in the caller's
    # shell or CI environment — if either is live, start() invokes
    # _create_instance/_spawn_instance which tries to spawn a real
    # SessionHost subprocess, breaking hermeticity. Pattern from
    # test_app_controller_awaiting.py's env-isolation fixtures.
    monkeypatch.delenv("SYMMETRIA_IDE_AGENT_PROMPT", raising=False)
    monkeypatch.delenv("SYMMETRIA_IDE_AGENT_VIEW", raising=False)

    return {
        "nvim_starts": nvim_starts,
        "nvim_stops": nvim_stops,
        "terminal_starts": terminal_starts,
        "terminal_stops": terminal_stops,
        "call_order": call_order,
    }


@pytest.fixture
def controller():
    """Bare controller, no patching — for tests that don't call start/stop."""
    ctrl = AppController()
    yield ctrl
    ctrl.shutdown()


# ---------------------------------------------------------------------------
# Initial state — Q2-d topology: terminal visible at launch.
# ---------------------------------------------------------------------------


def test_initial_central_surface_is_terminal(controller):
    """Q2-d topology — first launch shows the terminal, not the editor."""
    assert controller.centralSurface == "terminal"
    assert controller.terminalVisible is True
    assert controller.editorVisible is False


def test_visibility_props_are_xor(controller):
    """`editorVisible` and `terminalVisible` are derived from the same
    field, so they can never both be true (or both false). Pin this
    invariant explicitly — a future refactor that splits them into
    separate stored fields would break this test."""
    assert controller.editorVisible is not controller.terminalVisible


# ---------------------------------------------------------------------------
# Swap slots — idempotent transitions + XOR invariant maintained.
# ---------------------------------------------------------------------------


def _capture(signal) -> list[None]:
    """Capture emission count for a parameterless signal."""
    emissions: list[None] = []
    signal.connect(lambda: emissions.append(None))
    return emissions


def test_swap_to_terminal_on_terminal_is_noop(controller):
    """Already on terminal → swap_to_terminal fires nothing.

    Catches a regression where the slot would emit unconditionally,
    causing spurious QML re-binds on every chord press.
    """
    emissions = _capture(controller.centralSurfaceChanged)
    controller.swap_to_terminal()
    assert emissions == []
    assert controller.centralSurface == "terminal"


def test_swap_to_editor_changes_surface_and_emits(controller):
    """terminal → editor flips state AND emits exactly one signal."""
    emissions = _capture(controller.centralSurfaceChanged)

    controller.swap_to_editor()

    assert controller.centralSurface == "editor"
    assert controller.editorVisible is True
    assert controller.terminalVisible is False
    assert len(emissions) == 1


def test_swap_to_editor_on_editor_is_noop(controller):
    """Already on editor → swap_to_editor fires nothing."""
    controller.swap_to_editor()  # now editor
    emissions = _capture(controller.centralSurfaceChanged)
    controller.swap_to_editor()
    assert emissions == []
    assert controller.centralSurface == "editor"


def test_swap_to_terminal_changes_surface_and_emits(controller):
    """editor → terminal flips state AND emits exactly one signal.

    Symmetric counterpart of test_swap_to_editor_changes_surface_and_emits.
    Catches a regression where swap_to_terminal mutates state but forgets to
    emit, or emits but forgets to mutate — neither would be caught by the
    round-trip test's aggregate emission count alone.
    """
    controller.swap_to_editor()  # precondition: start from editor
    emissions = _capture(controller.centralSurfaceChanged)

    controller.swap_to_terminal()

    assert controller.centralSurface == "terminal"
    assert controller.terminalVisible is True
    assert controller.editorVisible is False
    assert len(emissions) == 1


def test_round_trip_swap_preserves_invariant(controller):
    """terminal → editor → terminal → editor: each transition emits once;
    XOR invariant holds at every step."""
    emissions = _capture(controller.centralSurfaceChanged)

    controller.swap_to_editor()
    assert controller.editorVisible is not controller.terminalVisible
    controller.swap_to_terminal()
    assert controller.editorVisible is not controller.terminalVisible
    controller.swap_to_editor()
    assert controller.editorVisible is not controller.terminalVisible

    assert len(emissions) == 3


# ---------------------------------------------------------------------------
# Toggle slot — Ctrl+Shift+E flips editor↔terminal. Asymmetric: from
# editor → terminal; from anything else → editor (chord names the editor,
# so non-editor → editor is the dominant direction).
# ---------------------------------------------------------------------------


def test_toggle_from_terminal_lands_on_editor(controller):
    """First press of Ctrl+Shift+E from the default terminal surface
    lands on the editor. Pins the "chord names the editor" semantic."""
    assert controller.centralSurface == "terminal"
    emissions = _capture(controller.centralSurfaceChanged)

    controller.toggle_editor_terminal()

    assert controller.centralSurface == "editor"
    assert controller.editorVisible is True
    assert controller.terminalVisible is False
    assert len(emissions) == 1


def test_toggle_from_editor_returns_to_terminal(controller):
    """Pressing the toggle while on the editor returns to terminal.
    This is the 'flip' half of the toggle's contract."""
    controller.swap_to_editor()  # precondition: start on editor
    emissions = _capture(controller.centralSurfaceChanged)

    controller.toggle_editor_terminal()

    assert controller.centralSurface == "terminal"
    assert controller.terminalVisible is True
    assert controller.editorVisible is False
    assert len(emissions) == 1


def test_toggle_round_trip_emits_each_time(controller):
    """Unlike the swap_to_* primitives, toggle is never a no-op from
    the user's perspective — each press flips state and emits exactly
    one signal. Catches a regression where the toggle short-circuits
    after a redundant call into one of the primitives."""
    emissions = _capture(controller.centralSurfaceChanged)

    controller.toggle_editor_terminal()  # terminal → editor
    controller.toggle_editor_terminal()  # editor → terminal
    controller.toggle_editor_terminal()  # terminal → editor

    assert controller.centralSurface == "editor"
    assert len(emissions) == 3


# ---------------------------------------------------------------------------
# Focus slot — emits focusTerminalRequested for QML's Connections block.
# ---------------------------------------------------------------------------


def test_focus_terminal_emits_signal(controller):
    """focus_terminal() emits focusTerminalRequested — Main.qml's
    Connections block calls terminalView.forceActiveFocus() on receipt."""
    emissions = _capture(controller.focusTerminalRequested)
    controller.focus_terminal()
    assert len(emissions) == 1


# ---------------------------------------------------------------------------
# Lifecycle — start() pre-warms terminal AFTER nvim; shutdown() reverses.
# ---------------------------------------------------------------------------


def test_start_pre_warms_terminal_after_nvim(patched_backends):
    """start() must call nvim's start() BEFORE the terminal's. Q1-1b:
    nvim ordering gates the QSGRenderThread's first frame; flashing
    an empty editor on slow hardware is the symptom of the wrong order.
    """
    ctrl = AppController()
    try:
        ctrl.start()
    finally:
        ctrl.shutdown()

    assert len(patched_backends["nvim_starts"]) == 1
    assert len(patched_backends["terminal_starts"]) == 1
    # terminal start receives the controller's _cwd (raw, NOT displayedRoot).
    assert patched_backends["terminal_starts"][0] == ctrl._cwd

    # Order: nvim_start MUST come before terminal_start.
    order = patched_backends["call_order"]
    nvim_idx = next(i for i, s in enumerate(order) if s.startswith("nvim_start"))
    term_idx = next(i for i, s in enumerate(order) if s.startswith("terminal_start"))
    assert nvim_idx < term_idx, f"nvim must start before terminal — got order: {order}"


def test_shutdown_stops_terminal_before_nvim(patched_backends):
    """shutdown() must stop the terminal BEFORE nvim. The terminal owns
    a shell process group; killpg reaping must complete on a healthy
    event loop, not one in nvim-teardown handshake."""
    ctrl = AppController()
    ctrl.start()
    # Clear the call order so shutdown's order is what we inspect.
    patched_backends["call_order"].clear()
    ctrl.shutdown()

    order = patched_backends["call_order"]
    term_stop_idx = next(
        i for i, s in enumerate(order) if s.startswith("terminal_stop")
    )
    nvim_stop_idx = next(i for i, s in enumerate(order) if s.startswith("nvim_stop"))
    assert term_stop_idx < nvim_stop_idx, (
        f"terminal must stop before nvim — got order: {order}"
    )


def test_start_oserror_does_not_abort_app(patched_backends, monkeypatch):
    """If the terminal backend's spawn raises OSError (shell missing,
    fd-limit), the IDE must still finish starting up — log and continue,
    don't crash. The user can still use the editor."""

    def raising_start(self, cwd):
        raise OSError("no such file or directory")

    monkeypatch.setattr(TerminalBackend, "start", raising_start)

    ctrl = AppController()
    try:
        # Must NOT raise.
        ctrl.start()
    finally:
        ctrl.shutdown()

    # nvim must still have started — the editor is the usable fallback when
    # the terminal spawn fails. If _backend.start() is ever accidentally
    # moved inside the try block, this catches the regression.
    assert len(patched_backends["nvim_starts"]) == 1, (
        "nvim must start even when terminal OSError fires"
    )


# ---------------------------------------------------------------------------
# QML-facing surface — terminalBackend property exists and returns the
# instance held by the controller.
# ---------------------------------------------------------------------------


def test_terminal_backend_property_returns_instance(controller):
    """The `terminalBackend` Python property is what `_build_engine`
    binds as the QML context property; it must return the same instance
    held internally."""
    backend = controller.terminalBackend
    assert backend is controller._terminal_backend
    assert isinstance(backend, TerminalBackend)
