"""Tests for the surface-swap behavior of `AppController.open_in_nvim`.

When the user activates a file from any of the three IDE surfaces (the
sidebar file tree, the Ctrl+E file manager picker via `pick_in_nvim`,
or the git status panel), the central surface must swap to the editor
so the user can actually SEE the file. Activating a file while the
terminal pane is the visible central surface used to silently load the
buffer in an unseen nvim — the swap eliminates that surprise.

Hermetic shape: no Qt event loop, no nvim subprocess. The default
`AppController()` starts with `_central_surface == "terminal"` and
`_backend._nvim is None`, so `_backend.edit_file(path)` is a no-op
returning before any async_call attempt — we can observe the surface
state transition without mocking anything. Same fixture shape as
`test_app_controller_terminal_osc7.py`.
"""

from __future__ import annotations

import pytest

from symmetria_ide.app import AppController


@pytest.fixture
def controller():
    """Bare controller. `shutdown()` is near-instant because the nvim
    backend was never started."""
    ctrl = AppController()
    yield ctrl
    ctrl.shutdown()


def _capture(signal) -> list[object]:
    emissions: list[object] = []
    signal.connect(lambda: emissions.append(None))
    return emissions


# ---------------------------------------------------------------------------
# Surface swap on file open
# ---------------------------------------------------------------------------


def test_open_in_nvim_from_terminal_swaps_to_editor(controller):
    """Default surface is "terminal"; opening a file must move us to
    "editor" so the user sees the buffer they just opened."""
    assert controller._central_surface == "terminal"
    controller.open_in_nvim("/some/path.py")
    assert controller._central_surface == "editor"


def test_open_in_nvim_from_terminal_emits_central_surface_changed(controller):
    """The swap must fire exactly one `centralSurfaceChanged` so QML's
    visibility bindings on `editorVisible` / `terminalVisible` re-bind.
    Without the emission, the editor pane stays hidden even though
    `_central_surface` flipped — a silent UI bug."""
    emissions = _capture(controller.centralSurfaceChanged)
    controller.open_in_nvim("/some/path.py")
    assert len(emissions) == 1


def test_open_in_nvim_idempotent_when_editor_already_central(controller):
    """Opening a file while the editor is already visible must NOT
    re-emit `centralSurfaceChanged`. The dominant case — file tree
    clicked while editing — should never churn QML bindings or trigger
    spurious focus side-effects. Relies on `swap_to_editor`'s
    no-op-on-noop guard at app.py:1322."""
    controller.swap_to_editor()  # transition once: terminal → editor
    emissions = _capture(controller.centralSurfaceChanged)
    controller.open_in_nvim("/some/path.py")
    assert emissions == []


def test_open_in_nvim_empty_path_does_not_swap(controller):
    """An empty path is the early-return guard. The swap must NOT happen
    on a no-op call — accidentally invoking `open_in_nvim("")` (e.g.
    from a QML binding that hasn't resolved yet) must not yank the
    user into the editor for no reason."""
    emissions = _capture(controller.centralSurfaceChanged)
    controller.open_in_nvim("")
    assert controller._central_surface == "terminal"
    assert emissions == []


def test_open_in_nvim_swaps_before_calling_backend(controller, monkeypatch):
    """The surface swap must happen BEFORE the nvim edit_file marshal,
    so the editor pane is mounted + repainting by the time nvim's :edit
    flush reaches the GUI thread. Reversed order would let the file
    arrive in an unmounted pane, then snap into view with the buffer
    already loaded — a visible pop instead of a smooth transition.

    The order is observable: monkeypatch `edit_file` to record the
    surface state + the received path at the moment it's called; assert
    that state is already "editor" and the path round-trips intact when
    the backend call lands."""
    observed: list[tuple[str, str]] = []

    def fake_edit_file(path: str) -> None:
        observed.append((controller._central_surface, path))

    monkeypatch.setattr(controller._backend, "edit_file", fake_edit_file)
    assert controller._central_surface == "terminal"
    controller.open_in_nvim("/some/path.py")
    assert observed == [("editor", "/some/path.py")]


def test_pick_in_nvim_inherits_swap_via_delegation(controller):
    """`pick_in_nvim` is a thin wrapper that calls `open_in_nvim` plus
    `hide_fm`. The swap-to-editor behavior must propagate through the
    wrapper — file manager activations from terminal surface should
    also swap. This pins the single-funnel contract."""
    assert controller._central_surface == "terminal"
    controller.pick_in_nvim("/some/path.py")
    assert controller._central_surface == "editor"
