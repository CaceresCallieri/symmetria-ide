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


class _FakeSessionHost:
    """Stand-in for SessionHost — same shape as the awaiting-state fake.

    Carries `instance_index` so tests can construct one fake per slot
    and assert dispatch routes to the right one. `is_running` is
    flipped True after `start()` to mimic the real host's spawn
    semantics — the cold-vs-hot branch of `submit_prompt_for` reads
    `is_running` to decide between `start(prompt)` and
    `send_user_message(prompt)`.
    """

    def __init__(self, instance_index: int = 0) -> None:
        self.instance_index = instance_index
        self.is_running = False
        self.start_calls: list[str] = []
        self.send_calls: list[str] = []
        self.stop_calls = 0
        self.permission_calls: list[tuple[str, str]] = []
        self.set_permission_mode_calls: list[str] = []

    def start(self, prompt: str = "") -> None:
        self.start_calls.append(prompt)
        self.is_running = True

    def send_user_message(self, text: str) -> None:
        self.send_calls.append(text)

    def send_permission_response(self, request_id: str, behavior: str) -> None:
        self.permission_calls.append((request_id, behavior))

    def send_set_permission_mode(self, mode: str) -> None:
        self.set_permission_mode_calls.append(mode)

    def stop(self) -> None:
        self.stop_calls += 1


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
    ctrl._session_models[slot] = ctrl._session_models[1].__class__(  # noqa: SLF001
        ctrl, instance_index=slot
    )
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
    fake_2 = _add_slot(controller, 2)
    assert fake_2 is not None  # silence vulture-style "unused"
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
