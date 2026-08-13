"""Structural tests for the 2026-08-13 toggle-reduction decisions.

QML has no hermetic unit surface for chrome (instantiating AgentTopBar needs a
full QGuiApplication + window), so — like test_pr_tab_qml.py — these read the
sources and assert the load-bearing pieces.

What they protect is a set of DECISIONS, not an implementation. Each one looks
like an inconsistency to a future reader tidying the chrome, which is exactly
why an assertion is cheaper than a comment alone:

  * the surface switcher shows an icon per surface and names only the current
    one, while its three sibling SegmentedControls stay fully labelled;
  * the changes-scope switcher is drawn only when a focused agent exists, or
    when the scope is already "agent" so the way back survives;
  * the git tab header keeps every label, because each carries a live count.

A "make the switchers consistent" refactor would quietly undo all three.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_QML = Path(__file__).resolve().parent.parent / "qml"


@pytest.fixture(scope="module")
def agent_top_bar() -> str:
    return (_QML / "AgentTopBar.qml").read_text()


@pytest.fixture(scope="module")
def git_status_panel() -> str:
    return (_QML / "GitStatusPanel.qml").read_text()


@pytest.fixture(scope="module")
def segmented_control() -> str:
    return (_QML / "SegmentedControl.qml").read_text()


@pytest.fixture(scope="module")
def theme() -> str:
    return (_QML / "design" / "Theme.qml").read_text()


# ---------------------------------------------------------------------------
# Surface switcher — icon per surface, label for the active one only.
# ---------------------------------------------------------------------------


def test_every_surface_segment_carries_a_theme_glyph(agent_top_bar: str):
    """All four surfaces take their mark from Theme, never a local literal.

    A literal PUA character in this file would be invisible in review AND is
    the exact failure Theme's glyph block documents: one arrived through an
    edit pipeline as an empty string and rendered nothing, with no warning.
    """
    for key, label, token in (
        ("terminal", "Terminal", "Theme.glyph.surface.terminal"),
        ("editor", "Editor", "Theme.glyph.surface.editor"),
        ("agent", "Agents", "Theme.glyph.surface.agent"),
        ("git", "Git", "Theme.glyph.surface.git"),
    ):
        assert f'{{ key: "{key}", label: "{label}", icon: {token} }}' in agent_top_bar


def test_surface_glyph_tokens_are_escapes_not_literal_pua(theme: str):
    """Theme stores the four marks as \\u escapes.

    Asserted on the raw source text: reading the escaped FORM is the whole
    point, so a test that compared resolved characters would pass on exactly
    the encoding this rule exists to prevent.
    """
    for token in ("\\uf120", "\\uea73", "\\uec10", "\\uea68"):
        assert token in theme

    pua = [c for c in theme if 0xE000 <= ord(c) <= 0xF8FF]
    assert not pua, f"literal private-use-area characters in Theme.qml: {pua!r}"


def test_location_toggle_stays_fully_labelled(agent_top_bar: str):
    """local/vps keeps both words — no icon a reader would guess, and it is
    the seed of a future N-machine dropdown rather than a cycling label.

    The absence of an icon needs no separate assertion: these are whole-entry
    matches, so adding an `icon:` key would break them. (An earlier draft
    sliced the file between the two controls' `id`s to prove the negative,
    which would have raised IndexError the day someone reordered them —
    the same brittleness this module's docstring argues against.)
    """
    assert '{ key: "local", label: "Local" }' in agent_top_bar
    assert '{ key: "vps", label: "VPS" }' in agent_top_bar


# ---------------------------------------------------------------------------
# SegmentedControl — the icon contract itself.
# ---------------------------------------------------------------------------


def test_label_hides_only_when_the_segment_has_an_icon(segmented_control: str):
    """An icon-less segment must always draw its label — otherwise the two-way
    switchers would render as blank pills."""
    assert (
        'readonly property string iconGlyph: segment.modelData.icon || ""'
        in segmented_control
    )
    assert 'segment.iconGlyph === "" || segment.isCurrent' in segmented_control


def test_icons_render_in_the_nerd_font_not_the_ui_font(segmented_control: str):
    """The chrome UI font has no PUA glyphs; the mark would be a tofu box."""
    assert "font.family: editorFontFamily" in segmented_control


# ---------------------------------------------------------------------------
# Changes-scope switcher — drawn only when it can mean something.
# ---------------------------------------------------------------------------


def test_scope_switcher_hidden_without_a_focused_agent(git_status_panel: str):
    """Both clauses are load-bearing: the first hides the inert control, the
    second is the way back when the pool empties while scope is "agent"."""
    assert (
        'visible: root.focusedAgentSlot > 0 || root.scope === "agent"'
        in git_status_panel
    )


def test_scope_switcher_keeps_both_labels(git_status_panel: str):
    assert '{ key: "all", label: "All" }' in git_status_panel
    assert '{ key: "agent", label: "This agent" }' in git_status_panel
