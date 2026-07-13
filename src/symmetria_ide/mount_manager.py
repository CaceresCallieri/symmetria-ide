"""SSHFS mount lifecycle for the VPS location's file tree.

The vps context hands the FM tree module a `rootPath` that must be a LOCAL
filesystem path — SSHFS is what makes the remote repo one, reusing the
shared `Symmetria.FileManager.UI` module verbatim instead of reimplementing
a remote tree (the DRY-correct move; see the VPS-toggle plan, Phase 3).

**Deliberate exception to the "every ssh rides ssh_runner's ControlMaster"
rule: sshfs owns its OWN connection.** `-o reconnect` respawns sshfs's
internal ssh when the transport drops; multiplexed through the shared
master, a dead master would wedge the FUSE mount instead of letting it
reconnect — and the `ssh -O exit` at shutdown would yank the filesystem out
from under FUSE. Identity pinning still applies (the fail2ban incident —
see ssh_runner's module docstring).

Git deliberately does NOT run against this mount (a status scan's stat
storm over the network is seconds-slow) — git executes ON the server via
ssh_runner and only its OUTPUT paths are joined onto the mountpoint
(GitController's remote mode). The mount serves the tree, file opens, and
the untracked line counts.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from .agent_bridge import emit_gc_safe
from .server_registry import RemoteServer
from .ssh_runner import runtime_dir

log = logging.getLogger(__name__)

_MOUNT_TIMEOUT = 20.0
_UNMOUNT_TIMEOUT = 5.0
_HEALTH_TIMEOUT = 2.0


def mountpoint_for(server: RemoteServer, remote_path: str) -> str:
    """Local mountpoint for a server's remote repo (not created here)."""
    repo = os.path.basename(remote_path.rstrip("/"))
    return str(runtime_dir() / "mnt" / server.name / repo)


def sshfs_argv(server: RemoteServer, remote_path: str, mountpoint: str) -> list[str]:
    """The sshfs command line (pure — unit-testable without FUSE)."""
    options = ",".join(
        [
            "reconnect",
            "ServerAliveInterval=15",
            "ServerAliveCountMax=3",
            f"IdentityFile={os.path.expanduser(server.identity_file)}",
            "IdentitiesOnly=yes",
            "BatchMode=yes",
            # Kernel-side caching keeps tree browsing snappy; the git data
            # never reads through the mount, so staleness here only affects
            # the tree listing (bounded by the poll cadence anyway).
            "cache=yes",
            "kernel_cache",
            "compression=no",
        ]
    )
    return [
        "sshfs",
        "-o",
        options,
        f"{server.ssh_destination}:{remote_path}",
        mountpoint,
    ]


def _is_mounted(mountpoint: str) -> bool:
    """True when the mountpoint appears in /proc/mounts (fuse.sshfs)."""
    try:
        with open("/proc/mounts", encoding="utf-8") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) >= 3 and parts[1] == mountpoint:
                    return True
    except OSError:
        log.exception("mount_manager: cannot read /proc/mounts")
    return False


class SshfsMount(QObject):
    """One repo's SSHFS mount: unmounted → mounting → mounted | failed.

    `mount()` runs sshfs on a one-shot daemon thread (it blocks on the ssh
    handshake); the state lands back on the GUI thread via the internal
    queued signal. `healthy()` is a cheap /proc/mounts check plus a
    deadline-bounded stat on a WORKER thread — a stale FUSE mount blocks
    stat() indefinitely, which must never freeze the GUI.
    """

    stateChanged = Signal()
    # Worker → GUI handoff: (generation, ok, message).
    _mountFinished = Signal(int, bool, str)

    def __init__(
        self,
        server: RemoteServer,
        remote_path: str,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._server = server
        self._remote_path = remote_path
        self._mountpoint = mountpoint_for(server, remote_path)
        self._state = "unmounted"
        self._generation = 0
        from PySide6.QtCore import Qt

        # queued: the mount worker emits; state mutates on the GUI thread
        # (§4 P2).
        self._mountFinished.connect(
            self._on_mount_finished, Qt.ConnectionType.QueuedConnection
        )

    @property
    def mountpoint(self) -> str:
        return self._mountpoint

    @property
    def state(self) -> str:
        return self._state

    @property
    def mounted(self) -> bool:
        return self._state == "mounted"

    # -- mount ---------------------------------------------------------------

    def mount(self) -> None:
        """Start mounting (async; idempotent while mounting/mounted)."""
        if self._state in ("mounting", "mounted"):
            return
        self._generation += 1
        self._set_state("mounting")
        threading.Thread(
            target=self._mount_worker,
            args=(self._generation,),
            daemon=True,
            name="sshfs-mount",
        ).start()

    def _mount_worker(self, generation: int) -> None:
        """Worker-thread body: reuse a live mount, else run sshfs."""
        ok = False
        message = ""
        try:
            if _is_mounted(self._mountpoint):
                ok = True  # a previous session's mount is still good
            else:
                Path(self._mountpoint).mkdir(parents=True, exist_ok=True)
                result = subprocess.run(
                    sshfs_argv(self._server, self._remote_path, self._mountpoint),
                    capture_output=True,
                    text=True,
                    timeout=_MOUNT_TIMEOUT,
                )
                ok = result.returncode == 0
                message = (result.stderr or "").strip()
        except subprocess.TimeoutExpired:
            message = f"sshfs timed out after {_MOUNT_TIMEOUT:.0f}s"
        except OSError as exc:
            message = str(exc)
        except Exception:
            log.exception("mount_manager: mount worker failed")
            message = "unexpected mount failure (see log)"
        emit_gc_safe(self._mountFinished, generation, ok, message)

    def _on_mount_finished(self, generation: int, ok: bool, message: str) -> None:
        if generation != self._generation:
            return  # superseded by a newer mount/unmount cycle
        if not ok:
            log.error(
                "mount_manager: sshfs %s -> %s failed: %s",
                self._remote_path,
                self._mountpoint,
                message or "(no stderr)",
            )
        self._set_state("mounted" if ok else "failed")

    # -- health / unmount ------------------------------------------------------

    def healthy(self) -> bool:
        """Cheap liveness check: mounted in /proc AND stat answers in time.

        The stat runs on a scratch thread with a deadline because a WEDGED
        FUSE mount blocks the calling thread in the kernel — the whole point
        of this check is to never let that thread be the GUI one.
        """
        if not _is_mounted(self._mountpoint):
            return False
        answered = threading.Event()

        def _stat() -> None:
            try:
                os.stat(self._mountpoint)
                answered.set()
            except OSError:
                pass  # leaving answered unset = unhealthy

        probe = threading.Thread(target=_stat, daemon=True, name="sshfs-health")
        probe.start()
        return answered.wait(timeout=_HEALTH_TIMEOUT)

    def unmount(self) -> None:
        """Best-effort unmount: fusermount3 -u, then lazy -z on EBUSY."""
        self._generation += 1  # invalidates any in-flight mount worker
        if not _is_mounted(self._mountpoint):
            self._set_state("unmounted")
            return
        for extra in ([], ["-z"]):
            try:
                result = subprocess.run(
                    ["fusermount3", "-u", *extra, self._mountpoint],
                    capture_output=True,
                    text=True,
                    timeout=_UNMOUNT_TIMEOUT,
                )
                if result.returncode == 0:
                    break
            except (subprocess.SubprocessError, OSError) as exc:
                log.warning("mount_manager: fusermount3 failed: %s", exc)
        self._set_state("unmounted")

    def _set_state(self, state: str) -> None:
        if state != self._state:
            self._state = state
            self.stateChanged.emit()
