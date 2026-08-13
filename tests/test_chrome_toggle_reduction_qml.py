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

import re
from pathlib import Path

import pytest

_QML = Path(__file__).resolve().parent.parent / "qml"

# Private-use-area blocks, ALL THREE of them. The BMP block alone is the
# tempting shorthand and it is not enough: Nerd Font's Material Design set
# (`nf-md-*`, one of the rejected icon candidates) lives in Supplementary
# PUA-A at U+F0001 and up, so a BMP-only guard would wave through exactly the
# glyph family this project came closest to using.
_PUA_RANGES = ((0xE000, 0xF8FF), (0xF0000, 0xFFFFD), (0x100000, 0x10FFFD))


def _literal_pua(text: str) -> list[str]:
    return [
        f"U+{ord(c):04X}"
        for c in text
        if any(low <= ord(c) <= high for low, high in _PUA_RANGES)
    ]


def _segment_entry(source: str, key: str) -> str | None:
    """The `{ key: "...", ... }` entry for `key`, or None.

    Matched with tolerant whitespace on purpose. Pinning the exact rendered
    line is what broke test_pr_tab_qml's PRs assertion during the
    SegmentedControl extraction, and these entries are near the wrap column
    already — a reflow must not read as a behavioural regression.
    """
    match = re.search(rf'\{{\s*key:\s*"{re.escape(key)}"\s*,(.*?)\}}', source, re.S)
    return match.group(1) if match else None


@pytest.fixture(scope="module")
def agent_top_bar() -> str:
    return (_QML / "AgentTopBar.qml").read_text()


@pytest.fixture(scope="module")
def git_history_view() -> str:
    return (_QML / "githistory" / "GitHistoryView.qml").read_text()


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
        entry = _segment_entry(agent_top_bar, key)
        assert entry is not None, f"no segment entry for {key!r}"
        assert re.search(rf'label:\s*"{label}"', entry)
        assert re.search(rf"icon:\s*{re.escape(token)}", entry)


def test_surface_glyph_tokens_are_escapes_not_literal_pua(theme: str):
    """Theme stores the four marks as \\u escapes.

    Asserted on the raw source text: reading the escaped FORM is the whole
    point, so a test that compared resolved characters would pass on exactly
    the encoding this rule exists to prevent.

    Scoped to Theme.qml deliberately. The rule is "glyphs SHARED across chrome
    live in Theme, as escapes" — several files (AgentTopBar's globe, Toast's
    status marks, Main.qml) still carry one-off literals, so widening this
    scan repo-wide would fail on existing code rather than protect anything.
    Those are candidates for migration INTO Theme, not for this assertion.
    """
    for token in ("\\uf120", "\\uea73", "\\uec10", "\\uea68"):
        assert token in theme

    pua = _literal_pua(theme)
    assert not pua, f"literal private-use-area characters in Theme.qml: {pua}"


def test_location_toggle_stays_fully_labelled(agent_top_bar: str):
    """local/vps keeps both words — no icon a reader would guess, and it is
    the seed of a future N-machine dropdown rather than a cycling label.

    The `icon:` negative is asserted inside the matched ENTRY, not against the
    whole file — AgentTopBar legitimately contains four of them, in the other
    control. (An earlier draft sliced the file between the two controls' `id`s
    to prove the same negative, which would have raised IndexError the day
    someone reordered them — the brittleness this module argues against.)
    """
    for key, label in (("local", "Local"), ("vps", "VPS")):
        entry = _segment_entry(agent_top_bar, key)
        assert entry is not None, f"no segment entry for {key!r}"
        assert re.search(rf'label:\s*"{label}"', entry)
        assert "icon:" not in entry


# ---------------------------------------------------------------------------
# SegmentedControl — the icon contract itself.
# ---------------------------------------------------------------------------


def test_label_hides_only_when_the_segment_has_an_icon(segmented_control: str):
    """An icon-less segment must always draw its label — otherwise the two-way
    switchers would render as blank pills."""
    assert re.search(
        r"iconGlyph:\s*segment\.modelData\.icon\s*\|\|\s*\"\"", segmented_control
    )
    assert re.search(
        r'showLabel:\s*\n?\s*segment\.iconGlyph\s*===\s*""\s*\|\|\s*segment\.isCurrent',
        segmented_control,
    )


def test_label_returns_when_no_segment_is_current(segmented_control: str):
    """`centralSurface` has a fifth value, "browser", that the surface switcher
    carries no segment for. With nothing current, an icon-bearing control would
    otherwise draw as bare glyphs with no label and no highlight — saying less
    than the fully-labelled control it replaced."""
    assert re.search(r"\|\|\s*!root\.hasCurrentSegment", segmented_control)
    # Asserted POSITIVELY — that the property is a binding block containing a
    # loop — rather than negatively against `.some(`. The negative form was
    # tried and failed on this very file: the string appears in the comment
    # explaining why `.some` was avoided. A negative source assertion matches
    # prose as readily as code.
    assert re.search(
        r"readonly property bool hasCurrentSegment:\s*\{(?:.|\n)*?for\s*\(",
        segmented_control,
    )


def test_icons_render_in_a_declared_font_not_a_context_property(
    segmented_control: str,
):
    """PUA glyphs need a Nerd Font family or every mark is a tofu box, and the
    binding must not reach for the `editorFontFamily` context property
    directly: an unqualified access is a P0 violation in project-standards,
    and it would make this shared component un-instantiable in any engine that
    does not set one."""
    match = re.search(
        r"id:\s*segmentIcon(?:.|\n)*?font\.family:\s*([\w.]+)", segmented_control
    )
    assert match, "segmentIcon has no font.family binding"
    assert match.group(1) in {"root.iconFontFamily", "Theme.font.family"}


# ---------------------------------------------------------------------------
# Changes-scope switcher — drawn only when it can mean something.
# ---------------------------------------------------------------------------


def test_scope_switcher_hidden_without_a_focused_agent(git_status_panel: str):
    """Both clauses are load-bearing: the first hides the inert control, the
    second is the way back when the pool empties while scope is "agent"."""
    assert re.search(
        r'visible:\s*root\.focusedAgentSlot\s*>\s*0\s*\|\|\s*root\.scope\s*===\s*"agent"',
        git_status_panel,
    )


def test_scope_switcher_keeps_both_labels(git_status_panel: str):
    for key, label in (("all", "All"), ("agent", "This agent")):
        entry = _segment_entry(git_status_panel, key)
        assert entry is not None, f"no segment entry for {key!r}"
        assert re.search(rf'label:\s*"{label}"', entry)
        assert "icon:" not in entry


# ---------------------------------------------------------------------------
# Git tab header — the decision NOT to apply the icon treatment.
# ---------------------------------------------------------------------------


def test_git_tabs_keep_every_label(git_history_view: str):
    """The third decision from this module's docstring, and the one most
    exposed to a "make the switchers consistent" refactor: every tab here
    carries live data in its label (pending count, checked-out ref, open-PR
    count) whose whole purpose is to be readable WITHOUT switching to the tab.
    Active-only labelling would trade that data for width."""
    segments = re.search(r"segments:\s*\[(.*?)\n\s*\]", git_history_view, re.S)
    assert segments, "GitHistoryView has no segments array"
    assert "icon:" not in segments.group(1)
