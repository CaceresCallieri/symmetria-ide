"""Shutdown-path tests for NvimBackend.

Covers the threading.Event wiring added in tech-debt #3. Does NOT spawn a
real nvim process — these tests monkeypatch pynvim internals so they run
headless on any CI runner without the nvim binary on PATH.
"""

from __future__ import annotations

import os
import threading
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication  # noqa: E402

from symmetria_ide.nvim_backend import NvimBackend  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QCoreApplication.instance() or QCoreApplication([])
    yield app


def test_stop_event_initially_clear(qapp: QCoreApplication) -> None:
    """A freshly-constructed backend has the event clear — no shutdown yet."""
    backend = NvimBackend()
    assert backend.stop_event.is_set() is False


def test_stop_sets_event_even_without_nvim(qapp: QCoreApplication) -> None:
    """`stop()` must set the event synchronously, before touching RPC.

    If the nvim handle was never attached (e.g. spawn failed, or the
    caller bails early), `stop()` still has to unblock any waiter.
    """
    backend = NvimBackend()
    assert backend._nvim is None

    backend.stop()

    assert backend.stop_event.is_set() is True


def test_stop_event_set_before_rpc_quit(qapp: QCoreApplication) -> None:
    """Ordering guarantee: event fires BEFORE any RPC call.

    Any observer (tests, future coordinator) must be able to see
    `stop_event.is_set() is True` the moment teardown starts — not after
    the RPC round-trip completes.
    """
    backend = NvimBackend()

    event_was_set_at_rpc_time: list[bool] = []
    fake_nvim = MagicMock()

    def record(_fn):
        event_was_set_at_rpc_time.append(backend.stop_event.is_set())

    fake_nvim.async_call.side_effect = record
    backend._nvim = fake_nvim  # type: ignore[assignment]

    backend.stop()

    assert event_was_set_at_rpc_time == [True]


def test_stop_event_set_on_loop_exit(qapp: QCoreApplication) -> None:
    """`_run_loop`'s finally block must set the event on crash paths too.

    Simulates nvim closing its channel: `run_loop` raises EOFError, the
    worker unwinds, and any `stop_event.wait(timeout=...)` must return
    promptly.
    """
    backend = NvimBackend()
    fake_nvim = MagicMock()
    fake_nvim.run_loop.side_effect = EOFError("channel closed")
    backend._nvim = fake_nvim  # type: ignore[assignment]

    # Run the loop body inline — no real thread — so we can observe the
    # finally block's side effect deterministically.
    backend._run_loop()

    assert backend.stop_event.is_set() is True


def test_stop_event_wait_unblocks_within_timeout(qapp: QCoreApplication) -> None:
    """Standard wait pattern: stop() → stop_event.wait(0.5) returns True.

    This is the contract external callers rely on: instead of polling
    `_worker.is_alive()`, they can `wait(timeout=...)`.
    """
    backend = NvimBackend()

    def background_stop():
        backend.stop()

    worker = threading.Thread(target=background_stop, daemon=True)
    worker.start()

    assert backend.stop_event.wait(timeout=0.5) is True
    worker.join(timeout=0.5)
