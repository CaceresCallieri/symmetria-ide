"""AgentBridgeClient — wire-protocol and lifecycle tests.

A real Unix-socket server stands in for the shell's agent-bridge.py, so
these tests exercise the genuine connect / replay / publish / subscribe
paths without any shell dependency.

⚠ Nothing here pumps the Qt event loop, and spies connect with an explicit
`Qt.ConnectionType.DirectConnection`. Snapshot delivery crosses from the reader
thread, so an auto connection may be queued and the tempting way to collect it
— `QCoreApplication.processEvents()` — drains the GLOBAL queue of the
session-scoped app and runs `deleteLater` deletions posted by earlier
QML-heavy modules, tripping the Python-3.14 cyclic-GC-vs-Qt SEGV (gotcha #10).
`tests/test_agent_events.py` did exactly that and was the suite's main source
of intermittent death; see `conftest.wait_until` for the measurements.

The one thing that genuinely needed the loop is the 200ms title debounce, which
is a `QTimer` on the GUI thread. Those tests call `_flush_titles` — the timer's
own slot — by hand instead, and assert the timer is armed separately, so the
debounce is still covered without a running loop.
"""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest
from conftest import wait_until
from PySide6.QtCore import Qt

from symmetria_ide.agent_bridge import AgentBridgeClient


class FakeBridgeServer:
    """Minimal stand-in for agent-bridge.py: accepts one client, records
    every JSON line, and can push lines back (snapshots)."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.received: list[dict] = []
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(path)
        self._server.listen(2)
        self._conn: socket.socket | None = None
        self._conn_ready = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        self._server.settimeout(
            0.2
        )  # set once; repeated calls inside the loop are no-ops
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except (TimeoutError, OSError):
                continue
            self._conn = conn
            self._conn_ready.set()
            try:
                reader = conn.makefile("r", encoding="utf-8")
                for line in reader:
                    text = line.strip()
                    if text:
                        self.received.append(json.loads(text))
            except OSError:
                pass

    def wait_for_messages(self, count: int, timeout: float = 3.0) -> list[dict]:
        deadline = time.monotonic() + timeout
        while len(self.received) < count and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(self.received) >= count, (
            f"expected {count} messages, got {len(self.received)}: {self.received}"
        )
        return self.received

    def push(self, payload: dict) -> None:
        assert self._conn_ready.wait(timeout=3.0), "client never connected"
        assert self._conn is not None
        self._conn.sendall((json.dumps(payload) + "\n").encode())

    def drop_client(self) -> None:
        if self._conn is not None:
            self._conn.shutdown(socket.SHUT_RDWR)
            self._conn.close()
            self._conn = None
            self._conn_ready.clear()

    def close(self) -> None:
        self._stop.set()
        self.drop_client()
        self._server.close()
        self._thread.join(timeout=2.0)


@pytest.fixture
def bridge_server(tmp_path):
    server = FakeBridgeServer(str(tmp_path / "bridge.sock"))
    yield server
    server.close()


@pytest.fixture
def client(bridge_server):
    c = AgentBridgeClient(socket_path=bridge_server.path)
    yield c
    c.stop()


def _spy(signal, sink: list) -> None:
    """Capture a signal's payload synchronously on the emitting thread.

    Explicit rather than relying on what an auto connection resolves to for a
    plain Python callable — no delivery here may depend on the event loop. See
    the module docstring.
    """
    signal.connect(sink.append, Qt.ConnectionType.DirectConnection)


def _instance(slot: int, **overrides) -> dict:
    base = {
        "buf": slot,
        "cwd": "/home/jc/projects/demo",
        "project": "demo",
        "spawn_type": "fresh",
        "color_idx": slot,
        "dangerous": True,
        "title": "",
        "spawned_at": 1234567890,
        "active": True,
        "agent_type": "claude",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Connect-time replay
# ---------------------------------------------------------------------------


def test_connect_replays_hello_sync_subscribe_in_order(bridge_server, client):
    client.start()
    msgs = bridge_server.wait_for_messages(3)
    assert [m["type"] for m in msgs[:3]] == ["hello", "sync", "subscribe"]


def test_hello_carries_ide_pid_and_empty_nvim_socket(bridge_server, client):
    import os

    client.start()
    hello = bridge_server.wait_for_messages(1)[0]
    assert hello["nvim_pid"] == os.getpid()
    assert hello["nvim_socket"] == ""


def test_initial_sync_is_empty_without_registered_instances(bridge_server, client):
    client.start()
    sync = bridge_server.wait_for_messages(2)[1]
    assert sync["type"] == "sync"
    assert sync["instances"] == []


# ---------------------------------------------------------------------------
# Publish API
# ---------------------------------------------------------------------------


def test_notify_spawn_publishes_added_with_instance_payload(bridge_server, client):
    client.start()
    bridge_server.wait_for_messages(3)
    client.notify_spawn(_instance(1))
    added = bridge_server.wait_for_messages(4)[3]
    assert added["type"] == "added"
    assert added["instance"]["buf"] == 1
    assert added["instance"]["agent_type"] == "claude"
    assert added["instance"]["dangerous"] is True


def test_notify_remove_publishes_removed_with_slot_as_buf(bridge_server, client):
    client.start()
    bridge_server.wait_for_messages(3)
    client.notify_spawn(_instance(2))
    client.notify_remove(2)
    removed = bridge_server.wait_for_messages(5)[4]
    assert removed == {"type": "removed", "nvim_pid": removed["nvim_pid"], "buf": 2}


def test_notify_focus_publishes_focus_and_updates_active_flags(bridge_server, client):
    client.start()
    bridge_server.wait_for_messages(3)
    client.notify_spawn(_instance(1))
    client.notify_spawn(_instance(2, active=False))
    client.notify_focus(2)
    focus = bridge_server.wait_for_messages(6)[5]
    assert focus["type"] == "focus" and focus["buf"] == 2
    # Registry reflects the focus flip — visible in the next reconnect sync.
    bridge_server.drop_client()
    msgs_before = len(bridge_server.received)
    bridge_server.wait_for_messages(msgs_before + 2, timeout=8.0)
    sync = next(m for m in bridge_server.received[msgs_before:] if m["type"] == "sync")
    actives = {i["buf"]: i["active"] for i in sync["instances"]}
    assert actives == {1: False, 2: True}


def test_notify_title_debounces_and_publishes_updated(bridge_server, client):
    client.start()
    bridge_server.wait_for_messages(3)
    client.notify_spawn(_instance(1))
    client.notify_title(1, "first")
    client.notify_title(1, "final title")
    # The debounce is a single-shot GUI-thread QTimer, so firing it for real
    # needs a running loop — which is the one thing tests here may not do.
    # Asserting it is ARMED and then calling its own slot covers the same
    # contract: `notify_title` defers rather than sends, and one flush emits one
    # coalesced message. What is not covered is Qt actually firing the timer.
    assert client._title_timer.isActive(), "notify_title must arm the debounce"
    assert client._title_timer.isSingleShot(), "a repeating timer would re-send"
    client._flush_titles()
    wait_until(
        lambda: any(m["type"] == "updated" for m in bridge_server.received),
        timeout=3.0,
    )
    updates = [m for m in bridge_server.received if m["type"] == "updated"]
    assert len(updates) == 1, "debounce must coalesce rapid title churn"
    assert updates[0]["title"] == "final title"
    assert updates[0]["buf"] == 1


def test_notify_title_for_unknown_slot_is_dropped(bridge_server, client):
    client.start()
    bridge_server.wait_for_messages(3)
    client.notify_title(9, "ghost")
    # Absence is asserted behind a POSITIVE signal rather than a wait: register a
    # real slot, title it, and let that message land. Because both went through
    # the same single flush, the ghost has had its full chance by the time the
    # real one arrives — no sleep to tune, and no false pass on a slow machine.
    client.notify_spawn(_instance(1))
    client.notify_title(1, "real")
    client._flush_titles()
    wait_until(
        lambda: any(
            m["type"] == "updated" and m.get("title") == "real"
            for m in bridge_server.received
        )
    )
    assert not any(m.get("title") == "ghost" for m in bridge_server.received)


# ---------------------------------------------------------------------------
# notify_activity (inversion Phase 2 — publish local activity outward)
# ---------------------------------------------------------------------------


def test_notify_activity_wire_format(bridge_server, client):
    client.start()
    bridge_server.wait_for_messages(3)  # hello/sync/subscribe
    client.notify_spawn(_instance(1))
    client.notify_activity(
        1, state="working", tool="Running", in_plan_mode=True, session_id="sess-1"
    )
    wait_until(
        lambda: any(
            m["type"] == "updated" and "activity_state" in m
            for m in bridge_server.received
        )
    )
    msg = next(
        m
        for m in bridge_server.received
        if m["type"] == "updated" and "activity_state" in m
    )
    assert msg["buf"] == 1
    assert msg["activity_state"] == "working"
    assert msg["activity_tool"] == "Running"
    assert msg["in_plan_mode"] is True
    assert msg["session_id"] == "sess-1"


def test_notify_activity_omits_empty_session_id(bridge_server, client):
    # A clear (idle) publish carries no session id: the field is omitted from the
    # wire message AND the sticky id stored from an earlier event is preserved on
    # the instance record (two distinct behaviors — wire omission vs preservation).
    client.start()
    bridge_server.wait_for_messages(3)
    client.notify_spawn(_instance(1))
    client.notify_activity(
        1, state="working", tool="", in_plan_mode=False, session_id="sess-orig"
    )
    client.notify_activity(1, state="", tool="", in_plan_mode=False)  # clear, no id
    wait_until(
        lambda: (
            sum(
                m["type"] == "updated" and "activity_state" in m
                for m in bridge_server.received
            )
            >= 2
        )
    )
    activity_updates = [
        m
        for m in bridge_server.received
        if m["type"] == "updated" and "activity_state" in m
    ]
    assert "session_id" not in activity_updates[-1]  # the clear omits it
    # ... but the captured id survives on the instance record (sticky).
    assert client._instances[1]["session_id"] == "sess-orig"


def test_notify_activity_for_unknown_slot_is_dropped(bridge_server, client):
    client.start()
    bridge_server.wait_for_messages(3)
    client.notify_activity(9, state="working", tool="Running", in_plan_mode=False)
    # Same shape as the title case: a registered slot's activity is sent AFTER
    # the ghost's on the same socket, so its arrival proves the ghost's would
    # already have arrived had one been sent.
    client.notify_spawn(_instance(1))
    client.notify_activity(1, state="thinking", tool="", in_plan_mode=False)
    wait_until(
        lambda: any(
            m.get("activity_state") == "thinking" for m in bridge_server.received
        )
    )
    assert not any(m.get("activity_state") == "working" for m in bridge_server.received)


def test_notify_activity_carried_in_reconnect_sync(bridge_server, client):
    # Activity stored on the instance must ride a reconnect `sync` so a bridge
    # restart re-learns the agent's current state without waiting for the next
    # hook event.
    client.start()
    bridge_server.wait_for_messages(3)
    client.notify_spawn(_instance(1))
    client.notify_activity(1, state="thinking", tool="", in_plan_mode=False)
    wait_until(
        lambda: any(
            m["type"] == "updated" and "activity_state" in m
            for m in bridge_server.received
        )
    )
    # Drop the connection; the reader reconnects and replays hello/sync/subscribe.
    bridge_server.drop_client()
    wait_until(
        lambda: sum(m["type"] == "sync" for m in bridge_server.received) >= 2,
        timeout=5.0,
    )
    last_sync = [m for m in bridge_server.received if m["type"] == "sync"][-1]
    inst = next(i for i in last_sync["instances"] if i["buf"] == 1)
    assert inst["activity_state"] == "thinking"
    assert inst["activity_tool"] == ""
    assert inst["in_plan_mode"] is False


# ---------------------------------------------------------------------------
# Subscribe feed
# ---------------------------------------------------------------------------


def test_snapshot_lines_emit_snapshot_received(bridge_server, client):
    received: list[dict] = []
    _spy(client.snapshot_received, received)
    client.start()
    bridge_server.wait_for_messages(3)
    bridge_server.push(
        {"agents": [{"id": "1_1", "activity_state": "working"}], "projects": ["demo"]}
    )
    wait_until(lambda: len(received) == 1)
    assert received[0]["agents"][0]["activity_state"] == "working"


def test_malformed_snapshot_line_is_skipped(bridge_server, client):
    received: list[dict] = []
    _spy(client.snapshot_received, received)
    client.start()
    bridge_server.wait_for_messages(3)
    assert bridge_server._conn_ready.wait(timeout=3.0)
    bridge_server._conn.sendall(b"this is not json\n")
    bridge_server.push({"agents": [], "projects": []})
    wait_until(lambda: len(received) == 1)
    assert received[0] == {"agents": [], "projects": []}


# ---------------------------------------------------------------------------
# Inbound routing (agent-ownership inversion, Phase 4)
#
# The bridge no longer routes inject commands back to this client — STT
# injection is a direct shell→IDE round-trip now. Every inbound line is a
# snapshot, even one that still carries a "type" field.
# ---------------------------------------------------------------------------


def test_all_inbound_lines_route_to_snapshot(bridge_server, client):
    snapshots: list[dict] = []
    _spy(client.snapshot_received, snapshots)
    client.start()
    bridge_server.wait_for_messages(3)
    # A line that in the old protocol would have been an inject command now
    # simply arrives as a snapshot — there is no inject channel anymore.
    bridge_server.push(
        {"type": "inject", "request_id": "r1", "buf": 2, "text": "hola", "submit": True}
    )
    bridge_server.push({"agents": [], "projects": []})
    wait_until(lambda: len(snapshots) == 2)
    assert snapshots[0]["request_id"] == "r1"
    assert snapshots[1] == {"agents": [], "projects": []}


# ---------------------------------------------------------------------------
# Reconnect + lifecycle
# ---------------------------------------------------------------------------


def test_reconnect_replays_sync_with_registered_instances(bridge_server, client):
    client.start()
    bridge_server.wait_for_messages(3)
    client.notify_spawn(_instance(3))
    bridge_server.wait_for_messages(4)
    before = len(bridge_server.received)
    bridge_server.drop_client()
    bridge_server.wait_for_messages(before + 3, timeout=8.0)
    replay = bridge_server.received[before:]
    assert [m["type"] for m in replay[:3]] == ["hello", "sync", "subscribe"]
    assert [i["buf"] for i in replay[1]["instances"]] == [3]


def test_connection_changed_signals_connect_and_drop(bridge_server, client):
    states: list[bool] = []
    _spy(client.connection_changed, states)
    client.start()
    bridge_server.wait_for_messages(3)
    wait_until(lambda: states == [True])
    bridge_server.drop_client()
    wait_until(lambda: len(states) >= 2, timeout=8.0)
    assert states[1] is False


def test_publish_without_bridge_is_a_silent_no_op(tmp_path):
    client = AgentBridgeClient(socket_path=str(tmp_path / "absent.sock"))
    # No start() — fully disconnected. Must not raise.
    client.notify_spawn(_instance(1))
    client.notify_focus(1)
    client.notify_remove(1)
    client.stop()


def test_stop_sends_goodbye_and_joins_worker(bridge_server, client):
    client.start()
    bridge_server.wait_for_messages(3)
    client.stop()
    goodbye = bridge_server.wait_for_messages(4)[3]
    assert goodbye["type"] == "goodbye"
    assert client._reader_thread is None


def test_start_is_idempotent(bridge_server, client):
    client.start()
    first_thread = client._reader_thread
    client.start()
    assert client._reader_thread is first_thread
