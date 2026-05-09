"""Unit tests for `AppController.awaitingResponse` — the loading-state spinner.

Covers all four transition edges and the idempotency guard:

- ON: `submit_prompt` flips True after the synthetic user-event injection.
- OFF: `_on_session_event` with `type="result"` (canonical turn-complete).
- OFF: `_on_session_closed` (subprocess EOF / crash / SIGTERM).
- OFF: `_on_agent_event` `op="show", action="new"` (the `<leader>aN` reset).
- Idempotency: `_set_awaiting_response` does NOT re-emit
  `awaitingResponseChanged` when the value didn't actually change.

Hermetic shape: no nvim subprocess, no `claude` subprocess, no QML engine.
We construct a real `AppController` (its `__init__` is allocation-only —
threads / subprocesses spawn from `start()` which we never call) and stub
out the bits of `SessionHost` that would otherwise reach for the network
or argv.
"""

from __future__ import annotations

import pytest

from symmetria_ide.app import AppController
from tests.conftest import (
    FakeSessionHost as _FakeSessionHost,
)  # canonical fake — see conftest.py


@pytest.fixture
def controller():
    """Construct an `AppController` with the session host swapped for a fake.

    The fake is installed AFTER `__init__` returns because `__init__`
    wires several signal connections against the real `SessionHost`
    object. Replacing the field after construction is safe — none of
    the slots that this test exercises (`_set_awaiting_response`,
    `_on_session_event`, `_on_session_closed`, `submit_prompt`,
    `_on_agent_event`) follow a `_session_host` reference set during
    construction; they read it dynamically each call.
    """
    ctrl = AppController()
    # `__init__` no longer auto-allocates slot 1 — production now waits
    # for the user's first `<leader>aN` to spawn the first instance.
    # Tests still need slot 1 populated synchronously so they can
    # exercise per-instance dispatch without driving the agent_event
    # path; `_create_instance(1)` allocates host + model + scalar state
    # without spawning a subprocess. We then swap the real host for a
    # fake — slot 1's other dict entries (`_session_models[1]`,
    # `_awaiting_response[1]`, `_permission_mode[1]`) stay pointing at
    # the real objects which suffices for the assertions in this file.
    ctrl._create_instance(1)
    ctrl._session_hosts[1] = _FakeSessionHost(instance_index=1)  # type: ignore[assignment]  # pyright: ignore[reportPrivateUsage]
    yield ctrl
    # Stop the real backend that __init__ created, even though we
    # never started it — defensive in case future __init__ changes
    # spin up threads earlier.
    ctrl.shutdown()


def _capture_emissions(ctrl: AppController) -> list[bool]:
    """Capture every `awaitingResponseChanged` emission as a boolean snapshot.

    Cleaner than counting signal fires — we get both 'how many' and
    'in what order'. Pyside6 connects callables directly, so this
    list-append closure is the lightweight equivalent of `qtbot`'s
    SignalSpy without pulling pytest-qt into a pure-Python test.
    """
    emissions: list[bool] = []
    ctrl.awaitingResponseChanged.connect(
        lambda: emissions.append(ctrl.awaitingResponse)
    )
    return emissions


def test_initial_state_is_false(controller):
    """Spinner starts off — no in-flight request before `submit_prompt`."""
    assert controller.awaitingResponse is False


def test_submit_prompt_flips_awaiting_to_true(controller):
    """ON edge — `submit_prompt` must light the spinner."""
    emissions = _capture_emissions(controller)

    controller.submit_prompt("hello")

    assert controller.awaitingResponse is True
    assert emissions == [True]
    # Cold path was taken since `_FakeSessionHost.is_running` is False.
    assert controller._session_hosts[1].start_calls == ["hello"]
    assert controller._session_hosts[1].send_calls == []


def test_submit_prompt_hot_branch_also_flips(controller):
    """ON edge holds whether the host is cold or hot."""
    controller._session_hosts[1].is_running = True

    controller.submit_prompt("turn 2")

    assert controller.awaitingResponse is True
    assert controller._session_hosts[1].send_calls == ["turn 2"]
    assert controller._session_hosts[1].start_calls == []


def test_submit_prompt_empty_string_is_a_noop(controller):
    """Whitespace-only prompts must NOT light the spinner — `submit_prompt`
    short-circuits before reaching the ON edge."""
    emissions = _capture_emissions(controller)

    controller.submit_prompt("   ")

    assert controller.awaitingResponse is False
    assert emissions == []
    assert controller._session_hosts[1].start_calls == []


def test_on_session_event_result_flips_off(controller):
    """OFF edge — `result` envelope is the canonical turn-complete signal."""
    controller._set_awaiting_response_for(1, True)
    emissions = _capture_emissions(controller)

    controller._on_session_event_for(1, {"type": "result", "duration_ms": 1234})

    assert controller.awaitingResponse is False
    assert emissions == [False]


def test_on_session_event_non_result_does_not_flip(controller):
    """Stream-events / assistant deltas / tool_use must NOT clear the spinner.

    Tool-using turns interleave streaming text + tool_use + tool_result
    blocks BEFORE the canonical `result` envelope. If any of those
    flipped the spinner off early, the user would see "Claude is
    thinking" disappear and then sit through silent tool work with
    nothing to indicate progress.
    """
    controller._set_awaiting_response_for(1, True)
    emissions = _capture_emissions(controller)

    for event in (
        {"type": "stream_event", "event": {"type": "content_block_delta"}},
        {"type": "assistant", "message": {"content": "..."}},
        {"type": "user", "message": {"role": "user", "content": "..."}},
        {"type": "system", "subtype": "init"},
        {},  # missing type
    ):
        controller._on_session_event_for(1, event)

    assert controller.awaitingResponse is True
    assert emissions == []


def test_on_session_closed_flips_off(controller):
    """OFF edge — subprocess EOF / crash / SIGTERM."""
    controller._set_awaiting_response_for(1, True)
    emissions = _capture_emissions(controller)

    controller._on_session_closed_for(1)

    assert controller.awaitingResponse is False
    assert emissions == [False]


def _patch_create_instance_with_fake(controller, monkeypatch):
    """Swap `_create_instance` for a fake-installing version.

    Phase B's `<leader>aN` dispatches into `_spawn_instance(slot)` →
    `_create_instance(slot)` + `host.start("")`. The real
    `_create_instance` constructs an actual `SessionHost`; `start("")`
    then spawns a Node subprocess from `sidecar/dist/index.js`.
    Tests must avoid both side effects, so we replace the allocator
    with a fake-host installer that mirrors the real dict mutations.
    """

    def fake_create(slot: int) -> None:
        if slot in controller._session_hosts:
            return
        fake = _FakeSessionHost(instance_index=slot)
        controller._session_hosts[slot] = fake  # type: ignore[assignment]  # pyright: ignore[reportPrivateUsage]
        # Reuse the real SessionModel — no thread / subprocess
        # dependencies in the model itself.
        from symmetria_ide.session_models import SessionModel  # noqa: PLC0415

        controller._session_models[slot] = SessionModel(controller, instance_index=slot)
        controller._awaiting_response[slot] = False
        controller._permission_mode[slot] = "default"
        controller.instanceCountChanged.emit()

    monkeypatch.setattr(controller, "_create_instance", fake_create)


def test_agent_event_action_new_clears_focused_spinner(controller, monkeypatch):
    """OFF edge — `<leader>aN` spawns a fresh slot and focuses it.

    Phase B redefines `<leader>aN` as "spawn into next free slot,
    focus it" — NOT "reset the focused instance" (the old Phase A
    contract). The QML-facing `awaitingResponse` reads from
    `_focused_instance`'s slot in the per-instance dict; after the
    focus switch to the freshly-spawned slot 2, it reads slot 2's
    default (False), so the spinner stops without the dispatcher
    touching slot 1's state at all.

    Slot 1's underlying spinner state is preserved (per-instance
    isolation): the user can `<C-1>` back to find their prior turn
    still in flight from slot 1's perspective.
    """
    _patch_create_instance_with_fake(controller, monkeypatch)
    controller._set_awaiting_response_for(1, True)
    emissions = _capture_emissions(controller)

    controller._on_agent_event({"op": "show", "action": "new"})

    # QML-facing property reads False because focus moved to the new
    # slot 2, whose default awaiting state is False.
    assert controller.awaitingResponse is False
    # Single emission from the focus switch — the property's notify
    # signal fires once when `focus_instance` retargets the binding.
    assert emissions == [False]
    # Slot 1's underlying spinner state is UNTOUCHED — Phase B does
    # not reset other slots when spawning a new one.
    assert controller._awaiting_response[1] is True


def test_agent_event_show_without_action_new_does_not_reset(controller):
    """A plain `show` op (no action="new") must leave the spinner alone.

    Re-opening the pane with an in-flight turn shouldn't hide the
    "thinking" indicator — the conversation is still alive.
    """
    controller._set_awaiting_response_for(1, True)
    emissions = _capture_emissions(controller)

    controller._on_agent_event({"op": "show"})

    assert controller.awaitingResponse is True
    assert emissions == []


def test_set_awaiting_response_is_idempotent(controller):
    """Repeated edges must NOT re-emit the change signal.

    `event_received` can fan out multiple `result` envelopes during
    `--verbose`, and emitting `awaitingResponseChanged` repeatedly
    would force every QML binding (visibility, the SequentialAnimation's
    `running` state) to re-evaluate. This test locks in the
    change-detection contract.
    """
    emissions = _capture_emissions(controller)

    # Already False on construction — calling False again is a no-op.
    controller._set_awaiting_response_for(1, False)
    assert emissions == []

    # Real transition False -> True emits once.
    controller._set_awaiting_response_for(1, True)
    assert emissions == [True]

    # Re-asserting True is a no-op.
    controller._set_awaiting_response_for(1, True)
    assert emissions == [True]

    # Real transition True -> False emits once.
    controller._set_awaiting_response_for(1, False)
    assert emissions == [True, False]


# ---------------------------------------------------------------------------
# Permission state machine — `respond_to_permission` slot
# ---------------------------------------------------------------------------
#
# These tests cover the AppController glue between the QML approve/deny
# buttons and the two downstream consumers (the SessionHost write path
# and the SessionModel row-mutation slot). They use the same hermetic
# `_FakeSessionHost` swap as the awaiting-state tests above and exercise
# `respond_to_permission` with both the real `SessionModel` (no thread
# dependency, safe to call directly) and the fake host.


def _seed_pending_permission(ctrl: AppController, request_id: str, tool: str) -> None:
    """Inject a pending permission_request into model + the routing map.

    Two seeding steps mirror what happens in production:

    1. `_session_models[1].apply` — populates the row that the QML
       approve/deny card binds against (production path: queued
       connection from `SessionHost.event_received`).
    2. `_on_session_event_for(1, ...)` — populates
       `_pending_permissions[request_id] = 1` so the Phase A
       routing lookup in `respond_to_permission` finds the
       issuing slot. Without this seed, the controller would
       drop the response with "request_id not in pending map"
       because no `permission_request` event ever populated
       the routing dict.

    Both calls are GUI-thread direct (same shortcut `submit_prompt`
    uses for the optimistic user-row injection); no QueuedConnection
    detour needed in tests.
    """
    payload = {
        "type": "permission_request",
        "request_id": request_id,
        "tool_name": tool,
        "title": f"Allow {tool}?",
    }
    ctrl._session_models[1].apply(payload)
    ctrl._on_session_event_for(1, payload)


def test_respond_to_permission_allow_dispatches_host_and_model(controller):
    """Allow click — host receives ("req-1","allow"); model row flips approved."""
    _seed_pending_permission(controller, "req-1", "Bash")

    controller.respond_to_permission("req-1", "allow")

    assert controller._session_hosts[1].permission_calls == [("req-1", "allow")]
    # Walk the model rows and find the resolved one. There's only one
    # row at this point so this is concise.
    rows = controller._session_models[1]._rows
    assert len(rows) == 1
    assert rows[0].permission_state == "approved"
    assert rows[0].request_id == "req-1"


def test_respond_to_permission_deny_dispatches_host_and_model(controller):
    """Deny click — host receives ("req-2","deny"); model row flips denied."""
    _seed_pending_permission(controller, "req-2", "Edit")

    controller.respond_to_permission("req-2", "deny")

    assert controller._session_hosts[1].permission_calls == [("req-2", "deny")]
    rows = controller._session_models[1]._rows
    assert rows[0].permission_state == "denied"


def test_respond_to_permission_invalid_decision_is_a_noop(controller):
    """Garbage `decision` strings must NOT reach the host or the model."""
    _seed_pending_permission(controller, "req-3", "Read")

    controller.respond_to_permission("req-3", "maybe")

    assert controller._session_hosts[1].permission_calls == []
    rows = controller._session_models[1]._rows
    # Row stays pending — invalid input must not corrupt model state.
    assert rows[0].permission_state == "pending"


def test_permission_request_event_does_not_clear_spinner(controller):
    """Pending permission keeps the spinner ON.

    The turn is still in flight from the user's perspective — the
    sidecar's canUseTool callback is awaiting our reply, no `result`
    envelope has fired yet. Clearing the spinner here would lie to
    the user about whether work is happening.
    """
    controller._set_awaiting_response_for(1, True)
    emissions = _capture_emissions(controller)

    controller._on_session_event_for(
        1,
        {
            "type": "permission_request",
            "request_id": "req-4",
            "tool_name": "Bash",
        },
    )

    assert controller.awaitingResponse is True
    assert emissions == []


def test_respond_to_permission_ordering_host_before_model(controller):
    """Host-first ordering is the contract — sidecar promise resolves
    before the UI feedback fires, so a fast-following SDK event lands
    on a row we've already marked resolved.

    This test pins the ordering by stubbing the model's
    `resolve_permission` to record the host call count it observes;
    if the model ran first, that count would be 0 instead of 1.
    """
    _seed_pending_permission(controller, "req-5", "Glob")

    observed: list[int] = []
    original_resolve = controller._session_models[1].resolve_permission

    def spy(request_id: str, behavior: str) -> None:
        observed.append(len(controller._session_hosts[1].permission_calls))
        original_resolve(request_id, behavior)

    controller._session_models[1].resolve_permission = spy  # type: ignore[method-assign]
    controller.respond_to_permission("req-5", "allow")

    assert observed == [1], (
        "model.resolve_permission must run AFTER host.send_permission_response"
    )


# ---------------------------------------------------------------------------
# Lazy-spawn contract — `AppController.start()` does NOT pre-warm a sidecar
# unless an env-var path explicitly opts in
# ---------------------------------------------------------------------------
#
# The old pre-warm contract called `host.start("")` unconditionally at launch
# so the permission-mode pill + Shift+Tab cycling were live before the user
# touched the agent pane. This commit removes the unconditional pre-warm:
# slot 1 now spawns lazily on the user's first `<leader>aN`. The three
# tests below guard this new lazy contract:
#
#   - no env vars → pool stays empty, sidecar NOT spawned
#   - SYMMETRIA_IDE_AGENT_PROMPT → cold spawn carrying the prompt (one shot)
#   - SYMMETRIA_IDE_AGENT_VIEW   → spawn slot 1 empty + show pane
#
# Permission-mode cycling no longer needs a pre-warm because the sidecar's
# local `currentMode` is authoritative (CLAUDE.md gotcha #25); Shift+Tab
# dispatches correctly as long as a slot exists — which requires the user
# to have opened the pane first via `<leader>aN`.
#
# `controller.start()` also calls `self._backend.start()` which spawns
# nvim. To keep these tests hermetic we stub `_backend.start` (and `stop`
# for the fixture teardown) so no subprocess actually launches.


def _stub_backend(monkeypatch, ctrl: AppController) -> None:
    """Replace nvim spawn/teardown with no-ops so `controller.start()` is hermetic.

    The pre-warm tests below need to call `controller.start()` to exercise
    the new pre-warm path, but `start()` also spawns nvim via
    `self._backend.start()`. Stubbing both methods avoids the subprocess
    cost and keeps the test pure-Python — same hermetic discipline the
    awaiting-state tests above rely on.
    """
    monkeypatch.setattr(ctrl._backend, "start", lambda: None)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(ctrl._backend, "stop", lambda: None)  # pyright: ignore[reportPrivateUsage]


def test_app_controller_start_without_env_vars_does_not_spawn(controller, monkeypatch):
    """No env vars → `start()` must NOT spawn slot 1's subprocess.

    Regression guard for the user-visible "Claude is already running at
    launch" bug. The previous pre-warm contract called `host.start("")`
    unconditionally so the bubble strip lit slot 1 the moment the IDE
    opened — violating the user's mental model ("no agents until I press
    `<leader>aN`"). The new contract is lazy: the first `<leader>aN`
    spawns slot 1, and this test asserts the inactive default.

    The fixture's `_create_instance(1)` populates the pool dict so the
    test can assert against the slot, but `host.start_calls` should stay
    empty — the spawn is the user's call to make.
    """
    monkeypatch.delenv("SYMMETRIA_IDE_AGENT_PROMPT", raising=False)
    monkeypatch.delenv("SYMMETRIA_IDE_AGENT_VIEW", raising=False)
    _stub_backend(monkeypatch, controller)

    controller.start()

    assert controller._session_hosts[1].start_calls == [], (
        "no env vars should leave the sidecar dormant — user opts in via <leader>aN"
    )
    assert controller._session_hosts[1].send_calls == [], (
        "no env vars means no initial user_message write either"
    )


def test_app_controller_start_with_env_prompt_cold_spawns_with_prompt(
    controller, monkeypatch
):
    """`SYMMETRIA_IDE_AGENT_PROMPT` drives a cold spawn carrying the prompt.

    With no pre-warm, `submit_prompt` sees `is_running == False` and takes
    the cold branch — `host.start(prompt)` spawns the sidecar AND delivers
    `prompt` as the first user_message in a single SDK exchange. No
    separate empty pre-warm write, no follow-up send_user_message call.
    The synthetic-user-row injection inside `submit_prompt` still fires
    so the headless-smoke UX is preserved.
    """
    monkeypatch.setenv("SYMMETRIA_IDE_AGENT_PROMPT", "hi from env")
    monkeypatch.delenv("SYMMETRIA_IDE_AGENT_VIEW", raising=False)
    _stub_backend(monkeypatch, controller)

    controller.start()

    assert controller._session_hosts[1].start_calls == ["hi from env"], (
        "env-prompt drives a single cold spawn carrying the prompt"
    )
    assert controller._session_hosts[1].send_calls == [], (
        "cold branch never reaches send_user_message — start handles the first turn"
    )
    assert controller.awaitingResponse is True, (
        "submit_prompt's ON edge must still fire on the cold branch"
    )


def test_app_controller_start_with_env_view_only_pre_warms_slot_1(
    controller, monkeypatch
):
    """`SYMMETRIA_IDE_AGENT_VIEW=1` (no prompt) → spawn slot 1 empty + show pane.

    The VIEW-only env-var path is the analog of the user pressing
    `<leader>aN` interactively: the sidecar warms up so the pane has a
    session to focus on, but no message is sent. Without this, the pane
    would render with an empty pool and `agentVisible == True` — an
    incoherent state.
    """
    monkeypatch.delenv("SYMMETRIA_IDE_AGENT_PROMPT", raising=False)
    monkeypatch.setenv("SYMMETRIA_IDE_AGENT_VIEW", "1")
    _stub_backend(monkeypatch, controller)

    controller.start()

    assert controller._session_hosts[1].start_calls == [""], (
        "VIEW-only env path warms up the sidecar with an empty prompt"
    )
    assert controller._session_hosts[1].send_calls == [], (
        "no message yet — composer is ready for the user's first input"
    )
    assert controller.agentVisible is True, (
        "VIEW-only env path must reveal the agent pane on startup"
    )


# ---------------------------------------------------------------------------
# action="new" — Phase B redefines `<leader>aN` as spawn-into-next-free
# rather than reset-focused. The fresh slot must be pre-warmed with
# `start("")` so its permission-mode pill + Shift+Tab cycling are live
# from the moment focus arrives, and the previously-focused slot must
# survive untouched (Phase B's per-instance isolation contract).
# ---------------------------------------------------------------------------


def test_agent_event_action_new_spawns_fresh_slot_and_preserves_focused(
    controller, monkeypatch
):
    """`<leader>aN` Phase B contract — spawn a NEW slot, leave the focused alone.

    Pre-Phase-B (Phase A), `<leader>aN` reset the focused slot:
    `host.stop()` + `model.clear()` + `host.start("")` re-warm. Phase
    B redefines the keybind as "spawn into next free slot, focus it"
    so users can keep multiple parallel sessions alive — the focused
    slot's transcript is preserved in case the user `<C-1>`s back.

    This test pins the new contract: slot 2 gets allocated +
    pre-warmed with `start("")`, slot 1 is NOT stopped, slot 1's
    state survives unchanged, and focus moves to slot 2.
    """
    _patch_create_instance_with_fake(controller, monkeypatch)
    # Simulate slot 1 being mid-session (running + pending state).
    controller._session_hosts[1].is_running = True
    initial_slot_1_start_calls = list(controller._session_hosts[1].start_calls)

    controller._on_agent_event({"op": "show", "action": "new"})

    # Slot 2 was spawned + pre-warmed.
    assert 2 in controller._session_hosts, (
        "action='new' must allocate slot 2 (next free after 1)"
    )
    fake_2 = controller._session_hosts[2]
    assert fake_2.start_calls == [""], (
        "action='new' must pre-warm the new slot with start('') so the "
        "permission-mode pill is live from frame 1"
    )
    # Slot 1 is UNTOUCHED — no stop, no reset.
    assert controller._session_hosts[1].stop_calls == 0, (
        "action='new' must NOT stop the previously-focused slot — "
        "Phase B preserves parallel sessions"
    )
    assert controller._session_hosts[1].start_calls == initial_slot_1_start_calls, (
        "action='new' must NOT re-call start on the previously-focused slot"
    )
    # Focus moved to the new slot.
    assert controller.focusedInstance == 2
    # Pane is shown.
    assert controller.agentVisible is True
