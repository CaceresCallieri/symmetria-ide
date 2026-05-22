"""Tests for the displayedRoot → nvim `:pwd` sync slot + the
HOME-collapsed `displayedRootCompact` view-layer property.

Wires under test:

- `AppController._sync_nvim_cwd` is connected to `displayedRootChanged`
  and calls `NvimBackend.set_current_dir(displayedRoot)` whenever the
  displayed root changes. Without this, nvim's `:pwd` stays frozen at
  Python's launch-time cwd and file pickers / `:find` / git-from-nvim
  silently target the wrong project after the user cd's around.

- `AppController.displayedRootCompact` is a pure view-layer
  transformation that collapses `$HOME` to `~`. The side-panel header
  binds it; it's a `@Property` over `displayedRootChanged` so the QML
  binding updates whenever the underlying root does.

Hermetic shape: monkeypatch `NvimBackend.set_current_dir` to a capture
function so we exercise the orchestration logic without spawning nvim.
Same pattern as `test_app_controller_central_surface.py` /
`test_app_controller_terminal_osc7.py`.
"""

from __future__ import annotations

import os

import pytest

from symmetria_ide.app import AppController
from symmetria_ide.nvim_backend import NvimBackend


@pytest.fixture
def captured_backend(monkeypatch):
    """Replace NvimBackend.set_current_dir with a list-capturing fake so
    tests can assert what (if anything) the slot pushed to nvim."""
    captured: list[str] = []

    def fake_set_current_dir(self, path: str) -> None:
        captured.append(path)

    monkeypatch.setattr(NvimBackend, "set_current_dir", fake_set_current_dir)
    return captured


@pytest.fixture
def controller():
    ctrl = AppController()
    yield ctrl
    ctrl.shutdown()


# ---------------------------------------------------------------------------
# _sync_nvim_cwd — connected to displayedRootChanged, forwards displayedRoot.
# ---------------------------------------------------------------------------


def test_sync_nvim_cwd_pushes_displayed_root_on_cwd_change(
    captured_backend, controller
):
    """A live cwd update (no anchor) must push the new path to
    nvim. This is the dominant code path — terminal cd → OSC 7 →
    _route_capsule → displayedRootChanged → _sync_nvim_cwd → backend."""
    controller._on_terminal_osc7("/tmp/some-project")
    assert "/tmp/some-project" in captured_backend


def test_sync_nvim_cwd_pushes_cwd_on_release_after_silent_drift(
    captured_backend, controller
):
    """When the IDE is anchored, terminal cwd updates flow into `_cwd`
    silently (no displayedRootChanged, no nvim sync). On release, the
    displayed root flips from the anchored root to whatever `_cwd` is
    now — and nvim MUST catch up to the new displayed root in that
    transition. This is the canonical "I anchored at A, wandered to B
    in the terminal, then released to resume drifting" case; without
    the sync, nvim would still be on A while the file tree and git
    pane have correctly moved to B.

    Validates that the slot reads `displayedRoot` (anchor-aware) rather
    than reacting on raw `_cwd` — the silent drift would otherwise
    trigger spurious nvim moves while anchored."""
    controller._on_terminal_osc7("/tmp/anchored-project")
    controller.anchor_to_current_cwd()
    # Silent drift while anchored — no displayedRootChanged, no sync.
    controller._on_terminal_osc7("/tmp/wandered")
    captured_backend.clear()

    controller.release_anchor()

    # Release flips displayedRoot from `/tmp/anchored-project` →
    # `/tmp/wandered` (the live cwd) — nvim must follow.
    assert "/tmp/wandered" in captured_backend


def test_sync_nvim_cwd_suppressed_while_anchored(captured_backend, controller):
    """While anchored, raw cwd updates do NOT fire displayedRootChanged
    (anchor pins the displayed root). The nvim sync must follow the
    same gate — nvim stays on the anchored root while the user cd's
    around in the terminal. Mirrors `_sync_git_repo_root`'s contract."""
    controller._on_terminal_osc7("/tmp/anchored-project")
    controller.anchor_to_current_cwd()
    captured_backend.clear()

    # Wander away in the terminal — should NOT touch nvim.
    controller._on_terminal_osc7("/tmp/elsewhere")
    assert captured_backend == []


def test_sync_nvim_cwd_no_op_on_empty_displayed_root(captured_backend, controller):
    """`_sync_nvim_cwd` must short-circuit on empty `displayedRoot`.
    Covers the initialization edge case where the signal could fire
    before the first cwd capsule lands. The downstream
    `NvimBackend.set_current_dir` is itself no-op-on-empty, so this
    is belt-and-suspenders defense — but the AppController guard
    means we can assert "nothing was pushed" rather than "the
    push happened but was suppressed at the backend layer."""
    # Force displayedRoot to empty by clearing _cwd and emitting.
    controller._cwd = ""
    captured_backend.clear()

    controller._sync_nvim_cwd()

    assert captured_backend == []


# ---------------------------------------------------------------------------
# displayedRootCompact — HOME-collapse view-layer transform.
# ---------------------------------------------------------------------------


def test_compact_returns_empty_string_for_empty_root(controller):
    """No root set → compact is the empty string. The side-panel
    header binds this; it must not crash on the pre-first-capsule
    transient state."""
    controller._cwd = ""
    assert controller.displayedRootCompact == ""


def test_compact_collapses_home_to_tilde(controller):
    """Paths under $HOME render with `~` prefix."""
    home = os.path.expanduser("~")
    controller._on_terminal_osc7(os.path.join(home, "projects", "foo"))
    assert controller.displayedRootCompact == "~/projects/foo"


def test_compact_returns_home_as_tilde(controller):
    """The exact $HOME path collapses to bare `~` (not `~/`)."""
    home = os.path.expanduser("~")
    controller._on_terminal_osc7(home)
    assert controller.displayedRootCompact == "~"


def test_compact_does_not_collapse_partial_prefix_match(controller):
    """A path that starts with the same characters as $HOME but is
    NOT actually under $HOME (e.g., `/home/jcsomething` when HOME is
    `/home/jc`) must NOT collapse — the `+ os.sep` guard in the
    transform exists exactly for this. Without it, sibling-user dirs
    would silently render as if they were under our HOME."""
    home = os.path.expanduser("~")
    # Construct a sibling path: `<home>foo` instead of `<home>/foo`.
    sibling = home + "foo"
    controller._on_terminal_osc7(sibling)
    # The full path must come through verbatim.
    assert controller.displayedRootCompact == sibling


def test_compact_leaves_non_home_paths_unchanged(controller):
    """Paths outside `$HOME` (e.g., `/tmp/x`, `/opt/y`) render as-is."""
    controller._on_terminal_osc7("/tmp/some-project")
    assert controller.displayedRootCompact == "/tmp/some-project"


def test_compact_fires_change_signal_on_root_update(controller):
    """`displayedRootCompact` is a @Property over `displayedRootChanged`.
    QML bindings re-evaluate on that signal — assert the signal does
    fire on a cwd update so the side-panel header re-binds."""
    emissions: list[None] = []
    controller.displayedRootChanged.connect(lambda: emissions.append(None))

    controller._on_terminal_osc7("/tmp/some-project")

    assert len(emissions) >= 1
