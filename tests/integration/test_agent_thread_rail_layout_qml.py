"""Out-of-process Qt Quick geometry for the thread rail's two-line rows.

Asserts RENDERED geometry, not QML source. Every way the second line can be
wrong is silent — a zero-height line looks like a tight row, an overlapping
anchor chain draws text over text, and a binding loop only prints a warning —
so reading the source back would prove nothing that a typo could not also pass.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PROBE = REPO_ROOT / "tests" / "qml_harness" / "thread_rail_layout_probe.py"


def _probe() -> dict:
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    completed = subprocess.run(
        [sys.executable, str(PROBE)],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, (
        f"thread-rail layout probe exited {completed.returncode}\n"
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    return json.loads(completed.stdout)


def _rows() -> list[dict]:
    return _probe()["rows"]


def test_every_row_shows_its_age_in_the_right_form() -> None:
    """One duration per row state, and one per branch of the formatter.

    The probe's four rows are stamped 22 minutes busy, 3 hours idle, 10
    seconds idle, and 2 days since a dead thread last moved, against a pinned
    clock — so the assertions below also pin the minute/hour/day thresholds.
    """
    working, idle, fresh, dead = _rows()

    assert working["ageText"] == "22m"
    assert working["working"] is True

    assert idle["ageText"] == "3h"
    assert idle["working"] is False

    # Under a minute prints a WORD, not "0m". An agent that has just answered
    # is the commonest moment for the user to look at the rail, and a zero
    # there reads as a broken counter rather than as "moments ago".
    assert fresh["ageText"] == "now"

    # A dead row has no slot to read a stamp from, so its age comes off the
    # ROW's own `updatedAt`. Getting this from the same formatter is the point:
    # the rail must not print a live agent's age and a dead thread's age in two
    # different shapes.
    assert dead["ageText"] == "2d"
    assert dead["working"] is False


def test_rows_are_uniform_height_and_the_second_line_sits_below_the_first() -> None:
    """The stack is real, and no indicator can change a row's height.

    The uniformity assert is the one with history: line one was first sized to
    the max of the title and the indicator cluster, which made a row exactly
    ONE PIXEL taller while it owned a browser window — so rows twitched as
    agents opened and closed windows.
    """
    rows = _rows()

    heights = {row["height"] for row in rows}
    assert len(heights) == 1, f"rows disagree on height: {heights}"

    # Taller than the single-line row this replaced (~26px), which is the
    # visible half of the change: the second line has to have somewhere to go.
    assert heights.pop() > 30

    for row in rows:
        title, age = row["title"], row["age"]
        assert title is not None and age is not None
        # Strictly below, with no overlap: the age's top must clear the
        # title's bottom edge.
        assert age["y"] >= title["y"] + title["h"], row


def test_threads_are_separated_by_more_air_than_their_two_lines_are() -> None:
    """The rail is read by a CONTRAST, so assert the ratio, not a pixel count.

    A two-line row is a group, and a group only reads as one if the gap around
    it beats the gap inside it. The failure this guards is subtle enough to
    have shipped once: line two is empty on the LEFT for a row with no
    worktree, so down the left column the titles sat at an even pitch and the
    whole rail read as a list of single lines.

    Measured against the TEXT, not the delegate boxes — the row's own padding
    is part of the air the reader sees, and comparing box edges would credit
    the gap with padding the eye reads as belonging to the row.
    """
    rows = _rows()

    inner = {
        round(row["age"]["y"] - (row["title"]["y"] + row["title"]["h"]), 1)
        for row in rows
    }
    assert len(inner) == 1, f"rows disagree on their internal gap: {inner}"
    within = inner.pop()

    between = [
        round(
            (later["y"] + later["title"]["y"])
            - (earlier["y"] + earlier["age"]["y"] + earlier["age"]["h"]),
            1,
        )
        for earlier, later in zip(rows, rows[1:], strict=False)
    ]
    assert between, "need at least two rows to have a gap at all"
    assert len(set(between)) == 1, f"rows are unevenly spaced: {between}"

    assert between[0] >= within * 4, (
        f"threads are separated by {between[0]}px but their own lines by "
        f"{within}px — too close to read as separate threads"
    )


def test_the_active_row_is_marked_by_a_border_and_a_recess() -> None:
    """Two marks for one state, and the pair is the point.

    The active row used to be told by a LIGHTER fill and by the title's colour
    rung. The fill now aliases the canvas rung, which is DARKER than the rail
    behind it — it reads as a window onto the surface that thread is on, and
    it is only about three lightness units away, so it cannot carry the state
    alone. The border is what actually announces the row.

    Both halves are asserted because either one alone silently degrades: drop
    the border and the state is at the threshold of visible, lighten the fill
    "to restore contrast" and it stops meaning anything.
    """
    probe = _probe()
    rows = probe["rows"]
    active = [row for row in rows if row["focused"]]
    assert len(active) == 1, "the probe is meant to focus exactly one row"

    assert active[0]["borderWidth"] == 1.0
    # Zero, never a transparent 1px border: a border of any width insets the
    # fill, so an inactive row would paint a pixel smaller than its active
    # neighbour and the list would breathe as focus moved.
    assert all(row["borderWidth"] == 0.0 for row in rows if not row["focused"])

    # RECESSED, not raised. This is the assertion that catches the tempting
    # wrong fix.
    assert active[0]["fill"] is not None
    assert active[0]["fill"] < probe["railGround"], (
        f"the active row's fill ({active[0]['fill']}) is not darker than the "
        f"rail behind it ({probe['railGround']})"
    )

    # An inactive row paints NOTHING and shows the rail through. A second
    # opaque fill would compete with the active one at whatever rung it chose.
    assert all(row["fill"] is None for row in rows if not row["focused"])


def test_the_age_is_right_aligned_across_every_row() -> None:
    """One right edge for every row, whatever else the row is showing.

    A ragged right edge is what a column of durations must not have — it is
    the difference between a list you can scan down and three separate labels.
    """
    right_edges = {round(row["age"]["x"] + row["age"]["w"], 1) for row in _rows()}
    assert len(right_edges) == 1, f"ages are not aligned: {right_edges}"


def test_the_worktree_name_renders_beside_the_age_only_when_there_is_one() -> None:
    """The worktree moved onto line two, in words rather than as a bare glyph.

    The absent case is asserted too, because the probe matches VISIBLE text
    only: an invisible Text still reports a real width, so a row with no
    worktree that still rendered one would otherwise pass unnoticed.
    """
    working, idle, fresh, dead = _rows()

    assert working["worktree"] is not None
    assert dead["worktree"] is not None
    assert idle["worktree"] is None, "a row with no worktree drew one anyway"
    assert fresh["worktree"] is None, "a row with no worktree drew one anyway"

    # On the same line as the age, and clearing it — the name elides against
    # the age's left edge rather than running under it.
    for row in (working, dead):
        assert row["worktree"]["y"] == row["age"]["y"], row
        assert row["worktree"]["x"] + row["worktree"]["w"] <= row["age"]["x"], row
