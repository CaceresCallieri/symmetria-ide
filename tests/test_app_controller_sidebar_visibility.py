"""Tests for the responsive + manually-toggled file-tree sidebar.

`AppController.treeVisible` is the single source of truth bound by every
sidebar consumer (separator, FocusScope, StatusBar column, Ctrl+L focus
guard). It is derived as the AND of two independent inputs:

  1. `_tree_user_visible` — user intent, flipped by `toggle_tree()` (Ctrl+S).
  2. `_window_width >= SIDEBAR_MIN_WINDOW_WIDTH` — a responsive gate fed by
     `set_window_width()` (QML reports the live window width).

The two emit-paths (`set_window_width`, `toggle_tree`) must fire
`treeVisibleChanged` ONLY when the *derived* boolean actually flips, so
narrow-window auto-hide and a manual toggle that the width gate already
overrides don't thrash the QML bindings or trigger spurious focus recovery.

Hermetic: a bare `AppController()` needs no subprocesses (start/stop are not
called), matching the `controller` fixture in
test_app_controller_central_surface.py.
"""

from __future__ import annotations

import pytest

from symmetria_ide.app import AppController, SIDEBAR_MIN_WINDOW_WIDTH


@pytest.fixture
def controller():
    """Bare controller — no start()/stop(), so no real backends spawn."""
    ctrl = AppController()
    yield ctrl
    ctrl.shutdown()


def _capture(signal) -> list[None]:
    """Capture emission count for a parameterless signal."""
    emissions: list[None] = []
    signal.connect(lambda: emissions.append(None))
    return emissions


# ---------------------------------------------------------------------------
# Default state — the constructor seeds width to 1280 (> threshold), so the
# sidebar is visible before QML reports a real width.
# ---------------------------------------------------------------------------


def test_sidebar_visible_by_default(controller):
    assert controller.treeVisible is True


# ---------------------------------------------------------------------------
# Responsive width gate.
# ---------------------------------------------------------------------------


def test_narrow_window_hides_sidebar(controller):
    """Below the threshold the sidebar auto-hides, emitting exactly once."""
    emissions = _capture(controller.treeVisibleChanged)
    controller.set_window_width(SIDEBAR_MIN_WINDOW_WIDTH - 100)
    assert controller.treeVisible is False
    assert len(emissions) == 1


def test_widening_above_threshold_restores_sidebar(controller):
    """Narrow → wide flips visibility back on, emitting once per flip."""
    controller.set_window_width(SIDEBAR_MIN_WINDOW_WIDTH - 100)  # hidden
    emissions = _capture(controller.treeVisibleChanged)
    controller.set_window_width(SIDEBAR_MIN_WINDOW_WIDTH + 100)  # shown
    assert controller.treeVisible is True
    assert len(emissions) == 1


def test_threshold_is_inclusive(controller):
    """Exactly at the threshold the sidebar is visible (>=), one px below
    it is hidden — pins the boundary so a future `>` typo is caught."""
    controller.set_window_width(SIDEBAR_MIN_WINDOW_WIDTH)
    assert controller.treeVisible is True
    controller.set_window_width(SIDEBAR_MIN_WINDOW_WIDTH - 1)
    assert controller.treeVisible is False


def test_width_change_without_flip_emits_nothing(controller):
    """A resize that stays on the same side of the threshold must not emit —
    a window drag fires per-pixel; the bindings only care about crossings."""
    emissions = _capture(controller.treeVisibleChanged)
    controller.set_window_width(1500)  # still wide → still visible
    controller.set_window_width(1100)  # still wide → still visible
    assert controller.treeVisible is True
    assert emissions == []


def test_same_width_is_noop(controller):
    """Reporting the current width early-returns before any work."""
    emissions = _capture(controller.treeVisibleChanged)
    controller.set_window_width(1280.0)  # == constructor seed
    assert emissions == []


# ---------------------------------------------------------------------------
# Manual toggle (Ctrl+S) — flips user intent; width gate still dominates.
# ---------------------------------------------------------------------------


def test_toggle_hides_when_wide(controller):
    """Wide window: toggle hides the sidebar, emitting once."""
    emissions = _capture(controller.treeVisibleChanged)
    controller.toggle_tree()
    assert controller.treeVisible is False
    assert len(emissions) == 1


def test_toggle_round_trip_restores(controller):
    """Two toggles return to the original visible state."""
    controller.toggle_tree()  # hidden
    controller.toggle_tree()  # shown
    assert controller.treeVisible is True


def test_toggle_overridden_by_narrow_width(controller):
    """The 'easily overridden' contract: while the window is narrow the
    auto-hide dominates, so toggling user intent does NOT re-show the
    sidebar and emits nothing (derived visibility never flips)."""
    controller.set_window_width(SIDEBAR_MIN_WINDOW_WIDTH - 100)  # auto-hidden
    assert controller.treeVisible is False
    emissions = _capture(controller.treeVisibleChanged)
    controller.toggle_tree()  # intent flips True→False, but width still wins
    assert controller.treeVisible is False
    assert emissions == []


def test_user_hidden_intent_persists_across_widening(controller):
    """Pins the pure-AND semantics: if the user toggled intent off, widening
    the window does NOT auto-re-show — intent and the width gate are
    independent. Documented as intentional; a future settings panel may
    revisit the precedence."""
    controller.toggle_tree()  # wide → intent off → hidden
    assert controller.treeVisible is False
    controller.set_window_width(2000)  # very wide, but intent is still off
    assert controller.treeVisible is False
