"""Project ↔ remote-server pairing for the Local ↔ VPS location toggle.

Pairing is **by repo identity**: a project is VPS-capable when a directory
with the same basename containing a ``.git`` exists under a registered
server's ``repos_dir`` (``/opt/dev/repos/<name>`` on the Vigilia VPS). No
per-project configuration — open a project that also lives on the server and
the VPS tab appears.

``RemoteContext`` owns the async probe (one short ssh per registered server,
first match wins) and the resulting pairing state. Pure path math lives in
module functions so it is testable without Qt or a network.
"""

from __future__ import annotations

import logging
import posixpath
import threading
from collections.abc import Callable

from PySide6.QtCore import QObject, Qt, Signal

from .agent_bridge import emit_gc_safe
from .server_registry import RemoteServer, load_servers
from .ssh_runner import run_remote

log = logging.getLogger(__name__)

_PROBE_TIMEOUT = 8.0


def remote_repo_path(server: RemoteServer, project_root: str) -> str:
    """The paired repo's path on ``server`` for a local ``project_root``.

    Pairing key is the directory basename — the same name vigiliad's
    ``GET /projects`` and the phone app use for repos under ``repos_dir``.
    """
    name = posixpath.basename(project_root.rstrip("/"))
    return posixpath.join(server.repos_dir, name)


def _default_probe(server: RemoteServer, project_root: str) -> bool:
    """True when the paired repo exists on ``server`` (worker-thread only)."""
    result = run_remote(
        server,
        ["test", "-d", f"{remote_repo_path(server, project_root)}/.git"],
        timeout=_PROBE_TIMEOUT,
    )
    return result is not None and result.returncode == 0


class RemoteContext(QObject):
    """Pairing state machine: unpaired → probing → paired/unpaired.

    ``probe(project_root)`` runs the per-server existence check on a one-shot
    daemon thread (template: AppController's ``_fetch_opencode_sessions``).
    A generation counter discards stale results — a probe finishing after the
    user already switched projects must not pair the wrong root.
    """

    pairingChanged = Signal()
    # Worker → GUI handoff. object = the matched RemoteServer or None.
    _probeFinished = Signal(int, str, object)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        servers_loader: Callable[[], list[RemoteServer]] = load_servers,
        prober: Callable[[RemoteServer, str], bool] = _default_probe,
    ) -> None:
        super().__init__(parent)
        self._servers_loader = servers_loader
        self._prober = prober
        self._generation = 0
        self._probing = False
        self._project_root = ""
        self._server: RemoteServer | None = None
        # Cross-thread: the probe worker emits, state must mutate on the GUI
        # thread — explicit QueuedConnection per project-standards §4 P2.
        self._probeFinished.connect(
            self._on_probe_finished, Qt.ConnectionType.QueuedConnection
        )

    # -- state ---------------------------------------------------------------

    @property
    def paired(self) -> bool:
        return self._server is not None

    @property
    def probing(self) -> bool:
        return self._probing

    @property
    def server(self) -> RemoteServer | None:
        return self._server

    @property
    def remote_root(self) -> str:
        if self._server is None:
            return ""
        return remote_repo_path(self._server, self._project_root)

    # -- probe ---------------------------------------------------------------

    def probe(self, project_root: str) -> None:
        """(Re)probe pairing for ``project_root``; clears pairing first.

        Clearing eagerly means a stale "paired" never survives a project
        switch — the VPS tab disappears immediately and reappears only when
        the new root's probe lands.
        """
        self._generation += 1
        generation = self._generation
        self._project_root = project_root
        changed = self._server is not None
        self._server = None
        servers = self._servers_loader() if project_root else []
        if not servers:
            self._probing = False
            if changed:
                self.pairingChanged.emit()
            return
        self._probing = True
        self.pairingChanged.emit()
        threading.Thread(
            target=self._run_probe,
            args=(generation, project_root, servers),
            daemon=True,
            name="vps-pairing-probe",
        ).start()

    def _run_probe(
        self, generation: int, project_root: str, servers: list[RemoteServer]
    ) -> None:
        """Worker-thread body: first server with the repo wins.

        Every path MUST reach the emit — an unhandled exception would leave
        the context stuck in "probing" forever, hence the broad catch.
        """
        matched: RemoteServer | None = None
        try:
            for server in servers:
                if self._generation != generation:
                    return  # superseded — the newer probe owns the state
                if self._prober(server, project_root):
                    matched = server
                    break
        except Exception:
            log.exception("vps pairing probe failed")
        emit_gc_safe(self._probeFinished, generation, project_root, matched)

    def _on_probe_finished(
        self, generation: int, project_root: str, server: object
    ) -> None:
        if generation != self._generation:
            return
        self._probing = False
        self._server = server if isinstance(server, RemoteServer) else None
        if self._server is not None:
            log.info(
                "vps pairing: %s ↔ %s",
                project_root,
                f"{self._server.name}:{self.remote_root}",
            )
        self.pairingChanged.emit()
