"""Tests for the Phase 2.5 central-surface state machine on AppController.

Covers the swap-state machine introduced in PR 4 (plus the swap-last-two
back-navigation added later):

- `_central_surface` is the source of truth
  ("terminal" | "editor" | "agent" | "git" | "browser").
- `centralSurface`, `editorVisible`, `terminalVisible` are derived
  `@Property` over the same notify signal — they can never drift.
- `swap_to_*` slots are idempotent (no spurious signal on repeat).
- `_previous_surface` + `_surface_back_target` drive back-navigation:
  a surface chord pressed on its own surface returns to where you came
  from, falling back to terminal when that surface is gone/emptied.
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


# ---------------------------------------------------------------------------
# Subprocess-free fixtures — patch start/stop on the RPC backend so a bare
# AppController() + start() + shutdown() cycle is hermetic. After the
# qmltermwidget migration the editor + shell are spawned by QMLTermSessions
# in Main.qml (not Python TerminalBackends), so start()/shutdown() touch only
# the nvim RPC client on the Python side.
# ---------------------------------------------------------------------------


@pytest.fixture
def patched_backends(monkeypatch):
    """Make NvimBackend.start/stop no-op, with capture lists so tests can
    assert the RPC client is brought up / torn down."""
    nvim_starts: list[None] = []
    nvim_stops: list[None] = []
    call_order: list[str] = []

    def fake_nvim_start(self):
        nvim_starts.append(None)
        call_order.append(f"nvim_start#{len(call_order)}")

    def fake_nvim_stop(self):
        nvim_stops.append(None)
        call_order.append(f"nvim_stop#{len(call_order)}")

    monkeypatch.setattr(NvimBackend, "start", fake_nvim_start)
    monkeypatch.setattr(NvimBackend, "stop", fake_nvim_stop)

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
    """Pressing the toggle while on the editor returns to the PREVIOUS surface
    — terminal here, because that's where we came from (swap_to_editor from the
    default terminal). The common single-step case of the swap-last-two model;
    test_editor_toggle_returns_to_previous_non_terminal covers the case where
    'back' is NOT the terminal."""
    controller.swap_to_editor()  # precondition: start on editor (from terminal)
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
# Surface back-navigation — single previous-pointer (swap-last-two / alt-tab).
#
# A surface chord pressed while already on its named surface returns to the
# surface you CAME FROM (`_previous_surface`), not a hard-coded terminal. The
# pointer is recorded at the one funnel `set_central_surface`, so it tracks
# every transition regardless of which chord/auto-switch caused it.
# ---------------------------------------------------------------------------


def test_previous_surface_starts_none(controller):
    """No history before the first transition — back-target falls back to the
    terminal home base in that case (see _surface_back_target)."""
    assert controller._previous_surface is None
    assert controller._surface_back_target() == "terminal"


def test_set_central_surface_records_previous(controller):
    """The funnel records where we came from BEFORE updating — that's what
    every back-toggle reads. One place to track, so history is correct no
    matter how the surface was reached."""
    controller.set_central_surface("editor")
    assert controller._previous_surface == "terminal"
    controller.set_central_surface("git")
    assert controller._previous_surface == "editor"


def test_noop_transition_does_not_pollute_history(controller):
    """Re-selecting the current surface is a no-op (returns before recording),
    so a spurious repeat press can't overwrite the real previous pointer."""
    controller.set_central_surface("editor")  # prev = terminal
    controller.set_central_surface("editor")  # no-op
    assert controller._previous_surface == "terminal"


def test_editor_toggle_returns_to_previous_non_terminal(controller):
    """The headline behavior: Ctrl+Shift+E pressed on the editor returns to the
    PREVIOUS surface (git here), not the terminal. This is the whole point of
    the feature — the chord no longer always dumps you on the terminal."""
    controller.toggle_git_history()  # terminal → git (forward)
    controller.toggle_editor_terminal()  # git → editor (forward)
    controller.toggle_editor_terminal()  # editor → BACK to git, not terminal
    assert controller.centralSurface == "git"


def test_git_toggle_returns_to_previous_non_terminal(controller):
    """Symmetric to the editor case: Ctrl+Shift+G on the git surface returns to
    the previous surface (editor here), not the terminal."""
    controller.toggle_editor_terminal()  # terminal → editor (forward)
    controller.toggle_git_history()  # editor → git (forward)
    controller.toggle_git_history()  # git → BACK to editor, not terminal
    assert controller.centralSurface == "editor"


def test_terminal_home_forward_lands_on_terminal(controller):
    """Ctrl+Shift+T from any other surface still goes to the terminal (the
    home base's forward direction is unchanged)."""
    controller.toggle_editor_terminal()  # terminal → editor
    controller.toggle_terminal_home()  # editor → terminal (forward)
    assert controller.centralSurface == "terminal"


def test_terminal_home_toggles_back_to_previous(controller):
    """Per the user's choice, Ctrl+Shift+T ALSO gained back-on-repeat: pressed
    while already on the terminal it returns to the previous surface, so the
    terminal is no longer a privileged forced-fallback."""
    controller.toggle_editor_terminal()  # terminal → editor (prev = terminal)
    controller.toggle_terminal_home()  # editor → terminal (prev = editor)
    controller.toggle_terminal_home()  # terminal → BACK to editor
    assert controller.centralSurface == "editor"


def test_swap_last_two_ping_pong(controller):
    """Single-pointer semantics: with editor + git as the two most-recent
    surfaces, each back-press swaps between them (vim Ctrl-^ / alt-tab),
    rather than unwinding a deeper trail."""
    controller.toggle_editor_terminal()  # terminal → editor
    controller.toggle_git_history()  # editor → git
    controller.toggle_git_history()  # git → editor (back)
    assert controller.centralSurface == "editor"
    controller.toggle_editor_terminal()  # editor → git (back)
    assert controller.centralSurface == "git"
    controller.toggle_git_history()  # git → editor (back)
    assert controller.centralSurface == "editor"


def test_agent_surface_is_a_valid_back_target(controller):
    """Agents are special only for their OWN chord (Ctrl+Shift+A opens the
    spawn menu). The agent surface still participates as a back DESTINATION:
    arriving at git from the agent surface, Ctrl+Shift+G returns to agent.

    A live agent is required for the surface to be navigable (an empty agent
    pool is rejected by _surface_is_navigable — see the emptied-pool tests
    below), so seed one directly into the pool list."""
    controller._agent_order.append(1)  # one agent is live → surface navigable
    controller.set_central_surface("agent")  # terminal → agent
    controller.toggle_git_history()  # agent → git (forward)
    controller.toggle_git_history()  # git → BACK to agent
    assert controller.centralSurface == "agent"


def test_back_target_skips_emptied_agent_surface(controller):
    """Regression (seal review of 8c5d4ee): closing the agent the back-pointer
    names must NOT strand the user on a blank agent surface. You arrive at git
    from the agent surface (prev="agent"), then the last agent closes — the
    back-toggle must fall back to the terminal, not the empty agent pane.

    The pool list is mutated directly (no subprocess) because that is exactly
    what _surface_is_navigable reads — an empty _agent_order IS "the agent the
    pointer named has since closed"."""
    controller.set_central_surface("agent")  # terminal → agent
    controller._agent_order.append(1)  # an agent is live while we're here
    controller.set_central_surface("git")  # agent → git (prev = "agent")
    controller._agent_order.clear()  # ...then that agent closes
    assert controller._surface_back_target() == "terminal"
    controller.toggle_git_history()  # git → back, NOT the empty agent surface
    assert controller.centralSurface == "terminal"


def test_back_target_skips_emptied_browser_surface(controller):
    """Browser counterpart of test_back_target_skips_emptied_agent_surface: a
    back-pointer to the browser surface whose window pool has since emptied
    falls back to the terminal rather than a blank browser pane."""
    controller.set_central_surface("browser")  # terminal → browser
    controller._browser_order.append(1)  # a window is open while we're here
    controller.set_central_surface("git")  # browser → git (prev = "browser")
    controller._browser_order.clear()  # ...then the only window closes
    assert controller._surface_back_target() == "terminal"
    controller.toggle_git_history()  # git → back, NOT the empty browser surface
    assert controller.centralSurface == "terminal"


def test_emptied_pool_guard_does_not_overfire_while_occupied(controller):
    """The navigability guard must reject ONLY emptied pools — while the agent
    pool is still occupied, the back-pointer to the agent surface is honored
    (a guard that always rejected agent/browser would silently re-break the
    swap-last-two behavior for those surfaces)."""
    controller.set_central_surface("agent")
    controller._agent_order.append(1)
    controller.set_central_surface("git")  # prev = "agent", pool still occupied
    assert controller._surface_back_target() == "agent"


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


def test_start_attaches_rpc_only(patched_backends):
    """start() after the qmltermwidget migration: BOTH the editor nvim and the
    shell are spawned by QMLTermSessions in Main.qml (at engine-load time,
    before start()). So start() itself only attaches the RPC client to nvim's
    --listen socket — no Python TerminalBackend is launched for either pane."""
    ctrl = AppController()
    try:
        ctrl.start()
    finally:
        ctrl.shutdown()

    # The RPC client (chrome relay over nvim's socket) is the one thing start()
    # brings up on the Python side — both panes are spawned by QMLTermSessions.
    assert len(patched_backends["nvim_starts"]) == 1


def test_shutdown_quits_nvim_rpc_gracefully(patched_backends):
    """shutdown() must call the nvim RPC client's stop() (graceful `qa!`) so
    nvim writes shada/swap cleanly. After the qmltermwidget migration there is
    no Python-side terminal killpg backstop — the QMLTermSessions reap their
    children when the QML engine tears down — so the RPC quit is the only
    Python-side shutdown step for the editor."""
    ctrl = AppController()
    ctrl.start()
    patched_backends["call_order"].clear()
    ctrl.shutdown()

    order = patched_backends["call_order"]
    assert any(s.startswith("nvim_stop") for s in order), (
        f"shutdown must stop the nvim RPC client — got: {order}"
    )
    assert len(patched_backends["nvim_stops"]) == 1


# ---------------------------------------------------------------------------
# send_editor_keys — thin passthrough slot for chord relay
# ---------------------------------------------------------------------------


def test_send_editor_keys_delegates_to_backend(monkeypatch):
    """send_editor_keys(keys) must pass the keycode string straight through
    to NvimBackend.input. This is a thin wrapper slot that lets QML call
    the RPC control channel without importing NvimBackend directly.

    Verified by patching NvimBackend.input and asserting the call arrives
    with the correct argument.
    """
    received: list[str] = []

    def fake_input(self, keys: str) -> None:
        received.append(keys)

    monkeypatch.setattr(NvimBackend, "input", fake_input)

    ctrl = AppController()
    try:
        ctrl.send_editor_keys("<C-2>")
        ctrl.send_editor_keys("<C-S-q>")
    finally:
        ctrl.shutdown()

    assert received == ["<C-2>", "<C-S-q>"]


def test_send_editor_keys_empty_string_is_noop(monkeypatch):
    """NvimBackend.input guards against empty strings (returns early on
    ``not keys``). send_editor_keys must propagate empty strings to that
    guard — calling input("") should NOT inject a key into nvim.

    This pins the pass-through contract: any input validation lives in
    NvimBackend.input, not duplicated in AppController.
    """
    received: list[str] = []

    def fake_input(self, keys: str) -> None:
        received.append(keys)

    monkeypatch.setattr(NvimBackend, "input", fake_input)

    ctrl = AppController()
    try:
        ctrl.send_editor_keys("")
    finally:
        ctrl.shutdown()

    # NvimBackend.input will be called with "" but will return early
    # without injecting — the assert here pins that AppController passes
    # through rather than silently filtering, so the guard lives in one place.
    assert received == [""]
