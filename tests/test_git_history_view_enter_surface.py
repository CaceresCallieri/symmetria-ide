"""Structural smoke tests for the git surface's entry-time tab selection.

When the git central surface (Ctrl+Shift+G) becomes visible it picks its
default sub-view from the live worktree state: "changes" when the tree is
dirty, "history" when it's clean — so a clean tree doesn't strand the user on
an empty changes pane. The choice lives in `GitHistoryView.enterSurface()`,
which Main.qml wires to `onVisibleChanged`.

QML has no hermetic unit-test surface in this codebase (instantiating the
component needs a full QGuiApplication + window), so we use the same
read-the-file / assert-the-structural-pieces pattern as
`test_main_qml_terminal_wiring.py`. These tests catch the regressions a
"refactor / simplify" pass could silently introduce:
  - enterSurface() losing its dirty-tree branch (always lands on one tab),
  - the visibility hook reverting to the old always-focusContent() behavior
    (clean tree opens on an empty changes pane again),
  - the Ctrl+H focus-restore path getting "consistently" swapped to
    enterSurface() too — which would re-pick the mode and yank the user out
    of a sub-view they deliberately toggled to (it must stay focusContent()).
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def git_history_view_qml() -> str:
    """Load GitHistoryView.qml once per module."""
    repo_root = Path(__file__).resolve().parent.parent
    return (repo_root / "qml" / "githistory" / "GitHistoryView.qml").read_text()


@pytest.fixture(scope="module")
def main_qml() -> str:
    """Load Main.qml once per module."""
    repo_root = Path(__file__).resolve().parent.parent
    return (repo_root / "qml" / "Main.qml").read_text()


# ---------------------------------------------------------------------------
# GitHistoryView — the entry-time tab selection lives here
# ---------------------------------------------------------------------------


def test_enter_surface_function_exists(git_history_view_qml: str):
    """The host calls enterSurface() on show; it must exist as a function."""
    assert "function enterSurface()" in git_history_view_qml


def test_enter_surface_picks_changes_when_dirty_else_history(git_history_view_qml: str):
    """enterSurface() must branch on the dirty-tree predicate, landing on
    "changes" when there's pending work and "history" otherwise. Both arms
    must be present so a refactor can't collapse it to a single fixed tab."""
    assert 'hasPendingChanges ? "changes" : "history"' in git_history_view_qml


def test_dirty_tree_predicate_derives_from_status_count(git_history_view_qml: str):
    """hasPendingChanges is the single source of truth for "tree is dirty" and
    must derive from the status model's file count (null-guarded, since
    statusModel is injected late by the host)."""
    assert "readonly property bool hasPendingChanges" in git_history_view_qml
    assert "root.statusModel != null" in git_history_view_qml
    assert "root.statusModel.count > 0" in git_history_view_qml


# ---------------------------------------------------------------------------
# Main.qml — the wiring that drives the selection, and the path that must NOT
# ---------------------------------------------------------------------------


def test_visibility_hook_calls_enter_surface(main_qml: str):
    """The git surface's onVisibleChanged must call enterSurface() (the
    entry-time re-pick), not the old focusContent() (which left a clean tree
    on an empty changes pane)."""
    assert "onVisibleChanged: if (visible) enterSurface()" in main_qml


def test_focus_restore_path_stays_focus_content(main_qml: str):
    """The Ctrl+H focus-restore branch must re-home focus onto the already-open
    git surface WITHOUT re-picking the mode — i.e. it stays focusContent(),
    never enterSurface(). Swapping it would discard a deliberately toggled
    sub-view every time focus returns from the tree."""
    assert "gitHistoryView.focusContent()" in main_qml
