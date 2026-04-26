"""Unit tests for `AppController.permissionMode` — the SDK-permission pill state.

Covers the four-step state machine the AppController exposes to QML:

- The `permissionMode` property starts at the canonical `"default"`.
- `cycle_permission_mode` writes the next mode (in cycle order) to the
  session host but does NOT optimistically mutate the property —
  the sidecar's `permission_mode_changed` echo is the source of truth.
- A well-formed `permission_mode_changed` event flips the property and
  emits exactly one signal.
- A malformed mode in the event is silently ignored (forward compat).
- `_set_permission_mode` is idempotent — repeated echoes don't re-emit.
- `_on_session_closed` resets the property back to `"default"` so the
  pill doesn't lie about the dead subprocess's last mode.

Hermetic shape: same `_FakeSessionHost` swap as `test_app_controller_awaiting.py`.
No real subprocess, no QML engine.
"""

from __future__ import annotations

import pytest

from symmetria_ide.app import AppController


class _FakeSessionHost:
    """Stand-in for SessionHost — extends the awaiting-test fake with a
    `set_permission_mode_calls` ledger so we can assert the controller
    dispatched the right mode in the right order."""

    def __init__(self, instance_index: int = 0) -> None:
        # Mirror the real `SessionHost.__init__(..., instance_index=...)` shape so
        # Phase B fixtures can construct N fakes with distinct slot ids.
        self.instance_index = instance_index
        self.is_running = False
        self.start_calls: list[str] = []
        self.send_calls: list[str] = []
        self.stop_calls = 0
        self.permission_calls: list[tuple[str, str]] = []
        # New: every cycle dispatches one entry here. Order matters —
        # the cycle test reads positionally to verify wraparound.
        self.set_permission_mode_calls: list[str] = []

    def start(self, prompt: str) -> None:
        self.start_calls.append(prompt)

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
    """Construct an `AppController` with the session host swapped for a fake.

    Same pattern as `test_app_controller_awaiting.py` — replace the
    field after `__init__` since the constructor wires connections
    against the real SessionHost (which we never `start()`, so its
    threads never spawn).
    """
    ctrl = AppController()
    # Phase A pool refactor: replace slot 1's real host with the fake.
    ctrl._session_hosts[1] = _FakeSessionHost(instance_index=1)  # type: ignore[assignment]  # pyright: ignore[reportPrivateUsage]
    yield ctrl
    ctrl.shutdown()


def _capture_mode_emissions(ctrl: AppController) -> list[str]:
    """Capture every `permissionModeChanged` emission as the post-emit value."""
    emissions: list[str] = []
    ctrl.permissionModeChanged.connect(lambda: emissions.append(ctrl.permissionMode))
    return emissions


# ---------------------------------------------------------------------------
# Initial state + property exposure
# ---------------------------------------------------------------------------


def test_initial_permission_mode_is_default(controller):
    """Sidecar starts in `default`; the controller mirrors that initial state."""
    assert controller.permissionMode == "default"


# ---------------------------------------------------------------------------
# cycle_permission_mode — dispatch only, no optimistic mutation
# ---------------------------------------------------------------------------


def test_cycle_permission_mode_dispatches_next_mode(controller):
    """First cycle from `default` writes `acceptEdits` to the host."""
    emissions = _capture_mode_emissions(controller)

    controller.cycle_permission_mode()

    assert controller._session_hosts[1].set_permission_mode_calls == ["acceptEdits"]
    # Property MUST NOT mutate — the sidecar echo is the source of truth.
    assert controller.permissionMode == "default"
    assert emissions == []


def test_cycle_permission_mode_full_cycle_order(controller):
    """Four cycles wrap default -> acceptEdits -> bypassPermissions -> plan -> default.

    Verifies the cycle reads from the property's CURRENT value each
    time, not from a private counter — we feed echoes between calls
    to simulate the sidecar's confirmation arriving.
    """
    fake = controller._session_hosts[1]

    controller.cycle_permission_mode()
    assert fake.set_permission_mode_calls[-1] == "acceptEdits"
    controller._on_session_event_for(
        1, {"type": "permission_mode_changed", "mode": "acceptEdits"}
    )

    controller.cycle_permission_mode()
    assert fake.set_permission_mode_calls[-1] == "bypassPermissions"
    controller._on_session_event_for(
        1, {"type": "permission_mode_changed", "mode": "bypassPermissions"}
    )

    controller.cycle_permission_mode()
    assert fake.set_permission_mode_calls[-1] == "plan"
    controller._on_session_event_for(
        1, {"type": "permission_mode_changed", "mode": "plan"}
    )

    # Wraparound: from `plan` the next mode is `default` again.
    controller.cycle_permission_mode()
    assert fake.set_permission_mode_calls[-1] == "default"
    assert fake.set_permission_mode_calls == [
        "acceptEdits",
        "bypassPermissions",
        "plan",
        "default",
    ]


def test_cycle_after_event_resyncs_from_actual_mode(controller):
    """Cycle reads the SDK-confirmed mode, not the locally-requested one.

    Scenario: user cycles from default; before the echo arrives the
    SDK rejects the transition (or returns to default). The next
    cycle should advance from the ACTUAL mode (default) not the
    requested one (acceptEdits).
    """
    controller.cycle_permission_mode()  # requests acceptEdits
    # No echo arrives — SDK rejected the transition silently.
    # Property is still at default.
    assert controller.permissionMode == "default"

    controller.cycle_permission_mode()
    # Without optimistic mutation, this still computes "next after default"
    # which is acceptEdits — not "next after acceptEdits".
    assert controller._session_hosts[1].set_permission_mode_calls == [
        "acceptEdits",
        "acceptEdits",
    ]


# ---------------------------------------------------------------------------
# permission_mode_changed event — the authoritative update path
# ---------------------------------------------------------------------------


def test_permission_mode_changed_event_updates_property(controller):
    """A well-formed echo flips the property and emits exactly one signal."""
    emissions = _capture_mode_emissions(controller)

    controller._on_session_event_for(
        1, {"type": "permission_mode_changed", "mode": "acceptEdits"}
    )

    assert controller.permissionMode == "acceptEdits"
    assert emissions == ["acceptEdits"]


def test_permission_mode_changed_event_invalid_mode_is_ignored(controller):
    """Garbage modes (forward-compat / malformed envelope) must NOT mutate."""
    emissions = _capture_mode_emissions(controller)

    for bad in ("garbage", "ACCEPT_EDITS", "", "auto", "dontAsk"):
        controller._on_session_event_for(
            1, {"type": "permission_mode_changed", "mode": bad}
        )

    assert controller.permissionMode == "default"
    assert emissions == []


def test_permission_mode_changed_event_missing_mode_is_ignored(controller):
    """Missing `mode` field — defensive guard against malformed envelopes."""
    emissions = _capture_mode_emissions(controller)

    controller._on_session_event_for(1, {"type": "permission_mode_changed"})

    assert controller.permissionMode == "default"
    assert emissions == []


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_set_permission_mode_idempotent(controller):
    """Re-asserting the same mode does NOT re-emit.

    Sidecar emits a start-time echo AND an echo after every accepted
    transition. A no-op transition (default -> default) lands twice
    on the controller; the idempotency guard makes the QML pill
    skip the redundant re-bind.
    """
    emissions = _capture_mode_emissions(controller)

    # Start-time echo: default -> default, no-op.
    controller._set_permission_mode_for(1, "default")
    assert emissions == []

    # Real transition: emits once.
    controller._set_permission_mode_for(1, "plan")
    assert emissions == ["plan"]

    # Re-asserting plan: still no re-emit.
    controller._set_permission_mode_for(1, "plan")
    assert emissions == ["plan"]


# ---------------------------------------------------------------------------
# Subprocess-closed reset
# ---------------------------------------------------------------------------


def test_session_closed_resets_to_default(controller):
    """Subprocess EOF / SIGTERM / `<leader>aN` reset puts the pill back to default.

    The new sidecar's start-time echo would override this anyway, but
    the synchronous reset prevents the visible window where the pill
    shows the dead subprocess's last mode.
    """
    controller._set_permission_mode_for(1, "bypassPermissions")
    emissions = _capture_mode_emissions(controller)

    controller._on_session_closed_for(1)

    assert controller.permissionMode == "default"
    assert emissions == ["default"]


# ---------------------------------------------------------------------------
# Cycle when no session is running
# ---------------------------------------------------------------------------


def test_cycle_when_no_session_running_still_dispatches_to_host(controller):
    """cycle_permission_mode does NOT guard on is_running — it always dispatches.

    The split of responsibility: the controller dispatches unconditionally;
    SessionHost._write_command silently drops the command when _proc is None.
    This test documents that contract so future refactors don't accidentally
    add a guard here (which would prevent modes from cycling correctly even
    when the sidecar starts a moment later).

    The host-level drop is covered separately in
    test_send_set_permission_mode_without_subprocess_is_a_noop.
    """
    # Default fake has is_running = False
    assert controller._session_hosts[1].is_running is False

    controller.cycle_permission_mode()

    # Controller dispatches regardless — host decides whether to drop.
    assert controller._session_hosts[1].set_permission_mode_calls == ["acceptEdits"]
    # Property must not mutate without an echo.
    assert controller.permissionMode == "default"
