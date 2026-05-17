"""Unit tests for `AppController`'s project-anchor state machine.

Covers the three-field state machine the AppController exposes to QML:

- `_cwd`, `_anchored`, `_anchored_root` are the source of truth.
- `displayedRoot` is a derived `@Property` (anchored_root when anchored,
  else cwd) — never a stored field, so anchor state and displayed path
  can never drift apart.
- `displayedRootChanged` emits only on *effective* changes — anchor /
  release transitions, or cwd updates while NOT anchored.
- Idempotent transitions (anchor again to the same path, release while
  already released) are no-ops and emit nothing.
- The git controller is connected to `displayedRootChanged` (not raw
  `cwdChanged`), so git operations follow the anchored root.

Spike-validation contract: this file IS the safety net for the
load-bearing conditional emit in `_route_capsule`. If a future refactor
regresses anchor → cwd-changes-internally → release → next-cwd-change-
propagates, the `test_anchor_holds_against_cwd_change` +
`test_release_restores_cwd_tracking` pair fails fast.

Hermetic shape: no QML engine, no nvim subprocess. Tests poke
`_route_capsule` directly with synthetic capsule payloads to simulate
what the nvim worker thread would deliver — same pattern the dispatch
tests use against nvim_events.
"""

from __future__ import annotations

import pytest

from symmetria_ide.app import AppController


@pytest.fixture
def controller():
    """Construct a bare `AppController`.

    No session-host swap needed — anchor logic doesn't touch the agent
    pool. `shutdown()` cleans up the nvim backend (which never `start()`d
    its worker thread, so this is a near-instant tear-down).
    """
    ctrl = AppController()
    yield ctrl
    ctrl.shutdown()


def _capture(signal) -> list[None]:
    """Capture every emission of a parameterless signal as a list of None.

    Length of the returned list is the emission count. We don't capture
    the post-emit value here because `displayedRoot` and `anchored` are
    snapshot-able via property reads at the assertion site — keeping the
    capture simple makes test failures more legible.
    """
    emissions: list[None] = []
    signal.connect(lambda: emissions.append(None))
    return emissions


def _push_cwd(ctrl: AppController, path: str) -> None:
    """Simulate one `cwd` capsule arriving from runtime/init.lua."""
    ctrl._route_capsule({"id": "cwd", "label": "", "value": path})


# ---------------------------------------------------------------------------
# Initial state + property exposure
# ---------------------------------------------------------------------------


def test_initial_state_not_anchored(controller):
    """Fresh controller is unanchored; displayedRoot tracks cwd."""
    assert controller.anchored is False
    # The __init__ seed is $HOME (see app.py:410); displayedRoot mirrors it.
    assert controller.displayedRoot == controller.cwd


def test_displayed_root_tracks_cwd_when_unanchored(controller):
    """cwd updates flow through to displayedRoot when not anchored."""
    _push_cwd(controller, "/tmp/a")
    assert controller.cwd == "/tmp/a"
    assert controller.displayedRoot == "/tmp/a"


# ---------------------------------------------------------------------------
# Anchor / release transitions
# ---------------------------------------------------------------------------


def test_anchor_to_current_cwd_sets_state_and_emits(controller):
    """Anchoring sets `anchored=True` and fires both signals exactly once."""
    _push_cwd(controller, "/projects/foo")
    anchored_emissions = _capture(controller.anchoredChanged)
    displayed_emissions = _capture(controller.displayedRootChanged)

    controller.anchor_to_current_cwd()

    assert controller.anchored is True
    assert controller.displayedRoot == "/projects/foo"
    assert len(anchored_emissions) == 1
    # No displayedRootChanged: cwd and anchor target are the same path,
    # so the EFFECTIVE displayed root didn't change. Spurious emit here
    # would cause a needless git rescan.
    assert displayed_emissions == []


def test_anchor_to_different_path_emits_displayed_root_changed(controller):
    """Anchoring to a path different from cwd flips the effective root."""
    _push_cwd(controller, "/tmp")
    displayed_emissions = _capture(controller.displayedRootChanged)

    controller.anchor_to_path("/projects/bar")

    assert controller.displayedRoot == "/projects/bar"
    assert len(displayed_emissions) == 1


def test_anchor_empty_path_is_no_op(controller):
    """Empty path is rejected — anchoring to nothing leaves an unrecoverable UI."""
    anchored_emissions = _capture(controller.anchoredChanged)

    controller.anchor_to_path("")

    assert controller.anchored is False
    assert anchored_emissions == []


def test_anchor_to_same_path_is_idempotent(controller):
    """Re-anchoring to the current anchored root fires nothing."""
    controller.anchor_to_path("/projects/foo")
    anchored_emissions = _capture(controller.anchoredChanged)
    displayed_emissions = _capture(controller.displayedRootChanged)

    controller.anchor_to_path("/projects/foo")

    assert anchored_emissions == []
    assert displayed_emissions == []


def test_anchor_to_different_path_while_anchored_only_emits_displayed(controller):
    """Switching anchor target shifts displayedRoot but `anchored` stays True."""
    controller.anchor_to_path("/projects/a")
    anchored_emissions = _capture(controller.anchoredChanged)
    displayed_emissions = _capture(controller.displayedRootChanged)

    controller.anchor_to_path("/projects/b")

    # anchored True throughout — no transition, no emit.
    assert anchored_emissions == []
    assert controller.anchored is True
    assert controller.displayedRoot == "/projects/b"
    assert len(displayed_emissions) == 1


def test_release_anchor_clears_state_and_emits(controller):
    """Release flips `anchored` back to False; displayedRoot reverts to cwd."""
    _push_cwd(controller, "/tmp")
    controller.anchor_to_path("/projects/foo")
    anchored_emissions = _capture(controller.anchoredChanged)
    displayed_emissions = _capture(controller.displayedRootChanged)

    controller.release_anchor()

    assert controller.anchored is False
    assert controller.displayedRoot == "/tmp"
    assert len(anchored_emissions) == 1
    assert len(displayed_emissions) == 1


def test_release_when_not_anchored_is_no_op(controller):
    """Release on an already-released controller fires nothing."""
    anchored_emissions = _capture(controller.anchoredChanged)
    displayed_emissions = _capture(controller.displayedRootChanged)

    controller.release_anchor()

    assert anchored_emissions == []
    assert displayed_emissions == []


def test_release_with_matching_cwd_does_not_emit_displayed(controller):
    """When anchored_root == cwd, releasing doesn't change displayedRoot.

    The release transition still fires `anchoredChanged` (the boolean
    state DID change), but `displayedRootChanged` is suppressed because
    the effective root is unchanged. Defense against a spurious git
    rescan on the common "anchor where you stood" → "release" sequence.
    """
    _push_cwd(controller, "/projects/foo")
    controller.anchor_to_current_cwd()
    anchored_emissions = _capture(controller.anchoredChanged)
    displayed_emissions = _capture(controller.displayedRootChanged)

    controller.release_anchor()

    assert len(anchored_emissions) == 1
    assert displayed_emissions == []


# ---------------------------------------------------------------------------
# Load-bearing: anchor holds against cwd churn, release re-syncs
# ---------------------------------------------------------------------------


def test_anchor_holds_against_cwd_change(controller):
    """While anchored, cwd updates flow into `_cwd` silently — no UI emit.

    This is the canary for the conditional emit at app.py's `_route_capsule`
    cwd branch. Regression here breaks the anchor's whole purpose.
    """
    _push_cwd(controller, "/projects/foo")
    controller.anchor_to_current_cwd()
    displayed_emissions = _capture(controller.displayedRootChanged)

    _push_cwd(controller, "/tmp/wandering")

    # _cwd updated (so a later release re-syncs cleanly)...
    assert controller.cwd == "/tmp/wandering"
    # ...but displayedRoot stays pinned and no signal fired.
    assert controller.displayedRoot == "/projects/foo"
    assert displayed_emissions == []


def test_release_restores_cwd_tracking(controller):
    """After release, the next cwd update fires displayedRootChanged again.

    Pairs with the test above: together they exercise the full
    anchor → silent-cwd-update → release → next-cwd-propagates loop.
    """
    _push_cwd(controller, "/projects/foo")
    controller.anchor_to_current_cwd()
    _push_cwd(controller, "/tmp/wandering")  # silent under anchor
    controller.release_anchor()
    displayed_emissions = _capture(controller.displayedRootChanged)

    _push_cwd(controller, "/var/log")

    assert controller.displayedRoot == "/var/log"
    assert len(displayed_emissions) == 1


# ---------------------------------------------------------------------------
# Defense-in-depth: malformed anchored_root falls back to cwd
# ---------------------------------------------------------------------------


def test_displayed_root_falls_back_to_cwd_when_anchored_root_empty(controller):
    """If `_anchored=True` but `_anchored_root==""`, displayedRoot returns cwd.

    `anchor_to_path` rejects empty strings, so this state is unreachable
    via the public API — but the property's empty-guard is documented
    defense-in-depth against future code paths that might set the fields
    directly (tests, migrations, debugger sessions).
    """
    _push_cwd(controller, "/tmp")
    controller._anchored = True
    controller._anchored_root = ""

    assert controller.displayedRoot == "/tmp"
