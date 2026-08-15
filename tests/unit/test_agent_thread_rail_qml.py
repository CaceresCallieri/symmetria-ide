"""Phase-2 source contracts for replacing agent pills with the thread rail.

Chrome QML has no small hermetic construction surface in this repository, so
these tests follow the established source-contract style used for the top bar,
canvas corners, and pane registry.  Comments are stripped before negative
assertions so prose cannot satisfy or defeat a contract.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from qml_source import extract_braced_body

REPO_ROOT = Path(__file__).resolve().parents[2]
QML = REPO_ROOT / "qml"


def _without_comments(source: str) -> str:
    """Blank QML line/block comments while preserving executable text."""
    return re.sub(r"/\*.*?\*/|//[^\n]*", "", source, flags=re.S)


def _source(path: Path) -> str:
    assert path.is_file(), (
        f"missing Phase-2 QML component: {path.relative_to(REPO_ROOT)}"
    )
    return path.read_text(encoding="utf-8")


def _rail() -> str:
    return _source(QML / "AgentThreadRail.qml")


def _keys_handler(source: str) -> str:
    marker = source.find("Keys.onPressed")
    assert marker >= 0, "AgentThreadRail has no Keys.onPressed handler"
    opening = source.find("{", marker)
    assert opening >= 0, "AgentThreadRail's Keys.onPressed is not a block handler"
    return extract_braced_body(source, opening)


def test_top_bar_drops_agent_order_pills_when_rail_is_mounted() -> None:
    """Spec: the new left rail replaces, rather than duplicates, the pills."""
    top_bar = _without_comments(_source(QML / "AgentTopBar.qml"))
    main = _without_comments(_source(QML / "Main.qml"))

    assert "controller.agentOrder" not in top_bar
    assert "id: instanceChips" not in top_bar
    assert "AgentThreadRail" in main


def test_top_bar_keeps_surface_switcher_and_location_toggle() -> None:
    """Guard: moving agent rows does not move either global control."""
    top_bar = _without_comments(_source(QML / "AgentTopBar.qml"))

    for control_id, current, action in (
        (
            "surfaceSwitcher",
            "controller.centralSurface",
            "controller.set_central_surface",
        ),
        ("locationToggle", "controller.location", "controller.set_location"),
    ):
        marker = top_bar.index(f"id: {control_id}")
        declaration = top_bar.rfind("SegmentedControl", 0, marker)
        body = extract_braced_body(top_bar, declaration)
        assert f"current: {current}" in body
        assert action in body


def test_rail_delegate_sources_every_live_pill_indicator_by_slot() -> None:
    """Spec: every status carried by a pill remains visible in its row."""
    rail = _without_comments(_rail())

    # The delegate's only link back to pane state is the integer slot.  Live
    # indicator arrays must keep the established slot-1 addressing discipline.
    for property_name in (
        "agentActivity",
        "agentTitles",
        "agentWorktree",
        "agentBrowserCount",
        "agentBrowserAttention",
        "agentCoordAttention",
    ):
        assert re.search(
            rf"controller\.{property_name}\s*\[\s*[^\]]*slot\s*-\s*1\s*\]",
            rail,
        ), f"rail does not source {property_name} through slot - 1"

    assert "AgentsUI.AgentChip" in rail, "activity sparkle was not moved to the rail"
    assert "sttTargetSlot" in rail and "sttTranscribing" in rail
    assert re.search(r"\bdisplayNumber\s*:\s*(?:\w+\.)?index\s*\+\s*1", rail)
    assert re.search(r"\b(?:sessionTitle|title)\b", rail)
    assert re.search(
        r"(?:\bagentType\s*:[^\n]*\bharness\b|"
        r"\b(?:text|source)\s*:[^\n]*\bharness\b|"
        r"\bharness(?:Glyph|Icon|Mark)\b)",
        rail,
        re.I,
    ), "rail has no rendered harness mark binding"
    assert "Theme.glyph.worktree" in rail


def test_rail_resolves_keyboard_selection_through_the_model() -> None:
    """Regression: recycled delegates cannot dispatch a departed pane slot."""
    rail = _without_comments(_rail())

    current_slot = extract_braced_body(
        rail,
        rail.index("{", rail.index("function _currentSlot")),
    )
    assert re.search(
        r"agentThreads\.slot_at\s*\(\s*threadList\.currentIndex\s*\)",
        current_slot,
    )
    assert "currentItem" not in current_slot


def test_delegate_does_not_require_unused_model_roles() -> None:
    """Regression: a dormant required role must not break delegate creation."""
    rail = _without_comments(_rail())
    marker = rail.index("delegate: Rectangle")
    delegate = extract_braced_body(rail, rail.index("{", marker))
    required_roles = re.findall(
        r"required\s+property\s+\w+\s+(\w+)",
        delegate,
    )

    assert required_roles
    for role in required_roles:
        assert len(re.findall(rf"\b{re.escape(role)}\b", delegate)) > 1, (
            f"delegate requires the unused model role {role!r}"
        )


def test_live_row_click_and_enter_focus_its_slot() -> None:
    """Spec: pointer and primary-key activation both focus the live pane."""
    rail = _without_comments(_rail())

    # A shared activate(slot) helper is preferable to duplicating the call, so
    # require one real controller dispatch and both event paths into an
    # activation-shaped handler rather than requiring two textual copies.
    assert re.search(r"controller\.focus_agent\s*\([^)]*slot[^)]*\)", rail)
    assert re.search(
        r"onClicked\s*:[^\n]*(?:focus_agent|activat|focusThread)", rail, re.I
    )
    assert re.search(
        r"(?:Keys\.onReturnPressed|Keys\.onEnterPressed|Qt\.Key_(?:Return|Enter))",
        rail,
    )
    assert re.search(
        r"(?:Return|Enter).{0,320}(?:focus_agent|activat|focusThread)",
        rail,
        re.I | re.S,
    )


def test_j_and_k_move_rail_selection_without_focusing_an_agent() -> None:
    """Spec: vim row navigation is selection-only until explicit activation."""
    handler = _keys_handler(_without_comments(_rail()))

    assert "Qt.Key_J" in handler and "Qt.Key_K" in handler
    assert re.search(
        r"(?:currentIndex\s*(?:=|\+=|-=)|(?:increment|decrement)CurrentIndex\s*\()",
        handler,
    )
    assert "focus_agent" not in handler


def test_ctrl_h_enters_rail_and_ctrl_l_returns_to_central_surface() -> None:
    """Spec: the rail becomes the left neighbour in spatial focus routing."""
    main = _without_comments(_source(QML / "Main.qml"))

    h_marker = main.index('sequences: ["Ctrl+H"]')
    h_shortcut = main.rfind("Shortcut", 0, h_marker)
    h_body = extract_braced_body(main, h_shortcut)
    l_marker = main.index('sequences: ["Ctrl+L"]')
    l_shortcut = main.rfind("Shortcut", 0, l_marker)
    l_body = extract_braced_body(main, l_shortcut)

    assert re.search(
        r"(?:threadRail\w*\.(?:forceActiveFocus|focus\w*)|_focusThreadRail)\s*\(",
        h_body,
    ), "Ctrl+H from a central surface does not focus the thread rail"
    assert re.search(r"threadRail\w*\.(?:activeFocus|containsFocus)", l_body)
    assert re.search(
        r"(?:_restoreCentralFocus|(?:editor|terminal|focusedTerminal|git\w*)\w*\.forceActiveFocus)\s*\(",
        l_body,
    ), "Ctrl+L from the rail does not restore a visible central surface"


def test_thread_rail_chrome_uses_theme_tokens_not_literal_styling() -> None:
    """Spec: the new delegate obeys the project's Theme-only chrome contract."""
    rail = _without_comments(_rail())

    assert "Theme.color." in rail
    assert "Theme.spacing." in rail
    assert "Theme.font." in rail
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", rail)
    assert not re.search(r"\b(?:rgb|rgba|hsla?)\s*\(", rail)


def test_whole_tree_qmllint_gate_has_no_new_findings() -> None:
    """Guard: Phase 2 cannot hide new QML debt in the checked-in baseline."""
    result = subprocess.run(
        [sys.executable, "scripts/qmllint_gate.py"],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stdout


def test_current_runtime_docs_point_agent_state_to_the_thread_rail() -> None:
    """Regression: operator guidance must not send readers to removed pills."""
    workflow = _source(REPO_ROOT / "docs" / "dev-workflow.md")
    stt_note = _source(
        REPO_ROOT
        / ".claude"
        / "memory"
        / "reference"
        / "host"
        / "stt_chip_hub_broadcast.md"
    )

    assert "thread-rail row" in workflow
    assert "top-bar chip" not in workflow.lower()
    assert "AgentThreadRail" in stt_note
    assert "AgentTopBar STT" not in stt_note
