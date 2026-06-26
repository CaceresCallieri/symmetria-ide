"""AgentEventsServer — socket-server lifecycle + parsing tests.

A real reporter is simulated by connecting to the server's Unix socket and
writing JSON lines, exercising the genuine accept → per-connection handler →
queued `hook_received` path. Delivery crosses from a handler thread to the GUI
thread via a queued signal, so assertions pump the QCoreApplication event loop.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

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


def _pump_events(predicate, timeout: float = 3.0) -> None:
    """Process Qt events until `predicate()` is true (queued-signal delivery)."""
    from PySide6.QtCore import QCoreApplication

    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.01)
    assert predicate(), "condition not reached while pumping events"


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
    server.hook_received.connect(received.append)
    try:
        server.start()
        _report(path, {"type": "hook", "agent_id": "100_1", "hook_event_name": "Stop"})
        _pump_events(lambda: len(received) >= 1)
        assert received[0]["agent_id"] == "100_1"
        assert received[0]["hook_event_name"] == "Stop"
    finally:
        server.stop()


def test_multiple_connections_each_emit(tmp_path):
    path = str(tmp_path / "agents.sock")
    server = AgentEventsServer(socket_path=path)
    received: list[dict] = []
    server.hook_received.connect(received.append)
    try:
        server.start()
        for i in range(3):
            _report(path, {"type": "hook", "agent_id": f"100_{i}"})
        _pump_events(lambda: len(received) >= 3)
        assert sorted(r["agent_id"] for r in received) == ["100_0", "100_1", "100_2"]
    finally:
        server.stop()


def test_bad_json_is_skipped(tmp_path):
    path = str(tmp_path / "agents.sock")
    server = AgentEventsServer(socket_path=path)
    received: list[dict] = []
    server.hook_received.connect(received.append)
    try:
        server.start()
        # A garbage line followed by a valid one on a fresh connection: the bad
        # line is dropped, the good line still arrives.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(path)
            sock.sendall(b"not json\n")
        _report(path, {"type": "hook", "agent_id": "100_9"})
        _pump_events(lambda: len(received) >= 1)
        assert received[0]["agent_id"] == "100_9"
    finally:
        server.stop()


def test_non_dict_json_is_skipped(tmp_path):
    path = str(tmp_path / "agents.sock")
    server = AgentEventsServer(socket_path=path)
    received: list[dict] = []
    server.hook_received.connect(received.append)
    try:
        server.start()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(path)
            sock.sendall(b"[1, 2, 3]\n")  # valid JSON, wrong shape
        _report(path, {"type": "hook", "agent_id": "100_5"})
        _pump_events(lambda: len(received) >= 1)
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
    server.hook_received.connect(received.append)
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
        _pump_events(lambda: len(received) >= 1)
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
    server.hook_received.connect(received.append)
    try:
        server.start()
        _run_reporter(path, {"hook_event_name": "Notification"}, "idle-notification")
        _pump_events(lambda: len(received) >= 1)
        assert received[0]["idle_notification"] is True
    finally:
        server.stop()


def test_reporter_without_env_is_silent_noop(tmp_path):
    # Missing SYMMETRIA_AGENT_ID / _SOCK → the reporter exits 0 without
    # connecting, so the server receives nothing.
    path = str(tmp_path / "agents.sock")
    server = AgentEventsServer(socket_path=path)
    received: list[dict] = []
    server.hook_received.connect(received.append)
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
        time.sleep(0.3)
        from PySide6.QtCore import QCoreApplication

        QCoreApplication.processEvents()
        assert received == []
    finally:
        server.stop()
