"""AgentEventsServer — socket-server lifecycle + parsing tests.

A real reporter is simulated by connecting to the server's Unix socket and
writing JSON lines, exercising the genuine accept → per-connection handler →
`hook_received` path.

⚠ EVERY spy here connects with an explicit `Qt.ConnectionType.DirectConnection`,
and nothing in this file pumps the Qt event loop. That is not a style choice.
The emit happens on a handler thread, so an auto connection may be queued, and
the obvious way to collect a queued payload — `QCoreApplication.processEvents()`
— drains the GLOBAL queue of the session-scoped app, running `deleteLater`
deletions left by earlier QML-heavy modules and tripping the Python-3.14
cyclic-GC-vs-Qt SEGV (gotcha #10). This file WAS the suite's main source of
intermittent death for exactly that reason: 4 failures in 9 full-suite runs, as
a hang, as exit 139 and as exit 134, while passing in isolation every single
time. Direct delivery captures the payload synchronously on the handler thread,
so `wait_until` only has to wait for that thread — no loop, nothing global
touched.

What direct delivery gives up, and why it is acceptable: these tests no longer
prove that a QUEUED connection delivers. That is Qt's behaviour, not ours;
production's queued connect sites live in `app.py` and are asserted
structurally there.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from conftest import wait_until
from PySide6.QtCore import Qt

from symmetria_ide.agent_events import (
    AgentEventsServer,
    default_socket_path,
    reap_orphan_sockets,
)

# The real reporter script the IDE injects via `claude --settings`. Resolved the
# same way app.py does (relative to the package's runtime dir) so this test
# exercises the genuine on-disk script, not a copy.
_REPORTER = (
    Path(__file__).resolve().parent.parent / "runtime" / "symmetria-ide-agent-hook.py"
)


def _spy(signal, sink: list) -> None:
    """Capture a signal's payload synchronously on the emitting thread.

    Explicit rather than relying on what an auto connection resolves to for a
    plain Python callable — the whole point is that no delivery depends on the
    event loop. See the module docstring.
    """
    signal.connect(sink.append, Qt.ConnectionType.DirectConnection)


def _report(sock_path: str, payload: dict) -> None:
    """Simulate a reporter: connect, write one JSON line, close."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(2.0)
        sock.connect(sock_path)
        sock.sendall((json.dumps(payload) + "\n").encode())


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_start_creates_socket_file(tmp_path):
    path = str(tmp_path / "agents.sock")
    server = AgentEventsServer(socket_path=path)
    try:
        server.start()
        assert os.path.exists(path)
    finally:
        server.stop()


def test_stop_unlinks_socket(tmp_path):
    path = str(tmp_path / "agents.sock")
    server = AgentEventsServer(socket_path=path)
    server.start()
    server.stop()
    assert not os.path.exists(path)


def test_stop_is_idempotent(tmp_path):
    server = AgentEventsServer(socket_path=str(tmp_path / "agents.sock"))
    server.start()
    server.stop()
    server.stop()  # must not raise


def test_socket_path_property(tmp_path):
    path = str(tmp_path / "agents.sock")
    assert AgentEventsServer(socket_path=path).socket_path == path


# ---------------------------------------------------------------------------
# Receiving hook events
# ---------------------------------------------------------------------------


def test_hook_line_is_emitted(tmp_path):
    path = str(tmp_path / "agents.sock")
    server = AgentEventsServer(socket_path=path)
    received: list[dict] = []
    _spy(server.hook_received, received)
    try:
        server.start()
        _report(path, {"type": "hook", "agent_id": "100_1", "hook_event_name": "Stop"})
        wait_until(lambda: len(received) >= 1)
        assert received[0]["agent_id"] == "100_1"
        assert received[0]["hook_event_name"] == "Stop"
    finally:
        server.stop()


def test_multiple_connections_each_emit(tmp_path):
    path = str(tmp_path / "agents.sock")
    server = AgentEventsServer(socket_path=path)
    received: list[dict] = []
    _spy(server.hook_received, received)
    try:
        server.start()
        for i in range(3):
            _report(path, {"type": "hook", "agent_id": f"100_{i}"})
        wait_until(lambda: len(received) >= 3)
        assert sorted(r["agent_id"] for r in received) == ["100_0", "100_1", "100_2"]
    finally:
        server.stop()


def test_bad_json_is_skipped(tmp_path):
    path = str(tmp_path / "agents.sock")
    server = AgentEventsServer(socket_path=path)
    received: list[dict] = []
    _spy(server.hook_received, received)
    try:
        server.start()
        # A garbage line followed by a valid one on a fresh connection: the bad
        # line is dropped, the good line still arrives.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(path)
            sock.sendall(b"not json\n")
        _report(path, {"type": "hook", "agent_id": "100_9"})
        wait_until(lambda: len(received) >= 1)
        assert received[0]["agent_id"] == "100_9"
    finally:
        server.stop()


def test_non_dict_json_is_skipped(tmp_path):
    path = str(tmp_path / "agents.sock")
    server = AgentEventsServer(socket_path=path)
    received: list[dict] = []
    _spy(server.hook_received, received)
    try:
        server.start()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(path)
            sock.sendall(b"[1, 2, 3]\n")  # valid JSON, wrong shape
        _report(path, {"type": "hook", "agent_id": "100_5"})
        wait_until(lambda: len(received) >= 1)
        assert received[0]["agent_id"] == "100_5"
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# Orphan reaping + default path
# ---------------------------------------------------------------------------


def test_default_socket_path_encodes_pid(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert default_socket_path(4242) == str(tmp_path / "symmetria-ide-agents-4242.sock")
    assert default_socket_path().endswith(f"symmetria-ide-agents-{os.getpid()}.sock")


def test_reap_removes_dead_spares_live_and_own(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    dead = tmp_path / "symmetria-ide-agents-999999.sock"  # pid that cannot exist
    own = tmp_path / f"symmetria-ide-agents-{os.getpid()}.sock"
    foreign = tmp_path / "something-else.sock"
    for p in (dead, own, foreign):
        p.write_text("")
    reap_orphan_sockets()
    assert not dead.exists()  # dead pid → reaped
    assert own.exists()  # our pid → spared
    assert foreign.exists()  # not a pid-named agent socket → untouched


# ---------------------------------------------------------------------------
# End-to-end: the real reporter script → the server (the injected wire)
# ---------------------------------------------------------------------------


def _run_reporter(sock_path: str, hook_event: dict, *args: str) -> None:
    """Invoke the genuine reporter script as a subprocess, as claude would."""
    env = {
        **os.environ,
        "SYMMETRIA_AGENT_ID": "100_1",
        "SYMMETRIA_IDE_AGENT_SOCK": sock_path,
    }
    subprocess.run(
        [sys.executable, str(_REPORTER), *args],
        input=json.dumps(hook_event),
        text=True,
        env=env,
        timeout=5,
        check=True,
    )


def test_reporter_forwards_curated_fields(tmp_path):
    path = str(tmp_path / "agents.sock")
    server = AgentEventsServer(socket_path=path)
    received: list[dict] = []
    _spy(server.hook_received, received)
    try:
        server.start()
        _run_reporter(
            path,
            {
                "hook_event_name": "PreToolUse",
                "session_id": "sess-xyz",
                "tool_name": "Bash",
                "permission_mode": "plan",
                # A field NOT in the curated set must be dropped by the reporter.
                "tool_input": {"command": "rm -rf /tmp/whatever"},
            },
        )
        wait_until(lambda: len(received) >= 1)
        msg = received[0]
        assert msg["type"] == "hook"
        assert msg["agent_id"] == "100_1"  # from SYMMETRIA_AGENT_ID env
        assert msg["hook_event_name"] == "PreToolUse"
        assert msg["session_id"] == "sess-xyz"
        assert msg["tool_name"] == "Bash"
        assert msg["permission_mode"] == "plan"
        assert msg["idle_notification"] is False  # no argv marker
        assert "event_ts_ns" in msg
        assert "tool_input" not in msg  # uncurated field dropped
    finally:
        server.stop()


def test_reporter_idle_notification_argv_marker(tmp_path):
    path = str(tmp_path / "agents.sock")
    server = AgentEventsServer(socket_path=path)
    received: list[dict] = []
    _spy(server.hook_received, received)
    try:
        server.start()
        _run_reporter(path, {"hook_event_name": "Notification"}, "idle-notification")
        wait_until(lambda: len(received) >= 1)
        assert received[0]["idle_notification"] is True
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# Direct STT channel (inversion P4): stt_recording + stt_inject request/reply
# ---------------------------------------------------------------------------


def test_stt_recording_is_emitted(tmp_path):
    path = str(tmp_path / "agents.sock")
    server = AgentEventsServer(socket_path=path)
    received: list[dict] = []
    _spy(server.stt_recording_received, received)
    try:
        server.start()
        _report(path, {"type": "stt_recording", "buf": 1, "transcribing": True})
        wait_until(lambda: len(received) >= 1)
        assert received[0]["buf"] == 1
        assert received[0]["transcribing"] is True
    finally:
        server.stop()


def _run_inject_client(path: str, request: dict, reply: dict) -> threading.Thread:
    """Connect, send one stt_inject, capture the reply line. Returns the thread."""

    def client() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(8.0)
            sock.connect(path)
            sock.sendall((json.dumps(request) + "\n").encode())
            reply["msg"] = json.loads(sock.makefile("r").readline())

    t = threading.Thread(target=client, daemon=True)
    t.start()
    return t


def test_stt_inject_request_reply(tmp_path):
    path = str(tmp_path / "agents.sock")
    server = AgentEventsServer(socket_path=path)
    seen: list[dict] = []

    # Stands in for the QML paste: resolve the moment the inject is delivered.
    #
    # DIRECT, so this runs on the handler thread — production connects this
    # queued and resolves from the GUI thread, and the difference is safe to
    # erase here because `_handle_inject` registers the Future BEFORE it emits.
    # Resolving inside the emit therefore lands on an already-registered
    # Future, and the `future.result()` that follows returns immediately
    # instead of blocking. What this cannot do is collect a QUEUED delivery:
    # that needs the loop pumped, which is what made this file the suite's
    # main source of intermittent SEGVs (see the module docstring).
    def on_inject(payload: dict) -> None:
        seen.append(payload)
        server.resolve_inject(payload["request_id"], True, True, "")

    server.stt_inject_received.connect(on_inject, Qt.ConnectionType.DirectConnection)
    reply: dict = {}
    try:
        server.start()
        t = _run_inject_client(
            path, {"type": "stt_inject", "buf": 1, "text": "hi", "submit": True}, reply
        )
        wait_until(lambda: "msg" in reply, timeout=8.0)
        t.join(timeout=2.0)
        assert reply["msg"] == {
            "type": "stt_inject_result",
            "ok": True,
            "submitted": True,
            "error": "",
        }
        # The client supplied no request_id; the server stamps one and forwards it.
        assert seen and seen[0].get("request_id")
    finally:
        server.stop()


def test_stop_releases_pending_inject(tmp_path):
    # An inject that is never resolved (no QML): stop() must release the blocked
    # handler with a shutting-down reply so the dictation client never hangs.
    path = str(tmp_path / "agents.sock")
    server = AgentEventsServer(socket_path=path)
    arrived: list[dict] = []
    _spy(server.stt_inject_received, arrived)  # captured, NOT resolved
    reply: dict = {}
    server.start()
    t = _run_inject_client(path, {"type": "stt_inject", "buf": 1, "text": "hi"}, reply)
    wait_until(lambda: len(arrived) >= 1)  # handler now blocked on the Future
    server.stop()  # releases pending → handler writes the shutting-down reply
    wait_until(lambda: "msg" in reply, timeout=3.0)
    t.join(timeout=2.0)
    assert reply["msg"]["ok"] is False
    assert reply["msg"]["error"] == "shutting-down"


def test_resolve_inject_unknown_id_returns_false(tmp_path):
    # resolve_inject reports False for an id it doesn't own (advisory contract —
    # callers no longer branch on it since the bridge fallback was removed).
    server = AgentEventsServer(socket_path=str(tmp_path / "agents.sock"))
    assert server.resolve_inject("no-such-id", True, True, "") is False


def test_reporter_without_env_is_silent_noop(tmp_path):
    # Missing SYMMETRIA_AGENT_ID / _SOCK → the reporter exits 0 without
    # connecting, so the server receives nothing.
    path = str(tmp_path / "agents.sock")
    server = AgentEventsServer(socket_path=path)
    received: list[dict] = []
    _spy(server.hook_received, received)
    try:
        server.start()
        subprocess.run(
            [sys.executable, str(_REPORTER)],
            input=json.dumps({"hook_event_name": "Stop"}),
            text=True,
            env={k: v for k, v in os.environ.items() if not k.startswith("SYMMETRIA")},
            timeout=5,
            check=True,
        )
        # Give any (erroneous) delivery a brief window, then assert silence.
        # A sleep is enough and a pump is not needed: the spy is direct, so a
        # report that DID arrive would already be in the list.
        time.sleep(0.3)
        assert received == []
    finally:
        server.stop()
