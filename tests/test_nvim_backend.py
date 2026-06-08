"""Tests for NvimBackend — the socket-attach RPC client.

No real nvim is spawned: we exercise the notification→signal dispatch
table and the defensive no-op-before-attach behaviour of the control
methods directly. The full socket attach + run_loop is integration-tested
by the live app (the editor surface).

This replaces the old test_nvim_backend_dispatch.py, which exercised the
ext_linegrid redraw state machine — gone now that nvim renders its grid
in the terminal and the backend only relays rpcnotify chrome channels.
"""

from __future__ import annotations

import threading

import pytest

from symmetria_ide.nvim_backend import NvimBackend


@pytest.fixture
def backend(qt_app):
    # qt_app: signals need a live QCoreApplication to emit to Python slots.
    return NvimBackend("/tmp/symmetria-test-does-not-exist.sock")


def test_chrome_signals_declared(backend):
    for sig in (
        "capsule_updated",
        "cmdline_updated",
        "completions_updated",
        "whichkey_event",
        "fm_event",
        "nav_event",
        "anchor_event",
        "minimap_event",
        "minimap_viewport_event",
        "minimap_diagnostics_event",
        "minimap_git_event",
        "closed",
    ):
        assert hasattr(backend, sig), f"missing signal {sig}"


def test_stop_event_exposed_and_unset(backend):
    assert isinstance(backend.stop_event, threading.Event)
    assert backend.stop_event.is_set() is False


@pytest.mark.parametrize(
    "channel, signal_attr",
    [
        ("capsule", "capsule_updated"),
        ("cmdline", "cmdline_updated"),
        ("completions", "completions_updated"),
        ("whichkey", "whichkey_event"),
        ("fm", "fm_event"),
        ("nav", "nav_event"),
        ("anchor", "anchor_event"),
        ("minimap", "minimap_event"),
        ("minimap_viewport", "minimap_viewport_event"),
        ("minimap_diagnostics", "minimap_diagnostics_event"),
        ("minimap_git", "minimap_git_event"),
    ],
)
def test_dispatch_routes_channel_to_signal(backend, channel, signal_attr):
    """An rpcnotify on a known channel re-emits args[0] on its signal."""
    received: list = []
    getattr(backend, signal_attr).connect(received.append)
    payload = {"id": "x", "value": "1"}
    backend._dispatch_notification(channel, [payload])
    assert received == [payload]


def test_dispatch_unknown_channel_is_ignored(backend):
    backend._dispatch_notification("totally_unknown_channel", [{"a": 1}])  # no raise


def test_dispatch_non_dict_payload_is_ignored(backend):
    """A malformed (non-dict) payload is dropped, not emitted."""
    received: list = []
    backend.capsule_updated.connect(received.append)
    backend._dispatch_notification("capsule", ["not a dict"])
    assert received == []


def test_dispatch_empty_args_emits_empty_dict(backend):
    received: list = []
    backend.capsule_updated.connect(received.append)
    backend._dispatch_notification("capsule", [])
    assert received == [{}]


def test_channel_to_signal_table_matches_real_signals(backend):
    """Every channel in the dispatch table maps to an attribute that
    actually exists — guards against a typo silently dropping a channel."""
    for channel, attr in NvimBackend._CHANNEL_TO_SIGNAL.items():
        assert hasattr(backend, attr), f"{channel} -> missing signal {attr}"


def test_control_methods_noop_before_attach(backend):
    """input/edit_file/set_current_dir are called from the GUI thread and
    may fire before the worker has attached (self._nvim is None) — they
    must no-op, not raise."""
    backend.input("ihello")
    backend.edit_file("/tmp/foo.txt")
    backend.set_current_dir("/tmp")


def test_stop_before_start_is_noop(backend):
    backend.stop()  # never started — must not raise
