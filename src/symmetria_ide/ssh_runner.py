"""SSH plumbing for the Local ↔ VPS location toggle.

The single home for ssh argv construction — every ssh invocation the remote
feature makes (pairing probe, remote git, tmux session ops, agent panes, the
hub socket forward, the remote shell) builds its argv here so the option set
cannot drift between call sites. Two invariants the options encode:

- **Identity pinning is mandatory** (``IdentitiesOnly=yes -i <key>``): a
  multi-key ssh-agent otherwise offers every key, trips the VPS's hardened
  ``MaxAuthTries=3``, and the failures feed its fail2ban — Vigilia's first
  live bootstrap got the operator's machine self-banned this way (see
  vigilia CLAUDE.md "Common Pitfalls"). Never construct a raw ssh argv.
- **ControlMaster multiplexing**: the first connection opens a shared master
  (``ControlPersist=60``); every later call rides it and skips the handshake,
  which is what makes per-scan remote git affordable.

The one deliberate NON-user of this module is sshfs (``mount_manager``):
``-o reconnect`` must own its connection — multiplexed through the master, a
dead master wedges the FUSE mount instead of letting it reconnect.
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from .agent_bridge import emit_gc_safe
from .server_registry import RemoteServer

log = logging.getLogger(__name__)

_RUN_TIMEOUT_DEFAULT = 15.0


def runtime_dir() -> Path:
    """Per-user runtime dir for control sockets / forwarded sockets."""
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(base) / "symmetria-ide"


def control_path(server: RemoteServer) -> str:
    """ControlMaster socket path for ``server`` (parent dir created on demand).

    ``%C`` is ssh's own local-host/remote/port/user hash token — it keeps the
    expanded path short (AF_UNIX paths cap at ~108 bytes) and distinct per
    destination even if two registry entries share a name.
    """
    directory = runtime_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return str(directory / f"cm-{server.name}-%C")


def ssh_base_argv(server: RemoteServer, *, batch: bool = True) -> list[str]:
    """Common ssh argv prefix up to and including the destination.

    ``batch=True`` (default) adds ``BatchMode=yes`` so non-interactive calls
    fail fast instead of hanging on a passphrase/hostkey prompt; interactive
    panes pass ``batch=False`` (via ``remote_command_argv(tty=True)``).
    """
    argv = [
        "ssh",
        "-o",
        "IdentitiesOnly=yes",
        "-i",
        os.path.expanduser(server.identity_file),
        "-o",
        "ControlMaster=auto",
        "-o",
        f"ControlPath={control_path(server)}",
        "-o",
        "ControlPersist=60",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
    ]
    if batch:
        argv += ["-o", "BatchMode=yes"]
    return argv + [server.ssh_destination]


def remote_command_argv(
    server: RemoteServer, remote_argv: list[str], *, tty: bool = False
) -> list[str]:
    """Full local argv running ``remote_argv`` on ``server``.

    ssh flattens the remote command through the remote user's shell, so every
    token is ``shlex.quote``d and joined — the remote shell splits it back
    into exactly the tokens given. ``tty=True`` inserts ``-t`` (interactive
    panes: agent tmux attach, remote shell) and drops BatchMode so an
    interactive auth prompt, should one ever appear, lands in the pane
    instead of insta-failing it.
    """
    base = ssh_base_argv(server, batch=not tty)
    if tty:
        base.insert(1, "-t")
    return base + ["--", " ".join(shlex.quote(token) for token in remote_argv)]


def run_remote(
    server: RemoteServer,
    remote_argv: list[str],
    *,
    timeout: float = _RUN_TIMEOUT_DEFAULT,
    text: bool = True,
) -> subprocess.CompletedProcess | None:
    """Run ``remote_argv`` on the server; ``None`` on transport failure.

    Worker-thread only (blocks up to ``timeout``). Mirrors
    ``git_subprocess.run_git``'s warn-and-degrade contract: callers branch on
    ``None`` / ``returncode`` rather than catching. A non-zero returncode is
    returned as-is — remote-command failure and transport failure are
    different conditions and callers may care which.

    ``text=False`` yields BYTES stdout/stderr — required by the git parsers
    (porcelain ``-z`` framing splits on ``b"\\x00"``).
    """
    argv = remote_command_argv(server, remote_argv)
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=text,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        log.warning(
            "run_remote: %s timed out after %.0fs: %s",
            server.name,
            timeout,
            " ".join(remote_argv[:4]),
        )
        return None
    except OSError:
        log.exception("run_remote: cannot exec ssh")
        return None


class SocketForward(QObject):
    """A ``ssh -N -L <local unix sock>:<remote unix sock>`` child, supervised.

    Owns the forward subprocess plus a daemon monitor thread that waits on it
    and emits ``lost`` when it exits (GC-suspended per gotcha #10 — the emit
    crosses into the GUI thread via queued delivery). Restart/backoff policy
    deliberately lives with the OWNER (AppController arms a GUI timer on
    ``lost`` only while a VPS pairing is active) — this class is just the
    process lifecycle.
    """

    ready = Signal(str)  # local socket path, emitted after successful spawn
    lost = Signal()

    def __init__(
        self,
        server: RemoteServer,
        remote_socket: str,
        local_socket: str,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._server = server
        self._remote_socket = remote_socket
        self._local_socket = local_socket
        self._proc: subprocess.Popen | None = None
        self._stopping = threading.Event()

    @property
    def local_socket(self) -> str:
        return self._local_socket

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        """Spawn the forward (idempotent while running)."""
        if self.is_running():
            return
        self._stopping.clear()
        # A stale local socket file makes ssh's bind fail even with
        # StreamLocalBindUnlink (that option governs the REMOTE end of -R;
        # the local -L endpoint is our own file to clean).
        try:
            os.unlink(self._local_socket)
        except FileNotFoundError:
            pass
        except OSError:
            log.exception("SocketForward: cannot unlink stale %s", self._local_socket)
        Path(self._local_socket).parent.mkdir(parents=True, exist_ok=True)
        argv = ssh_base_argv(self._server) + [
            "-N",
            "-o",
            "ExitOnForwardFailure=yes",
            "-L",
            f"{self._local_socket}:{self._remote_socket}",
        ]
        try:
            self._proc = subprocess.Popen(
                argv,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
        except OSError:
            log.exception("SocketForward: cannot exec ssh")
            emit_gc_safe(self.lost)
            return
        threading.Thread(
            target=self._monitor,
            args=(self._proc,),
            daemon=True,
            name=f"hub-forward-{self._server.name}",
        ).start()
        emit_gc_safe(self.ready, self._local_socket)

    def _monitor(self, proc: subprocess.Popen) -> None:
        """Wait for the forward child; report its death unless we caused it."""
        returncode = proc.wait()
        if self._stopping.is_set():
            return
        log.warning(
            "SocketForward: forward to %s exited %d",
            self._server.name,
            returncode,
        )
        emit_gc_safe(self.lost)

    def stop(self) -> None:
        """Terminate the forward and remove the local socket (idempotent)."""
        self._stopping.set()
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        try:
            os.unlink(self._local_socket)
        except OSError:
            pass


def close_control_master(server: RemoteServer) -> None:
    """Tear down the shared ControlMaster at shutdown (best-effort).

    ``ControlPersist=60`` is the crash safety net; this is the polite path so
    a clean IDE exit doesn't leave a master lingering for a minute.
    """
    try:
        subprocess.run(
            [
                "ssh",
                "-o",
                f"ControlPath={control_path(server)}",
                "-O",
                "exit",
                server.ssh_destination,
            ],
            capture_output=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        log.debug("close_control_master: no master to close for %s", server.name)
