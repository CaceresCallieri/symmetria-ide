"""The rail's "how long" — where each slot's busy/idle clock comes from.

`AppController.agentTiming` publishes one `{busySince, idleSince}` pair per
slot, and the whole feature rests on one invariant: exactly ONE of the pair is
non-zero, and which one IS the state. There is no third field recording whether
the agent is working, so a stamp written without zeroing its twin would leave
the rail unable to tell "working for 3h" from "replied 3h ago" — the two
readings the user is asking the row to distinguish.

The stamps live on the `_term_agents` record rather than inside the activity
dict, and that placement is load-bearing enough to be tested: both snapshot
handlers REBUILD `_term_agent_activity` wholesale from payloads that carry no
timestamps, so a stamp stored there would be erased by the next bridge frame.

Hermetic: a bare `AppController()` needs no subprocesses (start/stop are not
called), matching the `controller` fixture in
test_app_controller_sidebar_visibility.py.
"""

from __future__ import annotations

import pytest

from symmetria_ide.app import AppController


@pytest.fixture
def controller():
    """Bare controller — no start()/stop(), so no real backends spawn."""
    ctrl = AppController()
    yield ctrl
    ctrl.shutdown()


def _seed_slot(ctrl: AppController, slot: int, *, busy_since: int, idle_since: int):
    """A pool record with the two fields spawn_agent writes, and nothing else.

    Deliberately not a call to `spawn_agent`: that path starts a real agent
    CLI. Everything under test reads only these two keys plus presence in
    `_term_agent_activity`, so the minimal record is the honest fixture.

    The `_sync_pane_slots()` is NOT optional padding — every per-slot
    `QVariantList` is sized by the pane registry's high-water mark, not by
    `_term_agents`, so a record seeded without it publishes an EMPTY list and
    the assertions below fail on an index rather than on a value.
    """
    ctrl._term_agents[slot] = {"busy_since": busy_since, "idle_since": idle_since}
    ctrl._sync_pane_slots()
    return ctrl._term_agents[slot]


def _mark_busy(ctrl: AppController, slot: int) -> None:
    """Busy-ness is PRESENCE in the activity dict, not the `state` string.

    The clear path POPS the entry rather than writing an idle state into it,
    so a reader keying on `state == ""` cannot tell idle from never-reported.
    """
    ctrl._term_agent_activity[slot] = {
        "state": "working",
        "tool": "Edit",
        "agentType": "claude",
    }


# ---------------------------------------------------------------------------
# The invariant


def test_going_busy_starts_the_busy_clock_and_clears_the_idle_one(controller) -> None:
    record = _seed_slot(controller, 1, busy_since=0, idle_since=1_000)

    _mark_busy(controller, 1)
    controller._stamp_activity_edges()

    assert record["busy_since"] > 0
    assert record["idle_since"] == 0


def test_going_idle_starts_the_idle_clock_and_clears_the_busy_one(controller) -> None:
    record = _seed_slot(controller, 1, busy_since=1_000, idle_since=0)

    controller._term_agent_activity.pop(1, None)
    controller._stamp_activity_edges()

    assert record["idle_since"] > 0
    assert record["busy_since"] == 0


def test_a_steady_state_never_moves_the_clock(controller) -> None:
    """The stamp answers "since when", so a re-publish must not restart it.

    Activity re-publishes constantly — every tool change emits — and a stamp
    that moved on each one would pin every working agent at "now" forever,
    which is the failure that makes the duration worthless rather than wrong.
    """
    record = _seed_slot(controller, 1, busy_since=1_000, idle_since=0)
    _mark_busy(controller, 1)

    for _ in range(3):
        controller._stamp_activity_edges()

    assert record["busy_since"] == 1_000


def test_exactly_one_stamp_is_set_through_a_whole_busy_idle_cycle(controller) -> None:
    """The invariant every reader depends on, asserted at each step."""
    record = _seed_slot(controller, 1, busy_since=0, idle_since=1_000)

    def live_one() -> int:
        return sum(1 for key in ("busy_since", "idle_since") if record[key])

    assert live_one() == 1
    _mark_busy(controller, 1)
    controller._stamp_activity_edges()
    assert live_one() == 1
    controller._term_agent_activity.pop(1, None)
    controller._stamp_activity_edges()
    assert live_one() == 1
    _mark_busy(controller, 1)
    controller._stamp_activity_edges()
    assert live_one() == 1


# ---------------------------------------------------------------------------
# Isolation between slots


def test_one_slot_flipping_leaves_the_others_alone(controller) -> None:
    """The stamper walks every record, so a shared clock would be easy to miss.

    Both snapshot handlers replace the whole activity dict at once, which means
    the stamper is routinely called with several slots' states in flight.
    """
    busy = _seed_slot(controller, 1, busy_since=1_000, idle_since=0)
    idle = _seed_slot(controller, 2, busy_since=0, idle_since=2_000)
    _mark_busy(controller, 1)

    _mark_busy(controller, 2)
    controller._stamp_activity_edges()

    assert busy["busy_since"] == 1_000, "an untouched slot's clock restarted"
    assert idle["busy_since"] > 0
    assert idle["idle_since"] == 0


# ---------------------------------------------------------------------------
# The published surface


def test_the_published_list_is_indexed_by_slot_minus_one(controller) -> None:
    """`agentTiming[slot - 1]`, the same indexing every per-slot list uses.

    Every rail indicator reads its list this way, so a timing list that
    numbered differently would silently show one agent's clock on another's
    row — which reads as a wrong duration, not as a wrong agent.
    """
    _seed_slot(controller, 1, busy_since=111, idle_since=0)
    _seed_slot(controller, 2, busy_since=0, idle_since=222)

    timing = controller.agentTiming

    assert len(timing) >= 2
    assert timing[0] == {"busySince": 111, "idleSince": 0}
    assert timing[1] == {"busySince": 0, "idleSince": 222}


def test_a_slot_with_no_record_publishes_zeroes(controller) -> None:
    """A freed slot must read as "no clock", never as the last agent's.

    Slot numbers are REUSED — the pane registry frees a slot rather than
    deleting its row — so the gap between one agent leaving and the next
    arriving is a real state the rail renders. Zero is what the formatter
    turns into an empty string.
    """
    _seed_slot(controller, 2, busy_since=0, idle_since=222)

    timing = controller.agentTiming

    assert timing[0] == {"busySince": 0, "idleSince": 0}


def test_the_emit_funnel_stamps_before_it_publishes(controller) -> None:
    """QML must never see an activity change ahead of its own timestamp.

    The two travel on ONE signal (`agentActivityChanged`), so a listener that
    ran between the emit and the stamp would read the new state against the
    previous state's clock. Asserting the order here is what lets every call
    site simply call the funnel.
    """
    record = _seed_slot(controller, 1, busy_since=0, idle_since=1_000)
    _mark_busy(controller, 1)
    seen: list[tuple[int, int]] = []
    controller.agentActivityChanged.connect(
        lambda: seen.append((record["busy_since"], record["idle_since"]))
    )

    controller._emit_activity_changed()

    assert len(seen) == 1
    busy_at_emit, idle_at_emit = seen[0]
    assert busy_at_emit > 0 and idle_at_emit == 0
