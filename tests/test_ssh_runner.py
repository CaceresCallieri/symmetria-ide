"""ssh_runner — argv construction + SocketForward lifecycle.

Subprocess-free: argv builders are asserted as pure data; run_remote and
SocketForward get monkeypatched subprocess seams. The invariants under test
are the two safety-critical option guarantees (identity pinning per the
Vigilia fail2ban incident; ControlMaster multiplexing) and correct remote
quoting through ssh's argv-flattening.
"""

from __future__ import annotations

import shlex
import subprocess

import pytest
from PySide6.QtCore import Qt

from symmetria_ide import ssh_runner
from symmetria_ide.server_registry import RemoteServer
from symmetria_ide.ssh_runner import (
    SocketForward,
    close_control_master,
    remote_command_argv,
    run_remote,
    ssh_base_argv,
)

SERVER = RemoteServer(name="vps", host="203.0.113.7")

# Every remote command is wrapped in a UTF-8 locale (the sshd command
# environment has none; a POSIX-locale tmux client renders non-ASCII as "_").
LOCALE_WRAP = ["env", ssh_runner.REMOTE_LOCALE_ENV]


def _opt_pairs(argv: list[str]) -> list[str]:
    """Collect the values of every `-o` option in an ssh argv."""
    return [argv[i + 1] for i, tok in enumerate(argv) if tok == "-o"]


# ---------------------------------------------------------------------------
# ssh_base_argv
# ---------------------------------------------------------------------------


def test_base_argv_pins_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    argv = ssh_base_argv(SERVER)
    opts = _opt_pairs(argv)
    # Identity pinning is MANDATORY (multi-key agents trip the VPS's
    # MaxAuthTries=3 → fail2ban self-ban; see module docstring).
    assert "IdentitiesOnly=yes" in opts
    key_index = argv.index("-i")
    assert argv[key_index + 1].endswith("/.ssh/id_ed25519")
    assert "~" not in argv[key_index + 1]  # expanded at use time


def test_base_argv_multiplexes_and_bounds_liveness(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    opts = _opt_pairs(ssh_base_argv(SERVER))
    assert "ControlMaster=auto" in opts
    assert any(o.startswith("ControlPath=") for o in opts)
    assert "ControlPersist=60" in opts
    assert "ConnectTimeout=5" in opts
    assert "ServerAliveInterval=15" in opts
    assert "ServerAliveCountMax=3" in opts


def test_base_argv_batchmode_default_on_and_off(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert "BatchMode=yes" in _opt_pairs(ssh_base_argv(SERVER))
    assert "BatchMode=yes" not in _opt_pairs(ssh_base_argv(SERVER, batch=False))


def test_base_argv_ends_with_destination(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert ssh_base_argv(SERVER)[-1] == "dev@203.0.113.7"


def test_control_path_created_under_runtime_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    path = ssh_runner.control_path(SERVER)
    assert path.startswith(str(tmp_path / "symmetria-ide"))
    assert "%C" in path  # ssh's own hash token keeps it under AF_UNIX limits
    assert (tmp_path / "symmetria-ide").is_dir()  # parent created on demand


# ---------------------------------------------------------------------------
# remote_command_argv — quoting through ssh's argv flattening
# ---------------------------------------------------------------------------


def test_remote_command_is_quoted_as_one_trailing_token(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    argv = remote_command_argv(SERVER, ["git", "-C", "/opt/dev/repos/x", "status"])
    assert argv[-2] == "--"
    # The remote shell must split the flattened string back into exactly
    # the tokens given (behind the locale wrapper).
    assert shlex.split(argv[-1]) == LOCALE_WRAP + [
        "git",
        "-C",
        "/opt/dev/repos/x",
        "status",
    ]


@pytest.mark.parametrize(
    "token",
    ["with space", 'has"quote', "has'quote", "$HOME", "a;b", "*glob*"],
    ids=["space", "dquote", "squote", "dollar", "semicolon", "glob"],
)
def test_remote_quoting_survives_hostile_tokens(tmp_path, monkeypatch, token):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    argv = remote_command_argv(SERVER, ["echo", token])
    assert shlex.split(argv[-1]) == LOCALE_WRAP + ["echo", token]


def test_remote_command_carries_utf8_locale():
    """The locale wrapper prefixes EVERY remote command, tty or not.

    Without it the tmux client attaches in the sshd command environment's
    POSIX locale and renders every non-ASCII glyph as an underscore.
    """
    for tty in (False, True):
        argv = remote_command_argv(SERVER, ["tmux", "attach"], tty=tty)
        assert shlex.split(argv[-1])[:2] == LOCALE_WRAP
    assert "UTF-8" in ssh_runner.REMOTE_LOCALE_ENV


def test_tty_inserts_dash_t_and_drops_batchmode(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    argv = remote_command_argv(SERVER, ["tmux", "attach"], tty=True)
    assert argv[0] == "ssh"
    assert argv[1] == "-t"
    assert "BatchMode=yes" not in _opt_pairs(argv)
    # Non-tty keeps BatchMode and has no -t.
    plain = remote_command_argv(SERVER, ["true"])
    assert "-t" not in plain
    assert "BatchMode=yes" in _opt_pairs(plain)


# ---------------------------------------------------------------------------
# run_remote — warn-and-degrade contract
# ---------------------------------------------------------------------------


def test_run_remote_returns_completed_process(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    seen: dict = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    monkeypatch.setattr(ssh_runner.subprocess, "run", fake_run)
    result = run_remote(SERVER, ["true"], timeout=7)
    assert result is not None and result.returncode == 0
    assert seen["argv"][0] == "ssh"
    assert seen["kwargs"]["timeout"] == 7
    assert seen["kwargs"]["capture_output"] is True


def test_run_remote_timeout_degrades_to_none(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))

    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 0))

    monkeypatch.setattr(ssh_runner.subprocess, "run", fake_run)
    assert run_remote(SERVER, ["true"]) is None


def test_run_remote_oserror_degrades_to_none(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))

    def fake_run(argv, **kwargs):
        raise OSError("no ssh binary")

    monkeypatch.setattr(ssh_runner.subprocess, "run", fake_run)
    assert run_remote(SERVER, ["true"]) is None


def test_run_remote_nonzero_returncode_passes_through(monkeypatch, tmp_path):
    """Remote-command failure ≠ transport failure — callers branch on which."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(
        ssh_runner.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "denied"),
    )
    result = run_remote(SERVER, ["test", "-d", "/nope"])
    assert result is not None and result.returncode == 1


# ---------------------------------------------------------------------------
# SocketForward
# ---------------------------------------------------------------------------


class _FakeProc:
    """Minimal Popen stand-in: alive until wait() is unblocked by kill/terminate."""

    def __init__(self):
        import threading

        self._done = threading.Event()
        self.returncode: int | None = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self._done.wait(timeout)
        return self.returncode if self.returncode is not None else 0

    def terminate(self):
        self.terminated = True
        self.returncode = 0
        self._done.set()

    def kill(self):
        self.returncode = -9
        self._done.set()

    def die(self, returncode: int):
        """Simulate the ssh child exiting on its own."""
        self.returncode = returncode
        self._done.set()


def test_forward_argv_shape_and_ready_signal(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    local_sock = str(tmp_path / "fwd" / "hub.sock")
    captured: dict = {}
    proc = _FakeProc()

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        return proc

    monkeypatch.setattr(ssh_runner.subprocess, "Popen", fake_popen)
    forward = SocketForward(SERVER, "/home/dev/hub.sock", local_sock)
    ready: list[str] = []
    forward.ready.connect(ready.append)
    forward.start()
    try:
        argv = captured["argv"]
        assert "-N" in argv
        assert "ExitOnForwardFailure=yes" in _opt_pairs(argv)
        forward_index = argv.index("-L")
        assert argv[forward_index + 1] == f"{local_sock}:/home/dev/hub.sock"
        assert ready == [local_sock]
        assert forward.is_running()
        # start() is idempotent while running — no second Popen.
        captured.clear()
        forward.start()
        assert "argv" not in captured
    finally:
        forward.stop()


# NOTE: the two `lost`-signal tests below use EXPLICIT direct-connection
# spies and sleep-polls, NEVER `QCoreApplication.processEvents()` — pumping
# the shared session app drains earlier modules' deleteLater queue and trips
# the 3.14 GC-vs-Qt SEGV (gotcha #10; see memory
# reference/qt-pyside/processevents_shared_app_segv.md). The explicit
# DirectConnection is load-bearing: a Python lambda's wrapper takes the
# connecting (main) thread's affinity, so AutoConnection would resolve to
# queued for the monitor thread's emit and never fire without a pump.


def test_forward_stop_suppresses_lost(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    proc = _FakeProc()
    monkeypatch.setattr(ssh_runner.subprocess, "Popen", lambda *a, **k: proc)
    forward = SocketForward(SERVER, "/r.sock", str(tmp_path / "l.sock"))
    lost: list[None] = []
    forward.lost.connect(lambda: lost.append(None), Qt.ConnectionType.DirectConnection)
    forward.start()
    forward.stop()
    assert proc.terminated
    # The monitor observed a deliberate stop — lost must NOT fire. Give the
    # monitor thread a moment in which a wrong emit would have landed.
    import time

    time.sleep(0.15)
    assert lost == []


def test_forward_unexpected_death_emits_lost(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    proc = _FakeProc()
    monkeypatch.setattr(ssh_runner.subprocess, "Popen", lambda *a, **k: proc)
    forward = SocketForward(SERVER, "/r.sock", str(tmp_path / "l.sock"))
    lost: list[None] = []
    forward.lost.connect(lambda: lost.append(None), Qt.ConnectionType.DirectConnection)
    forward.start()
    proc.die(255)

    import time

    deadline = time.monotonic() + 2.0
    while not lost and time.monotonic() < deadline:
        time.sleep(0.01)
    assert lost, "monitor thread did not report the dead forward"


def test_forward_start_unlinks_stale_local_socket(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    local_sock = tmp_path / "stale.sock"
    local_sock.write_text("stale")
    proc = _FakeProc()
    monkeypatch.setattr(ssh_runner.subprocess, "Popen", lambda *a, **k: proc)
    forward = SocketForward(SERVER, "/r.sock", str(local_sock))
    forward.start()
    try:
        assert not local_sock.exists()
    finally:
        forward.stop()


# ---------------------------------------------------------------------------
# close_control_master
# ---------------------------------------------------------------------------


def test_close_control_master_argv(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    seen: dict = {}
    monkeypatch.setattr(
        ssh_runner.subprocess,
        "run",
        lambda argv, **kw: (
            seen.update(argv=argv) or subprocess.CompletedProcess(argv, 0)
        ),
    )
    close_control_master(SERVER)
    argv = seen["argv"]
    assert argv[0] == "ssh"
    assert "-O" in argv and argv[argv.index("-O") + 1] == "exit"
    assert argv[-1] == "dev@203.0.113.7"
