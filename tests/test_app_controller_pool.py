"""Phase A pool-shape coverage — per-instance dispatch + focus + routing.

Phase A's invisible refactor replaces scalar `_session_host` /
`_session_model` / `_awaiting_response` / `_permission_mode` with
slot-keyed dicts. This file pins the new surfaces:

- `focus_instance(int)` — sets `_focused_instance`, emits the three
  QML-facing signals (focusedInstance, awaitingResponse, permissionMode),
  no-ops on unknown slots, no-ops on no-change.
- `cycle_permission_mode_for(int)` — indexed cycle dispatch routes to
  the named slot's host.
- `submit_prompt_for(prompt, int)` — indexed submit dispatch routes to
  the named slot's host AND model.
- `respond_to_permission` request_id routing — lookups in
  `_pending_permissions` find the issuing slot regardless of focus.
- `_create_instance(slot)` — idempotent on slot reuse, emits
  instanceCountChanged once per real allocation.

All tests use the same `_FakeSessionHost` swap pattern as
`test_app_controller_awaiting.py` and `test_app_controller_permission_mode.py`.
The fake takes `instance_index` so we can construct N fakes and
distinguish them in assertions.
"""

from __future__ import annotations

import pytest

from symmetria_ide.app import AppController
from symmetria_ide.session_models import SessionModel
from tests.conftest import (
    FakeSessionHost as _FakeSessionHost,
)  # canonical fake — see conftest.py


@pytest.fixture
def controller():
    """AppController with slot 1's host swapped for a fake.

    Slot 1 is the only instance in Phase A. Tests that exercise
    Phase B's multi-slot routing manually call
    `_create_instance(slot)` and then swap in additional fakes.
    """
    ctrl = AppController()
    ctrl._session_hosts[1] = _FakeSessionHost(instance_index=1)  # type: ignore[assignment]
    yield ctrl
    ctrl.shutdown()


def _add_slot(ctrl: AppController, slot: int) -> _FakeSessionHost:
    """Add a fake host at `slot` and return it.

    Bypasses the real `_create_instance` (which would spawn a real
    SessionHost we'd then have to swap out) by populating the pool
    dicts directly. Wires NO connections — these tests don't need
    the queued event delivery; they call slots and read state
    synchronously.
    """
    fake = _FakeSessionHost(instance_index=slot)
    ctrl._session_hosts[slot] = fake  # type: ignore[assignment]
    ctrl._session_models[slot] = SessionModel(ctrl, instance_index=slot)
    ctrl._awaiting_response[slot] = False
    ctrl._permission_mode[slot] = "default"
    return fake


# ---------------------------------------------------------------------------
# Initial pool shape
# ---------------------------------------------------------------------------


def test_initial_pool_has_one_slot(controller):
    """Slot 1 is allocated by `__init__` — the pool starts at size 1."""
    assert list(controller._session_hosts.keys()) == [1]
    assert controller.instanceCount == 1
    assert controller.focusedInstance == 1


def test_initial_per_instance_state_is_seeded(controller):
    """Slot 1's per-instance dicts are pre-populated with default values."""
    assert controller._awaiting_response == {1: False}
    assert controller._permission_mode == {1: "default"}


# ---------------------------------------------------------------------------
# focus_instance — Phase A locks at 1, but the slot exists for Phase B
# ---------------------------------------------------------------------------


def test_focus_instance_unknown_slot_is_a_noop(controller):
    """Phase A only has slot 1; focusing an unknown slot must not mutate state."""
    emissions: list[int] = []
    controller.focusedInstanceChanged.connect(
        lambda: emissions.append(controller.focusedInstance)
    )

    controller.focus_instance(3)

    assert controller.focusedInstance == 1
    assert emissions == []


def test_focus_instance_already_focused_is_a_noop(controller):
    """Focusing the currently-focused slot must not re-emit the signal."""
    emissions: list[int] = []
    controller.focusedInstanceChanged.connect(
        lambda: emissions.append(controller.focusedInstance)
    )

    controller.focus_instance(1)

    assert emissions == []


def test_focus_instance_real_switch_emits_three_signals(controller):
    """Switching focus emits focusedInstance + awaitingResponse + permissionMode.

    Re-emitting the property signals is what lets the QML pane re-bind
    the spinner and pill to the newly-focused slot's per-instance
    state without QML knowing the pool exists.
    """
    _add_slot(controller, 2)
    # Seed slot 2 with state distinct from slot 1's defaults so the
    # bound properties produce different values after the focus switch.
    controller._awaiting_response[2] = True
    controller._permission_mode[2] = "plan"

    focus_emissions: list[int] = []
    awaiting_emissions: list[bool] = []
    mode_emissions: list[str] = []
    controller.focusedInstanceChanged.connect(
        lambda: focus_emissions.append(controller.focusedInstance)
    )
    controller.awaitingResponseChanged.connect(
        lambda: awaiting_emissions.append(controller.awaitingResponse)
    )
    controller.permissionModeChanged.connect(
        lambda: mode_emissions.append(controller.permissionMode)
    )

    controller.focus_instance(2)

    assert focus_emissions == [2]
    assert awaiting_emissions == [True]
    assert mode_emissions == ["plan"]
    assert controller.focusedInstance == 2
    assert controller.awaitingResponse is True
    assert controller.permissionMode == "plan"


# ---------------------------------------------------------------------------
# cycle_permission_mode_for — indexed dispatch
# ---------------------------------------------------------------------------


def test_cycle_permission_mode_for_routes_to_named_slot(controller):
    """Indexed cycle dispatches to the named slot's host, not the focused one."""
    fake_2 = _add_slot(controller, 2)
    fake_1 = controller._session_hosts[1]
    # Focus stays at 1 — we cycle slot 2 explicitly.
    controller.cycle_permission_mode_for(2)

    assert fake_2.set_permission_mode_calls == ["acceptEdits"]
    assert fake_1.set_permission_mode_calls == []  # type: ignore[union-attr]
    # Focused slot's pill must not flicker — the cycle was on slot 2.
    assert controller.permissionMode == "default"


def test_cycle_permission_mode_for_unknown_slot_is_a_noop(controller):
    """Phase A only has slot 1; cycling an unknown slot must not crash."""
    controller.cycle_permission_mode_for(99)
    assert controller._session_hosts[1].set_permission_mode_calls == []


def test_cycle_permission_mode_wrapper_uses_focused(controller):
    """Nullary wrapper routes to the focused instance.

    The QML side calls the nullary form unaware of the pool. After a
    focus switch, the wrapper must dispatch on the new focus.
    """
    fake_2 = _add_slot(controller, 2)
    fake_1 = controller._session_hosts[1]
    controller.focus_instance(2)

    controller.cycle_permission_mode()

    assert fake_2.set_permission_mode_calls == ["acceptEdits"]
    assert fake_1.set_permission_mode_calls == []  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# submit_prompt_for — indexed dispatch + per-instance optimistic render
# ---------------------------------------------------------------------------


def test_submit_prompt_for_routes_to_named_slot_cold_branch(controller):
    """Indexed submit on a cold host calls start(prompt), not send."""
    fake_2 = _add_slot(controller, 2)

    controller.submit_prompt_for("hello slot 2", 2)

    assert fake_2.start_calls == ["hello slot 2"]
    assert fake_2.send_calls == []
    # Slot 2's spinner is on; slot 1 is unaffected.
    assert controller._awaiting_response[2] is True
    assert controller._awaiting_response[1] is False


def test_submit_prompt_for_routes_to_named_slot_hot_branch(controller):
    """Indexed submit on a hot host calls send_user_message."""
    fake_2 = _add_slot(controller, 2)
    fake_2.is_running = True

    controller.submit_prompt_for("turn 2 to slot 2", 2)

    assert fake_2.send_calls == ["turn 2 to slot 2"]
    assert fake_2.start_calls == []


def test_submit_prompt_for_unknown_slot_is_a_noop(controller):
    """Submit to an unknown slot drops without crashing — no model mutation."""
    rows_before = len(controller._session_models[1]._rows)

    controller.submit_prompt_for("orphan", 99)

    assert len(controller._session_models[1]._rows) == rows_before
    assert controller._session_hosts[1].start_calls == []


def test_submit_prompt_wrapper_uses_focused(controller):
    """Nullary-of-prompt wrapper routes to the focused instance."""
    fake_2 = _add_slot(controller, 2)
    fake_1 = controller._session_hosts[1]
    controller.focus_instance(2)

    controller.submit_prompt("focused submit")

    assert fake_2.start_calls == ["focused submit"]
    assert fake_1.start_calls == []  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# request_id routing — `_pending_permissions` is the source of truth
# ---------------------------------------------------------------------------


def test_permission_request_event_populates_pending_map(controller):
    """A permission_request event records the issuing slot in _pending_permissions."""
    controller._on_session_event_for(
        2,
        {
            "type": "permission_request",
            "request_id": "req-from-slot-2",
            "tool_name": "Bash",
        },
    )

    assert controller._pending_permissions == {"req-from-slot-2": 2}


def test_respond_to_permission_routes_via_pending_map(controller):
    """Response routes to the issuing slot, NOT the focused slot.

    Multi-instance scenario: slot 2 issues a permission request, user
    focus-switches to slot 1, then responds. The response MUST go to
    slot 2's sidecar (whose canUseTool promise is awaiting), not slot 1.
    """
    fake_2 = _add_slot(controller, 2)
    fake_1 = controller._session_hosts[1]
    # Slot 2 issues a permission_request.
    payload = {
        "type": "permission_request",
        "request_id": "req-from-slot-2",
        "tool_name": "Edit",
    }
    controller._session_models[2].apply(payload)
    controller._on_session_event_for(2, payload)
    # User focuses slot 1 before responding.
    controller.focus_instance(1)
    assert controller.focusedInstance == 1

    controller.respond_to_permission("req-from-slot-2", "allow")

    # Response went to slot 2 (the issuer), not slot 1 (the focused).
    assert fake_2.permission_calls == [("req-from-slot-2", "allow")]
    assert fake_1.permission_calls == []  # type: ignore[union-attr]
    # Pending map is cleared.
    assert "req-from-slot-2" not in controller._pending_permissions


def test_respond_to_permission_unknown_request_id_is_dropped(controller):
    """Response to an unrecorded request_id logs and drops, never routes."""
    controller.respond_to_permission("never-issued", "allow")

    assert controller._session_hosts[1].permission_calls == []


def test_session_close_clears_pending_for_that_slot_only(controller):
    """Closing slot 2's sidecar drops slot 2's pending requests but spares slot 1's."""
    _add_slot(controller, 2)
    controller._on_session_event_for(
        1,
        {"type": "permission_request", "request_id": "req-1", "tool_name": "Bash"},
    )
    controller._on_session_event_for(
        2,
        {"type": "permission_request", "request_id": "req-2", "tool_name": "Edit"},
    )
    assert controller._pending_permissions == {"req-1": 1, "req-2": 2}

    controller._on_session_closed_for(2)

    assert controller._pending_permissions == {"req-1": 1}


# ---------------------------------------------------------------------------
# Per-instance state isolation
# ---------------------------------------------------------------------------


def test_set_awaiting_response_for_only_emits_when_focused_slot_changes(controller):
    """Mutating slot 2's spinner while focus is on slot 1 must NOT emit.

    The QML property reads from the focused slot — emitting on a
    non-focused mutation would re-bind the QML binding to a stale
    value, producing user-visible flicker.
    """
    _add_slot(controller, 2)
    emissions: list[bool] = []
    controller.awaitingResponseChanged.connect(
        lambda: emissions.append(controller.awaitingResponse)
    )

    controller._set_awaiting_response_for(2, True)

    assert emissions == []  # Slot 2 is not focused.
    assert controller._awaiting_response[2] is True
    # Slot 1 (focused) is unchanged.
    assert controller.awaitingResponse is False


def test_set_permission_mode_for_only_emits_when_focused_slot_changes(controller):
    """Same isolation contract for permission mode."""
    _add_slot(controller, 2)
    emissions: list[str] = []
    controller.permissionModeChanged.connect(
        lambda: emissions.append(controller.permissionMode)
    )

    controller._set_permission_mode_for(2, "bypassPermissions")

    assert emissions == []  # Slot 2 is not focused.
    assert controller._permission_mode[2] == "bypassPermissions"
    assert controller.permissionMode == "default"  # Slot 1 still default.


# ---------------------------------------------------------------------------
# _create_instance — idempotency
# ---------------------------------------------------------------------------


def test_create_instance_idempotent_on_existing_slot(controller):
    """Re-calling `_create_instance(1)` on the existing slot logs and returns.

    Phase B's `<leader>aN` will call this on a fresh slot; tolerating
    re-entry on an already-allocated slot avoids a crash if the
    Lua-side debounce ever fails.
    """
    instance_count_before = controller.instanceCount

    controller._create_instance(1)

    assert controller.instanceCount == instance_count_before
    # The fake we installed in the fixture is still in slot 1 — not
    # replaced by a freshly-constructed real SessionHost.
    assert isinstance(controller._session_hosts[1], _FakeSessionHost)


def test_create_instance_emits_instance_count_changed(controller):
    """Allocating a fresh slot increments instanceCount and emits."""
    emissions: list[int] = []
    controller.instanceCountChanged.connect(
        lambda: emissions.append(controller.instanceCount)
    )

    controller._create_instance(2)

    assert emissions == [2]
    assert controller.instanceCount == 2


# ===========================================================================
# Phase B — multi-instance spawn + focus dispatch
# ===========================================================================
#
# Phase B introduces `<C-1>..<C-5>` focus + `<C-S-q>` close + a redefined
# `<leader>aN` (spawn-into-next-free instead of reset-focused). The tests
# below exercise:
#
#   - The pool helpers `_next_free_slot`, `_close_instance`,
#     `_next_focus_after_close`.
#   - The `_on_agent_event` dispatch table for focus / close / spawn-new.
#   - `sessionModelForFocused` re-binding on focus switch.
#
# Spawn-path tests use `monkeypatch` to swap `_create_instance` with a
# fake-installing version — `_spawn_instance` calls
# `_create_instance(slot)` THEN `host.start("")`, and the real
# `_create_instance` would construct an actual `SessionHost` whose
# `start()` would Popen a Node subprocess. Patching at the
# `_create_instance` boundary keeps the composition intact while
# preventing the side effect.


@pytest.fixture
def patched_controller(controller, monkeypatch):
    """Controller whose `_create_instance` installs a fake, not a real host.

    Used by tests that exercise spawn paths (`_spawn_instance`,
    `_on_agent_event` op=show action=new). Mirrors the real allocator
    closely (same dict mutations, same `instanceCountChanged` emit) so
    `_spawn_instance` still composes meaningfully — only the
    SessionHost/SessionModel construction is swapped for fakes.
    """

    def fake_create(slot: int) -> None:
        if slot in controller._session_hosts:
            return
        fake = _FakeSessionHost(instance_index=slot)
        controller._session_hosts[slot] = fake  # type: ignore[assignment]
        controller._session_models[slot] = SessionModel(controller, instance_index=slot)
        controller._awaiting_response[slot] = False
        controller._permission_mode[slot] = "default"
        controller.instanceCountChanged.emit()

    monkeypatch.setattr(controller, "_create_instance", fake_create)
    return controller


# ---------------------------------------------------------------------------
# _next_free_slot
# ---------------------------------------------------------------------------


def test_next_free_slot_returns_2_when_only_slot_1_occupied(controller):
    """Slot 1 is pre-allocated; the first free slot is 2."""
    assert controller._next_free_slot() == 2


def test_next_free_slot_walks_to_next_gap(controller):
    """Lowest-free, not lowest-numbered: {1, 2, 4} → 3."""
    _add_slot(controller, 2)
    _add_slot(controller, 4)
    assert controller._next_free_slot() == 3


def test_next_free_slot_returns_none_when_pool_full(controller):
    """{1..5} occupied → None — caller is responsible for the fallback."""
    for slot in range(2, 6):
        _add_slot(controller, slot)
    assert controller._next_free_slot() is None


def test_next_free_slot_returns_1_when_pool_empty(controller):
    """Empty pool → slot 1 (matches cold-start behavior)."""
    del controller._session_hosts[1]
    del controller._session_models[1]
    del controller._awaiting_response[1]
    del controller._permission_mode[1]
    assert controller._next_free_slot() == 1


# ---------------------------------------------------------------------------
# _spawn_instance — composition of _create_instance + host.start("")
# ---------------------------------------------------------------------------


def test_spawn_instance_allocates_and_pre_warms(patched_controller):
    """`_spawn_instance(2)` populates the pool slot AND starts the host."""
    patched_controller._spawn_instance(2)

    assert 2 in patched_controller._session_hosts
    fake = patched_controller._session_hosts[2]
    # Empty-prompt pre-warm — same contract slot 1 uses at app start.
    assert fake.start_calls == [""]
    assert fake.is_running is True


def test_spawn_instance_passes_prompt_through(patched_controller):
    """Non-empty prompt threads through to `host.start(prompt)`.

    Phase A's pre-warm passes "" (no first message). Phase C / future
    keybinds may pass an initial prompt; this test pins that the
    composition routes the argument as-is.
    """
    patched_controller._spawn_instance(3, prompt="hello world")

    fake = patched_controller._session_hosts[3]
    assert fake.start_calls == ["hello world"]


# ---------------------------------------------------------------------------
# _close_instance — tear-down + state cleanup
# ---------------------------------------------------------------------------


def test_close_instance_removes_slot_from_every_dict(controller):
    """Close drops the slot from hosts/models/awaiting/permission_mode dicts."""
    _add_slot(controller, 2)
    controller._awaiting_response[2] = True
    controller._permission_mode[2] = "plan"

    controller._close_instance(2)

    assert 2 not in controller._session_hosts
    assert 2 not in controller._session_models
    assert 2 not in controller._awaiting_response
    assert 2 not in controller._permission_mode


def test_close_instance_calls_stop_on_host(controller):
    """The slot's sidecar gets `stop()` called so its workers join cleanly."""
    fake = _add_slot(controller, 2)

    controller._close_instance(2)

    assert fake.stop_calls == 1


def test_close_instance_drops_pending_permissions_for_that_slot(controller):
    """`_pending_permissions` entries for the dead slot are pruned.

    The SDK auto-rejects canUseTool's promise on subprocess abort, so
    the request_id will never be answered. Leaving it in the dict
    means a future `respond_to_permission` round-trip would
    misroute. Critically: only entries for the closed slot are
    dropped — entries from other slots survive.
    """
    _add_slot(controller, 2)
    _add_slot(controller, 3)
    controller._pending_permissions = {
        "req-from-2": 2,
        "req-from-3": 3,
        "another-from-2": 2,
    }

    controller._close_instance(2)

    assert controller._pending_permissions == {"req-from-3": 3}


def test_close_instance_emits_instance_count_changed(controller):
    """Closing decrements the instanceCount and emits."""
    _add_slot(controller, 2)
    emissions: list[int] = []
    controller.instanceCountChanged.connect(
        lambda: emissions.append(controller.instanceCount)
    )

    controller._close_instance(2)

    assert emissions == [1]
    assert controller.instanceCount == 1


def test_close_instance_unknown_slot_is_a_noop(controller):
    """Closing a slot not in the pool logs and returns — no crash."""
    instance_count_before = controller.instanceCount

    controller._close_instance(7)

    assert controller.instanceCount == instance_count_before


# ---------------------------------------------------------------------------
# _next_focus_after_close — PRD §5.3 walk-down-then-up rule
# ---------------------------------------------------------------------------


def test_next_focus_after_close_walks_below_first(controller):
    """Closing slot 3 of {1,2,3} → focus 2 (below the closed one)."""
    _add_slot(controller, 2)
    _add_slot(controller, 3)
    # Simulate the close having already happened — the helper reads
    # the post-close pool state.
    del controller._session_hosts[3]
    del controller._session_models[3]
    del controller._awaiting_response[3]
    del controller._permission_mode[3]

    assert controller._next_focus_after_close(3) == 2


def test_next_focus_after_close_walks_up_when_no_below(controller):
    """Closing slot 1 of {1,2,3} → focus 2 (no below; first above)."""
    _add_slot(controller, 2)
    _add_slot(controller, 3)
    del controller._session_hosts[1]
    del controller._session_models[1]
    del controller._awaiting_response[1]
    del controller._permission_mode[1]

    assert controller._next_focus_after_close(1) == 2


def test_next_focus_after_close_skips_gaps(controller):
    """Closing slot 4 of {1,2,4} → focus 2 (walks below past 3, finds 2)."""
    _add_slot(controller, 2)
    _add_slot(controller, 4)
    del controller._session_hosts[4]
    del controller._session_models[4]
    del controller._awaiting_response[4]
    del controller._permission_mode[4]

    assert controller._next_focus_after_close(4) == 2


def test_next_focus_after_close_empty_pool_returns_none(controller):
    """Empty pool → None; dispatcher uses this to trigger hide_agent."""
    del controller._session_hosts[1]
    del controller._session_models[1]
    del controller._awaiting_response[1]
    del controller._permission_mode[1]

    assert controller._next_focus_after_close(1) is None


# ---------------------------------------------------------------------------
# _on_agent_event — Phase B dispatch table
# ---------------------------------------------------------------------------


def test_agent_event_focus_dispatches_to_focus_instance(controller):
    """`{op:focus, index:2}` switches focus when slot 2 is in the pool."""
    _add_slot(controller, 2)

    controller._on_agent_event({"op": "focus", "index": 2})

    assert controller.focusedInstance == 2


def test_agent_event_focus_unknown_slot_is_a_noop(controller):
    """Per PRD B2: focusing an empty slot does NOT spawn — no-op + log."""
    controller._on_agent_event({"op": "focus", "index": 4})

    assert controller.focusedInstance == 1
    assert 4 not in controller._session_hosts


def test_agent_event_focus_out_of_range_is_dropped(controller):
    """`<C-9>` would be caught at the Lua side, but defense in depth."""
    _add_slot(controller, 2)

    controller._on_agent_event({"op": "focus", "index": 9})

    assert controller.focusedInstance == 1


def test_agent_event_focus_missing_index_is_dropped(controller):
    """No index field → log warning, no-op."""
    controller._on_agent_event({"op": "focus"})

    assert controller.focusedInstance == 1


def test_agent_event_close_no_index_closes_focused(controller):
    """`<C-S-q>` emits `{op:close}` (no index) → close focused, refocus."""
    _add_slot(controller, 2)
    controller.focus_instance(2)
    fake_2 = controller._session_hosts[2]

    controller._on_agent_event({"op": "close"})

    assert 2 not in controller._session_hosts
    assert fake_2.stop_calls == 1  # type: ignore[union-attr]
    # Focused slot was 2 → after close, refocus to 1 (only one left).
    assert controller.focusedInstance == 1


def test_agent_event_close_explicit_index_closes_that_slot(controller):
    """`{op:close, index:2}` closes slot 2 even when slot 3 is focused."""
    _add_slot(controller, 2)
    _add_slot(controller, 3)
    controller.focus_instance(3)

    controller._on_agent_event({"op": "close", "index": 2})

    assert 2 not in controller._session_hosts
    # Focus stays at 3 — we closed a non-focused slot.
    assert controller.focusedInstance == 3


def test_agent_event_close_focused_with_multiple_picks_below(controller):
    """Closing slot 3 of {1,2,3} (focus on 3) → focus drops to 2."""
    _add_slot(controller, 2)
    _add_slot(controller, 3)
    controller.focus_instance(3)

    controller._on_agent_event({"op": "close"})

    assert controller.focusedInstance == 2


def test_agent_event_close_emptying_pool_hides_pane_and_resets_focus(controller):
    """Closing the last instance hides the pane and resets focus to 1."""
    controller.show_agent()
    assert controller.agentVisible is True

    controller._on_agent_event({"op": "close"})

    assert controller.instanceCount == 0
    assert controller.agentVisible is False
    # Focused instance snaps back to 1 so the next spawn lands at slot 1
    # (matches cold-start behavior).
    assert controller.focusedInstance == 1


def test_agent_event_close_emptying_pool_emits_all_signals_even_when_focused_was_1(
    controller,
):
    """Empty-pool close always emits awaitingResponse + permissionMode signals.

    Regression guard for the Phase B bug where the empty-pool branch
    only emitted focus-tracking signals when `_focused_instance != 1`.
    In the normal case — closing the only instance, which starts at
    slot 1 — the guard suppressed the emissions, leaving QML with a
    stale spinner / permission-pill state from the now-dead slot 1
    sidecar. The fix: always emit unconditionally.
    """
    # Seed slot 1 with non-default state to make stale-state observable.
    controller._set_awaiting_response_for(1, True)  # type: ignore[attr-defined]
    controller._permission_mode[1] = "plan"
    controller.show_agent()

    awaiting_emissions: list[bool] = []
    mode_emissions: list[str] = []
    focused_emissions: list[int] = []
    controller.awaitingResponseChanged.connect(
        lambda: awaiting_emissions.append(controller.awaitingResponse)
    )
    controller.permissionModeChanged.connect(
        lambda: mode_emissions.append(controller.permissionMode)
    )
    controller.focusedInstanceChanged.connect(
        lambda: focused_emissions.append(controller.focusedInstance)
    )

    controller._on_agent_event({"op": "close"})

    # Pool is empty — the property getters fall back to defaults.
    assert controller.awaitingResponse is False
    assert controller.permissionMode == "default"
    assert controller.focusedInstance == 1
    # Signals must have fired so QML re-evaluates the bindings and
    # stops showing the stale spinner / mode pill.
    assert len(awaiting_emissions) >= 1, (
        "awaitingResponseChanged must fire on empty-pool close"
    )
    assert len(mode_emissions) >= 1, (
        "permissionModeChanged must fire on empty-pool close"
    )


def test_agent_event_close_unknown_slot_is_a_noop(controller):
    """`{op:close, index:9}` logs and returns — no crash, no state change."""
    instance_count_before = controller.instanceCount

    controller._on_agent_event({"op": "close", "index": 9})

    assert controller.instanceCount == instance_count_before
    assert controller.focusedInstance == 1


def test_agent_event_show_new_spawns_next_free_slot(patched_controller):
    """`<leader>aN` with slot 1 occupied → spawn slot 2, focus it, show pane."""
    patched_controller._on_agent_event({"op": "show", "action": "new"})

    assert 2 in patched_controller._session_hosts
    assert patched_controller._session_hosts[2].start_calls == [""]
    assert patched_controller.focusedInstance == 2
    assert patched_controller.agentVisible is True


def test_agent_event_show_new_when_pool_full_focuses_highest(patched_controller):
    """Pool full → no spawn; focus highest-numbered (most recent) slot."""
    for slot in range(2, 6):
        _add_slot(patched_controller, slot)
    assert patched_controller.instanceCount == 5  # sanity

    patched_controller._on_agent_event({"op": "show", "action": "new"})

    # No new slot allocated.
    assert patched_controller.instanceCount == 5
    # Focus snapped to slot 5 (the last spawned).
    assert patched_controller.focusedInstance == 5
    assert patched_controller.agentVisible is True


def test_agent_event_show_without_action_just_shows(patched_controller):
    """`{op:show}` without `action` doesn't spawn — just opens the pane."""
    initial_count = patched_controller.instanceCount

    patched_controller._on_agent_event({"op": "show"})

    assert patched_controller.instanceCount == initial_count
    assert patched_controller.agentVisible is True


# ---------------------------------------------------------------------------
# Cross-instance request_id routing (Phase B's harder correctness gate)
# ---------------------------------------------------------------------------


def test_respond_to_permission_routes_across_focus_switch(controller):
    """A request from slot 2 must resolve on slot 2 even after focus switches.

    This is the PRD §4.3 risk #1 case manifesting in Phase B: user
    focuses slot 2, slot 2 issues a permission_request, user
    `<C-1>`s back to slot 1 to read something, then the permission
    card from slot 2 is still visible (it's part of slot 2's model
    history) — but the card lives in slot 2's transcript, so to see
    it the user re-focuses 2. Either way, the ROUTING via
    `_pending_permissions` must always pick slot 2's host, never the
    focused one.
    """
    fake_2 = _add_slot(controller, 2)
    # Slot 2 issues the request while focused.
    controller.focus_instance(2)
    controller._on_session_event_for(
        2, {"type": "permission_request", "request_id": "req-cross-focus"}
    )
    # User switches focus back to slot 1 mid-request.
    controller.focus_instance(1)

    controller.respond_to_permission("req-cross-focus", "allow")

    # Decision lands on slot 2's sidecar (the issuer), not slot 1's.
    fake_1 = controller._session_hosts[1]
    assert fake_2.permission_calls == [("req-cross-focus", "allow")]
    assert fake_1.permission_calls == []  # type: ignore[union-attr]
    # Pending map cleaned up regardless of focus.
    assert "req-cross-focus" not in controller._pending_permissions


# ---------------------------------------------------------------------------
# sessionModelForFocused property — re-binds on focus switch
# ---------------------------------------------------------------------------


def test_session_model_for_focused_returns_focused_slot_model(controller):
    """The property tracks `_focused_instance`'s model identity."""
    _add_slot(controller, 2)
    slot_1_model = controller._session_models[1]
    slot_2_model = controller._session_models[2]

    assert controller.sessionModelForFocused is slot_1_model
    controller.focus_instance(2)
    assert controller.sessionModelForFocused is slot_2_model


def test_session_model_for_focused_emits_via_focused_instance_changed(controller):
    """Focus switch emits `focusedInstanceChanged` — QML's binding trigger.

    PySide6 fires the property's notify when its declared signal
    emits; this test pins the contract by counting emissions of the
    signal and confirming the property's value updates in lockstep.
    """
    _add_slot(controller, 2)
    emissions: list[object] = []
    controller.focusedInstanceChanged.connect(
        lambda: emissions.append(controller.sessionModelForFocused)
    )

    controller.focus_instance(2)

    assert len(emissions) == 1
    assert emissions[0] is controller._session_models[2]
