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
import shlex
import threading
import time
from collections.abc import Callable

from PySide6.QtCore import QObject, Qt, Signal

from . import agent_harness
from .agent_bridge import emit_gc_safe
from .server_registry import RemoteServer, load_servers
from .ssh_runner import remote_command_argv, run_remote

log = logging.getLogger(__name__)

_PROBE_TIMEOUT = 8.0

# tmux list-sessions -F format for the attach picker: one row per session,
# tab-separated so names with spaces survive (tmux names can't contain tabs).
TMUX_LIST_FORMAT = "#{session_name}\t#{session_created}\t#{pane_current_path}"


def remote_repo_path(server: RemoteServer, project_root: str) -> str:
    """The paired repo's path on ``server`` for a local ``project_root``.

    Pairing key is the directory basename — the same name vigiliad's
    ``GET /projects`` and the phone app use for repos under ``repos_dir``.
    """
    name = posixpath.basename(project_root.rstrip("/"))
    return posixpath.join(server.repos_dir, name)


def _default_probe(server: RemoteServer, project_root: str) -> bool:
    """True when the paired repo exists on ``server`` (worker-thread only).

    ``sh -c 'test …'`` rather than a bare ``test`` argv: the locale wrapper
    (``env LANG=… <cmd>``) resolves its command from PATH, so a bare
    ``test`` would depend on the external coreutils binary — the shell
    builtin has no such dependency on minimal userlands.
    """
    repo_git_dir = f"{remote_repo_path(server, project_root)}/.git"
    result = run_remote(
        server,
        ["sh", "-c", f"test -d {shlex.quote(repo_git_dir)}"],
        timeout=_PROBE_TIMEOUT,
    )
    return result is not None and result.returncode == 0


def vps_tmux_session_name(project_root: str, slot: int) -> str:
    """tmux session name for an IDE-spawned VPS agent: ``<repo>-vps-<slot>``.

    Deliberately NOT ``agent_harness.tmux_session_name`` (its path-hash
    suffix disambiguates same-basename projects across LOCAL cwds — remotely
    there is exactly one repo per basename under repos_dir, and the hash
    reads as noise in the phone app's session list). Slot-keyed, so a
    restarted IDE re-attaches its own prior sessions via ``new-session -A``
    instead of piling up fresh ones — the same idempotence the local tmux
    substrate gets from stable names.
    """
    name = posixpath.basename(project_root.rstrip("/"))
    return f"{name}-vps-{slot}"


def parse_tmux_sessions(stdout: str, remote_root: str) -> list[dict]:
    """Parse ``tmux list-sessions -F TMUX_LIST_FORMAT`` for the attach picker.

    Keeps only sessions whose active pane cwd is the paired repo (or inside
    it) — a session on another project is attach-able in principle but
    meaningless in THIS project's picker. Rows are shaped for the picker
    delegate: ``{"name", "when", "cwd"}``, newest first (matching the
    opencode picker's recency ordering).
    """
    root = remote_root.rstrip("/")
    rows: list[dict] = []
    for line in stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        name, created_raw, cwd = parts[0], parts[1], parts[2]
        if not name:
            continue
        if cwd != root and not cwd.startswith(root + "/"):
            continue
        try:
            created = int(created_raw)
        except ValueError:
            created = 0
        when = (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(created)) if created else ""
        )
        rows.append({"name": name, "when": when, "cwd": cwd, "created": created})
    rows.sort(key=lambda row: row["created"], reverse=True)
    for row in rows:
        del row["created"]
    return rows


def remote_agent_argv(
    server: RemoteServer,
    record: dict,
    *,
    model: str = "",
    effort: str = "",
) -> list[str]:
    """Local argv for a VPS agent pane: ssh -t → tmux new -A → claude.

    The inner claude argv is deliberately MINIMAL compared to the local
    ``agent_harness.spawn_argv``: no ``env SYMMETRIA_AGENT_ID`` wrapper, no
    ``--settings`` reporter registration, no ``--mcp-config`` — the VPS
    agent environment is Vigilia-managed (its hooks report to the server's
    agent hub keyed by tmux session name; provision/04 installs them). Only
    the launch-shaping flags travel: dangerous, spawn-type, and the
    committed marker's model/effort defaults (the marker lives in the repo,
    so the same defaults apply on both sides of the pairing).

    ``spawn_type == "attach"`` produces an EMPTY inner argv — ``tmux
    new-session -A`` with no command is a pure attach-or-shell, which is
    exactly the resume-a-phone-session flow.
    """
    spec = agent_harness.HARNESSES[record.get("harness", "claude")]
    inner: list[str] = []
    spawn_type = record.get("spawn_type", "fresh")
    if spawn_type != "attach":
        inner = [spec.executable]
        if record.get("dangerous") and spec.dangerous_flag:
            inner.append(spec.dangerous_flag)
        inner += list(spec.flags.get(spawn_type, []))
        if model and spec.model_flag:
            inner += [spec.model_flag, model]
        if effort and spec.effort_flag:
            if not spec.valid_efforts or effort in spec.valid_efforts:
                inner += [spec.effort_flag, effort]
            else:
                log.warning(
                    "remote_agent_argv: effort %r not in %s's set — skipped",
                    effort,
                    spec.name,
                )
    wrapped = agent_harness.tmux_wrap(
        inner,
        server.tmux_socket,
        record["tmux_session"],
        # No conf_path: the VPS tmux server runs Vigilia's mobile-adapted
        # config (setup/11) — injecting the IDE's local conf would fight it.
        start_directory=record["remote_root"],
    )
    return remote_command_argv(server, wrapped, tty=True)


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
