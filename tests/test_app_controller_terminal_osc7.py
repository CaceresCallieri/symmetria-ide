"""Tests for the Phase 2.5 deliverable 3 PR 3 OSC 7 → AppController routing.

`AppController._on_terminal_osc7(path)` is the bridge between the
terminal reader thread's parsed cwd announcements and the existing
`cwd` capsule pipeline. The whole point of routing through
`_route_capsule` with a synthetic dict is that every downstream
consumer (anchor state machine, file tree, git controller) sees the
update through its existing wires — no parallel routing tree.

This test module proves that contract end-to-end at the AppController
layer: emit `osc7_received` (or call the slot directly), observe that
`_cwd` updates, `cwdChanged` fires, and the anchor gate correctly
suppresses `displayedRootChanged` when anchored.

Hermetic shape: no Qt event loop, no TerminalBackend subprocess. Direct
slot invocation + signal-capture assertions, mirroring the pattern in
`test_anchor_state.py`.
"""

from __future__ import annotations

import inspect

import pytest

from symmetria_ide.app import AppController


@pytest.fixture
def controller():
    """Bare controller. shutdown() cleans up the nvim backend
    (never started, so near-instant tear-down)."""
    ctrl = AppController()
    yield ctrl
    ctrl.shutdown()


def _capture(signal) -> list[object]:
    """Capture emission count for a parameterless signal."""
    emissions: list[object] = []
    signal.connect(lambda: emissions.append(None))
    return emissions


# ---------------------------------------------------------------------------
# Slot behavior — direct invocation
# ---------------------------------------------------------------------------


def test_osc7_updates_cwd(controller):
    """Calling `_on_terminal_osc7(path)` with a new path must update
    `_cwd` to match. This is the most basic contract — without it,
    nothing else downstream of the cwd capsule pipeline updates."""
    controller._on_terminal_osc7("/tmp")
    assert controller._cwd == "/tmp"
    assert controller.cwd == "/tmp"  # @Property exposure


def test_osc7_emits_cwd_changed(controller):
    """The base `cwdChanged` signal fires on every successful update.
    Any consumer that explicitly binds to raw cwd (a future terminal
    pane's new-tab cwd, the git controller pre-anchor-refactor) sees
    terminal-driven updates via this signal."""
    emissions = _capture(controller.cwdChanged)
    controller._on_terminal_osc7("/tmp")
    assert len(emissions) == 1


def test_osc7_no_op_on_same_path(controller):
    """If the path matches the current `_cwd`, neither signal fires.
    Matches the `_route_capsule` guard `if new_cwd != self._cwd`. A
    PROMPT_COMMAND that emits the OSC 7 hook on every prompt without
    actually changing directory must NOT churn the signal."""
    controller._on_terminal_osc7("/tmp")
    cwd_emissions = _capture(controller.cwdChanged)
    displayed_emissions = _capture(controller.displayedRootChanged)

    controller._on_terminal_osc7("/tmp")

    assert cwd_emissions == []
    assert displayed_emissions == []


def test_osc7_emits_displayed_root_changed_when_unanchored(controller):
    """When not anchored, `displayedRoot` tracks `_cwd` — so a cwd
    update from the terminal must fire `displayedRootChanged` and
    propagate to the file tree binding."""
    emissions = _capture(controller.displayedRootChanged)
    controller._on_terminal_osc7("/tmp")
    assert len(emissions) == 1
    assert controller.displayedRoot == "/tmp"


# ---------------------------------------------------------------------------
# Anchor invariant — terminal-driven cwd updates honor the anchor gate
# ---------------------------------------------------------------------------


def test_osc7_does_not_emit_displayed_root_changed_when_anchored(controller):
    """LOAD-BEARING: this is the keystone test for deliverable 3's
    integration with the anchor state machine.

    Once anchored, terminal-driven cwd updates must update `_cwd`
    silently (so a later release re-syncs cleanly to the latest path)
    but MUST NOT fire `displayedRootChanged` — the file tree / git
    controller stay pinned to the anchored root. This is identical
    behavior to what nvim's `:cd` does today, achieved by routing
    through the same `_route_capsule` code path.
    """
    controller._on_terminal_osc7("/projects/foo")
    controller.anchor_to_current_cwd()

    cwd_emissions = _capture(controller.cwdChanged)
    displayed_emissions = _capture(controller.displayedRootChanged)

    controller._on_terminal_osc7("/tmp/wandering")

    # Raw cwd updates — anchor doesn't gate cwdChanged.
    assert controller._cwd == "/tmp/wandering"
    assert len(cwd_emissions) == 1
    # Displayed root stays pinned — anchor IS gating displayedRootChanged.
    assert controller.displayedRoot == "/projects/foo"
    assert displayed_emissions == []


def test_osc7_release_after_terminal_walk_resyncs_to_latest(controller):
    """Companion to the test above: after `release_anchor`, the LATEST
    cwd update (the one the terminal walked to while anchored) must
    drive the next displayedRoot. Without this, the file tree would
    snap back to the anchored root on release — wrong; the user wants
    'where am I right now' to win after release."""
    controller._on_terminal_osc7("/projects/foo")
    controller.anchor_to_current_cwd()
    controller._on_terminal_osc7("/tmp/wandering")  # silent under anchor

    emissions = _capture(controller.displayedRootChanged)
    controller.release_anchor()

    assert len(emissions) == 1
    assert controller.displayedRoot == "/tmp/wandering"


# ---------------------------------------------------------------------------
# Defensive — empty / malformed payloads
# ---------------------------------------------------------------------------


def test_osc7_empty_path_dropped(controller):
    """`_parse_osc7` filters root-only OSC 7 (`file:///`) at the parser
    layer, so an empty string never normally arrives here. But a future
    parser change or a direct slot call (e.g. from tests) could pass
    `""` — the slot must guard against that to avoid corrupting `_cwd`
    with an empty string."""
    initial_cwd = controller._cwd
    controller._on_terminal_osc7("")
    assert controller._cwd == initial_cwd  # unchanged


# ---------------------------------------------------------------------------
# Signal wiring — the connect site uses QueuedConnection per §4 P2
# ---------------------------------------------------------------------------


def test_osc7_connect_uses_queued_connection():
    """§4 P2: terminal reader thread → AppController GUI thread is a
    cross-thread emit, so the connect MUST specify Qt.QueuedConnection
    explicitly with a grep-able comment at the connect site. Searches
    the full source of `AppController` so the test survives if the
    connect call moves to a helper method."""
    src = inspect.getsource(AppController)
    assert "terminal_backend.osc7_received" in src, (
        "osc7_received connect missing from AppController"
    )
    # The connect line must specify Qt.ConnectionType.QueuedConnection —
    # without it, Qt auto-picks based on sender/receiver thread affinity,
    # which can flip to direct if the QObject parenting changes.
    osc_idx = src.find("osc7_received.connect")
    assert osc_idx >= 0
    # Look in the ~200-char window after the connect for the queued marker.
    window = src[osc_idx : osc_idx + 300]
    assert "Qt.ConnectionType.QueuedConnection" in window, (
        "osc7_received connect MUST specify QueuedConnection (§4 P2)"
    )


def test_osc7_slot_signature_accepts_str():
    """The slot must accept a single `str` argument matching
    `osc7_received = Signal(str)`. PySide6 raises TypeError at emit
    time if the slot signature mismatches, but catching it at test
    time via signature inspection means we don't have to spawn a
    Qt event loop to verify the contract."""
    sig = inspect.signature(AppController._on_terminal_osc7)
    params = list(sig.parameters.values())
    # self + path
    assert len(params) == 2
    assert params[1].name == "path"
    # The annotation is `str` (PySide6's Signal(str) → Python str).
    # `from __future__ import annotations` in app.py makes the
    # annotation a string literal at module-load time; compare against
    # the string form (or use typing.get_type_hints for the resolved
    # form, but that's heavier and overkill for a structural test).
    assert params[1].annotation in (str, "str")


# ---------------------------------------------------------------------------
# Initialization invariant — _cwd seeds from launch dir, not home dir
# ---------------------------------------------------------------------------


def test_initial_cwd_is_launch_dir(tmp_path, monkeypatch):
    """AppController._cwd must be initialized to `os.getcwd()` (the launch
    directory), NOT `os.path.expanduser("~")`.

    This invariant is load-bearing for the terminal pre-warm path:
    `AppController.start()` calls `_terminal_backend.start(self._cwd)`
    synchronously, before nvim's VimEnter capsule can update `_cwd`. Whatever
    placeholder sits in `_cwd` at that moment is what the embedded shell
    process inherits as its starting directory. Using `~` instead of the
    launch dir would land the terminal in the wrong project until the first
    OSC 7 or nvim cwd capsule arrives.
    """
    # Simulate the user running `cd /some/project && python -m symmetria_ide`
    monkeypatch.chdir(tmp_path)
    ctrl = AppController()
    try:
        assert ctrl._cwd == str(tmp_path), (
            f"_cwd should be the launch dir {tmp_path!r}, got {ctrl._cwd!r}. "
            "If this regresses, the terminal pre-warm in AppController.start() "
            "will inherit the wrong starting directory."
        )
    finally:
        ctrl.shutdown()
