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


class _FakeSessionHost:
    """Stand-in for SessionHost.

    `submit_prompt` reaches into `is_running`, `start`, and
    `send_user_message`. None of those start threads here — they just
    record the call so the test can assert on which branch ran.
    """

    def __init__(self) -> None:
        self.is_running = False
        self.start_calls: list[str] = []
        self.send_calls: list[str] = []
        self.stop_calls = 0

    def start(self, prompt: str) -> None:
        self.start_calls.append(prompt)

    def send_user_message(self, text: str) -> None:
        self.send_calls.append(text)

    def stop(self) -> None:
        self.stop_calls += 1


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
    ctrl._session_host = _FakeSessionHost()  # type: ignore[assignment]  # pyright: ignore[reportPrivateUsage]
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
    assert controller._session_host.start_calls == ["hello"]
    assert controller._session_host.send_calls == []


def test_submit_prompt_hot_branch_also_flips(controller):
    """ON edge holds whether the host is cold or hot."""
    controller._session_host.is_running = True

    controller.submit_prompt("turn 2")

    assert controller.awaitingResponse is True
    assert controller._session_host.send_calls == ["turn 2"]
    assert controller._session_host.start_calls == []


def test_submit_prompt_empty_string_is_a_noop(controller):
    """Whitespace-only prompts must NOT light the spinner — `submit_prompt`
    short-circuits before reaching the ON edge."""
    emissions = _capture_emissions(controller)

    controller.submit_prompt("   ")

    assert controller.awaitingResponse is False
    assert emissions == []
    assert controller._session_host.start_calls == []


def test_on_session_event_result_flips_off(controller):
    """OFF edge — `result` envelope is the canonical turn-complete signal."""
    controller._set_awaiting_response(True)
    emissions = _capture_emissions(controller)

    controller._on_session_event({"type": "result", "duration_ms": 1234})

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
    controller._set_awaiting_response(True)
    emissions = _capture_emissions(controller)

    for event in (
        {"type": "stream_event", "event": {"type": "content_block_delta"}},
        {"type": "assistant", "message": {"content": "..."}},
        {"type": "user", "message": {"role": "user", "content": "..."}},
        {"type": "system", "subtype": "init"},
        {},  # missing type
    ):
        controller._on_session_event(event)

    assert controller.awaitingResponse is True
    assert emissions == []


def test_on_session_closed_flips_off(controller):
    """OFF edge — subprocess EOF / crash / SIGTERM."""
    controller._set_awaiting_response(True)
    emissions = _capture_emissions(controller)

    controller._on_session_closed()

    assert controller.awaitingResponse is False
    assert emissions == [False]


def test_agent_event_action_new_resets_awaiting(controller):
    """OFF edge — `<leader>aN` "New Claude" must clear the spinner synchronously.

    The QueuedConnection on `closed` would deliver the OFF eventually,
    but the synchronous reset in `_on_agent_event` keeps the spinner
    from briefly lingering when the user mashes `<leader>aN`.
    """
    controller._set_awaiting_response(True)
    emissions = _capture_emissions(controller)

    controller._on_agent_event({"op": "show", "action": "new"})

    assert controller.awaitingResponse is False
    assert emissions == [False]


def test_agent_event_show_without_action_new_does_not_reset(controller):
    """A plain `show` op (no action="new") must leave the spinner alone.

    Re-opening the pane with an in-flight turn shouldn't hide the
    "thinking" indicator — the conversation is still alive.
    """
    controller._set_awaiting_response(True)
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
    controller._set_awaiting_response(False)
    assert emissions == []

    # Real transition False -> True emits once.
    controller._set_awaiting_response(True)
    assert emissions == [True]

    # Re-asserting True is a no-op.
    controller._set_awaiting_response(True)
    assert emissions == [True]

    # Real transition True -> False emits once.
    controller._set_awaiting_response(False)
    assert emissions == [True, False]
