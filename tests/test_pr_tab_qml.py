"""Structural smoke tests for the git surface's PR-tab QML wiring.

QML has no hermetic unit surface here (instantiating GitHistoryView needs a
full QGuiApplication + window), so — like test_browser_surface_qml.py — we
read the files and assert the load-bearing structural pieces are present.
These catch regressions a "refactor the QML" PR could silently introduce:
the PR tab dropped from the mode cycle/header, the context properties no
longer injected, or the checkout confirm gate removed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_QML = Path(__file__).resolve().parent.parent / "qml"


@pytest.fixture(scope="module")
def git_history_view() -> str:
    return (_QML / "githistory" / "GitHistoryView.qml").read_text()


@pytest.fixture(scope="module")
def main_qml() -> str:
    return (_QML / "Main.qml").read_text()


# ---------------------------------------------------------------------------
# GitHistoryView.qml — the third mode's plumbing.
# ---------------------------------------------------------------------------


def test_prs_mode_in_cycle_and_header(git_history_view: str):
    """Tab cycles through "prs" and the tab header renders its segment.

    Asserted as two independent substrings rather than one whole-line match.
    The original form pinned the exact source line -- `{ mode: "prs", label:
    "PRs" }` -- and broke the moment the four hand-rolled tab headers were
    folded into the shared `SegmentedControl` (`mode:` became `key:`, the
    entry went multi-line, and the label gained a live PR count). That was a
    formatting change with no behavioural content, so the failure carried no
    information. Keep assertions here at the level of "the segment exists and
    is spelled this way", never the surrounding punctuation.
    """
    assert '"changes", "history", "prs"' in git_history_view
    assert 'key: "prs"' in git_history_view
    assert '"PRs"' in git_history_view


def test_pr_controllers_injected_as_properties(git_history_view: str):
    """Injected props, never globals — the extraction-ready contract."""
    assert "property var prController: null" in git_history_view
    assert "property var prModel: null" in git_history_view
    assert "property var prTimelineModel: null" in git_history_view


def test_pr_tab_entry_is_lazy_loaded(git_history_view: str):
    """Entering the tab fetches via ensure_loaded (once per root+filter) —
    the manual-freshness decision (no polling, ever)."""
    assert "ensure_loaded()" in git_history_view


def test_pr_checkout_bubbles_to_main(git_history_view: str):
    assert "signal prCheckoutRequested(int number, string branch)" in git_history_view


# ---------------------------------------------------------------------------
# Main.qml — injection + confirm gate + toast.
# ---------------------------------------------------------------------------


def test_main_injects_pr_context_properties(main_qml: str):
    assert "prController: ghPrController" in main_qml
    assert "prModel: ghPrListModel" in main_qml
    assert "prTimelineModel: ghPrTimelineModel" in main_qml


def test_main_has_pr_checkout_confirm_dialog(main_qml: str):
    """Checkout moves HEAD + the working tree — it must stay behind the
    deliberate-Enter ConfirmDialog like push."""
    assert "prCheckoutDialog" in main_qml
    assert "ghPrController.checkout(" in main_qml


def test_main_wires_checkout_toast(main_qml: str):
    """Checkout feedback rides the shared git-ops toast via the re-emitted
    operationStarted/operationFinished contract."""
    assert "target: ghPrController" in main_qml
