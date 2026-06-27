"""IDE agent-events socket SERVER — the inbound edge of local agent capture.

The IDE owns a Unix-domain socket at
``$XDG_RUNTIME_DIR/symmetria-ide-agents-<pid>.sock`` and listens on it for events
from its OWN agents' reporters (`runtime/symmetria-ide-agent-hook.py`, injected
per-agent via ``claude --settings``). Each Claude lifecycle hook opens the socket,
writes one JSON line, and closes; this server parses the line and emits
``hook_received`` onto the GUI thread, where `AppController` runs it through
`agent_activity.AgentActivityMachine` and updates the slot's sparkles +
resumable session id. This replaces reaching OUT to the shell bridge and
subscribing back for the IDE's own children's data
(`docs/agent-ownership-inversion.md`).

This is the SERVER counterpart to `AgentBridgeClient` (a client of the shell
hub); it deliberately mirrors that module's thread discipline
(project-standards §1 P0 + §4 P2):

  - one daemon accept thread + an explicit ``threading.Event`` for cooperative
    shutdown (the listening socket carries a short timeout so the loop can poll
    the event);
  - each accepted connection is handled on its OWN short-lived daemon thread, so
    a slow/hung reporter never blocks other agents' events (and the same shape
    extends to the Phase-4 STT request/reply path);
  - the cross-thread ``hook_received`` emit suspends the cyclic GC (gotcha #10),
    via the shared `emit_gc_safe`;
  - consumers connect with explicit ``Qt.QueuedConnection`` (the connect site is
    in `AppController` with a grep-able comment).

The socket name encodes this IDE's pid, both so concurrent IDE instances (the
deliberate one-IDE-per-project topology) never collide and so a future
shell→IDE STT channel can address a specific IDE by the ``<ide_pid>`` half of an
agent id.
"""

from __future__ import annotations

import concurrent.futures
import glob
import json
import logging
import os
import socket
import threading
import uuid

from PySide6.QtCore import QObject, Signal

from .agent_bridge import emit_gc_safe

__all__ = ["AgentEventsServer", "default_socket_path", "reap_orphan_sockets"]

log = logging.getLogger(__name__)

# Socket filenames are <prefix><pid>.sock in $XDG_RUNTIME_DIR — the pid is the
# LEADING token so reap_orphan_sockets can clean a hard-killed IDE's leftover.
_SOCKET_PREFIX = "symmetria-ide-agents-"

# How long the listening socket blocks in accept() before re-checking the stop
# event. Short so stop() is responsive; not zero so the loop isn't a busy-spin.
_ACCEPT_TIMEOUT_SECONDS = 0.5

# Per-connection read timeout — a reporter writes one line and closes promptly,
# so a connection that stalls past this is abandoned (its own daemon thread, so
# it never blocks accept()).
_CONN_TIMEOUT_SECONDS = 1.0

# How long an stt_inject handler waits for the GUI thread (QML bracketed paste +
# the submit-Enter) to resolve before replying "timeout" to the dictation client.
# Generous vs _CONN_TIMEOUT_SECONDS because the paste→absorb→Enter round-trip is
# slower than a one-line read; the STT client's own socket timeout sits above it.
_INJECT_TIMEOUT_SECONDS = 5.0


def _runtime_dir() -> str:
    return os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"


def default_socket_path(pid: int | None = None) -> str:
    """The agent socket path for `pid` (this process by default).

    Assumes `$XDG_RUNTIME_DIR` is short — AF_UNIX paths cap at ~108 bytes, and the
    normal value (`/run/user/<uid>`) leaves ample room. An unusually long override
    could exceed the limit; `start()` already treats the resulting bind failure as
    non-fatal (logged, local capture absent), so the failure mode is graceful.
    """
    return os.path.join(
        _runtime_dir(),
        f"{_SOCKET_PREFIX}{pid if pid is not None else os.getpid()}.sock",
    )


def reap_orphan_sockets() -> None:
    """Remove agent-socket files left by dead IDE instances.

    The IDE unlinks its own socket on clean shutdown, but a SIGKILL/crash leaves
    it behind. The filename encodes the writing IDE's pid, so a socket whose pid
    is no longer alive is safe to remove. Best-effort; never touches our own or a
    live instance's. Mirrors `browser_mcp.reap_orphan_configs` /
    `app._reap_orphan_nvim_sockets`. (pid reuse by an unrelated process keeps a
    stale file — harmless: nothing connects to an IDE socket without being handed
    its exact path.)
    """
    pattern = os.path.join(_runtime_dir(), f"{_SOCKET_PREFIX}*.sock")
    for path in glob.glob(pattern):
        stem = os.path.basename(path)[len(_SOCKET_PREFIX) : -len(".sock")]
        try:
            pid = int(stem)
        except ValueError:
            continue  # not a pid-named socket — leave it alone
        if pid == os.getpid() or os.path.exists(f"/proc/{pid}"):
            continue
        try:
            os.unlink(path)
        except OSError:
            pass


class AgentEventsServer(QObject):
    """Listens for this IDE's agents' hook reports; emits them to the GUI thread.

    Thread layout:
      GUI thread     — `start`, `stop`, signal connection setup.
      accept worker  — binds/listens, accepts connections, spawns a handler
                       thread per connection.
      handler workers — read one connection's lines, parse, emit `hook_received`
                        (auto-queued to the GUI thread via QueuedConnection).
    """

    # One parsed reporter message: {"type": "hook", "agent_id": ..., ...}.
    # Wired to AppController._on_agent_hook via explicit Qt.QueuedConnection.
    hook_received = Signal(dict)

    # One parsed Claude status-line tap: {"type": "status_line", "agent_id": ...,
    # "model", "effort", "context_pct", "rate_limits", "observed_at_ns", ...}.
    # Emitted by the ~/.claude/status-line.sh tap (symmetria-statusline-tap.py) so
    # the IDE renders model / effort / context% / account usage natively instead
    # of relying on the in-terminal status line. Fire-and-forget; wired to
    # AppController._on_status_line via explicit Qt.QueuedConnection.
    status_line_received = Signal(dict)

    # STT recording-state toggle from the shell (inversion P4 — direct channel,
    # replacing the bridge's snapshot `stt` field): {"type": "stt_recording",
    # "buf": int, "transcribing": bool}. Fire-and-forget. Wired to
    # AppController._on_stt_recording via explicit Qt.QueuedConnection.
    stt_recording_received = Signal(dict)

    # STT injection request from the shell (replacing the bridge `inject` verb):
    # {"type": "stt_inject", "buf": int, "text": str, "submit": bool} — the
    # server stamps a "request_id" and forwards this; the connection handler
    # BLOCKS for the reply (see _handle_inject / resolve_inject). Wired to
    # AppController._on_stt_inject via explicit Qt.QueuedConnection.
    stt_inject_received = Signal(dict)

    def __init__(
        self, parent: QObject | None = None, *, socket_path: str | None = None
    ) -> None:
        super().__init__(parent)
        self._socket_path = socket_path or default_socket_path()
        self._server_sock: socket.socket | None = None
        self._stop_event = threading.Event()
        self._accept_thread: threading.Thread | None = None
        # request_id -> Future resolved by resolve_inject (GUI thread, from
        # AppController.agent_inject_done) and awaited by the connection handler
        # thread so it can write the reply on the same socket. Guarded by the lock
        # (handler threads register/pop; the GUI thread resolves).
        self._pending_injects: dict[str, concurrent.futures.Future] = {}
        self._pending_lock = threading.Lock()

    @property
    def socket_path(self) -> str:
        """The path the reporter connects to (exported as SYMMETRIA_IDE_AGENT_SOCK)."""
        return self._socket_path

    # ------------------------------------------------------------------
    # Lifecycle (GUI thread)
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Bind + listen, then spawn the accept worker. Idempotent.

        Failure is non-fatal: a bind error logs and leaves the server inert
        (socket_path still returns the path, but nothing listens — agents whose
        reporters connect will fail their fast connect and no-op). The IDE keeps
        running; local capture is simply absent until the next launch.
        """
        if self._accept_thread is not None and self._accept_thread.is_alive():
            log.warning("AgentEventsServer.start: already running")
            return
        # Defensive: if a prior accept thread died abnormally while _server_sock
        # was still set, close it before rebinding so we don't leak its fd (and
        # the old, now-unlinked socket).
        if self._server_sock is not None:
            try:
                self._server_sock.close()
            except OSError:
                pass
            self._server_sock = None
        reap_orphan_sockets()
        # Unlink any stale file at our exact path first (a prior process with this
        # pid that was hard-killed) — bind() on an existing AF_UNIX path fails
        # with EADDRINUSE otherwise.
        try:
            os.unlink(self._socket_path)
        except FileNotFoundError:
            pass
        except OSError:
            log.exception("AgentEventsServer: could not clear stale socket")
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(self._socket_path)
            sock.listen(16)
            sock.settimeout(_ACCEPT_TIMEOUT_SECONDS)
        except OSError:
            log.exception("AgentEventsServer: failed to bind %s", self._socket_path)
            return
        self._server_sock = sock
        self._stop_event.clear()
        self._accept_thread = threading.Thread(
            target=self._run_accept_loop, name="agent-events-accept", daemon=True
        )
        self._accept_thread.start()
        log.info("agent-events server listening (%s)", self._socket_path)

    def stop(self) -> None:
        """Stop accepting, join the worker, unlink the socket. Idempotent.

        In-flight connection handler threads are daemon + short-lived (capped by
        the per-connection timeout), so they are not joined — they finish on
        their own or die with the process.
        """
        self._stop_event.set()
        # Release any handler thread blocked on an inject reply so it doesn't hang
        # past shutdown (it will write the error reply and close).
        with self._pending_lock:
            pending = list(self._pending_injects.values())
            self._pending_injects.clear()
        for future in pending:
            if not future.done():
                future.set_result(
                    {"ok": False, "submitted": False, "error": "shutting-down"}
                )
        if self._server_sock is not None:
            try:
                self._server_sock.close()
            except OSError:
                pass
            self._server_sock = None
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=2.0)
            self._accept_thread = None
        try:
            os.unlink(self._socket_path)
        except FileNotFoundError:
            pass
        except OSError:
            log.debug("agent-events: socket already gone at stop")
        log.info("agent-events server stopped")

    # ------------------------------------------------------------------
    # Accept worker
    # ------------------------------------------------------------------

    def _run_accept_loop(self) -> None:
        sock = self._server_sock
        if sock is None:
            return
        while not self._stop_event.is_set():
            try:
                conn, _ = sock.accept()
            except socket.timeout:
                continue  # poll the stop event
            except OSError:
                # Socket closed under us (stop) or a transient accept error.
                if self._stop_event.is_set():
                    return
                continue
            # Handle each connection on its own short-lived daemon thread so a
            # slow reporter can't stall the next agent's events.
            threading.Thread(
                target=self._handle_connection,
                args=(conn,),
                name="agent-events-conn",
                daemon=True,
            ).start()

    def _handle_connection(self, conn: socket.socket) -> None:
        """Dispatch one connection's JSON messages by `type`.

        Message kinds sharing the socket:
          - `hook` (and anything unknown) — a fire-and-forget activity report;
            emitted to the GUI thread, no reply.
          - `status_line` — a fire-and-forget Claude status-line tap (model /
            effort / context% / account usage); emitted, no reply. MUST be
            dispatched explicitly: it falls into the `hook` branch otherwise and
            the activity machine would choke on a non-hook payload.
          - `stt_recording` — a fire-and-forget recording-state toggle; emitted,
            no reply.
          - `stt_inject` — a REQUEST/REPLY: the handler blocks until the GUI
            thread performs the PTY paste and resolves the result, then writes a
            `stt_inject_result` line back on this same connection.
        """
        try:
            # Injects can take up to _INJECT_TIMEOUT_SECONDS (paste→absorb→Enter);
            # widen the socket timeout for this connection so the blocking reply
            # write isn't cut off by the shorter read timeout.
            conn.settimeout(_INJECT_TIMEOUT_SECONDS + 1.0)
            # `with` so the makefile wrapper closes deterministically: socket.makefile
            # bumps the socket's io-ref count, so the finally's conn.close() alone
            # would defer the fd release to GC of `reader` — exactly the worker-thread
            # GC timing the project avoids (gotcha #10). Releasing it here also keeps
            # fds from transiently piling up under a burst of short connections.
            with conn.makefile("r", encoding="utf-8", errors="replace") as reader:
                for line in reader:
                    text = line.strip()
                    if not text:
                        continue
                    try:
                        payload = json.loads(text)
                    except json.JSONDecodeError:
                        log.warning("agent-events: bad JSON (%.100s)", text)
                        continue
                    if not isinstance(payload, dict):
                        continue
                    msg_type = payload.get("type")
                    # GC suspended around each cross-thread emit (gotcha #10 — a
                    # worker collection racing QSGRenderThread mid-paint SEGVs).
                    if msg_type == "stt_inject":
                        self._handle_inject(conn, payload)
                    elif msg_type == "stt_recording":
                        emit_gc_safe(self.stt_recording_received, payload)
                    elif msg_type == "status_line":
                        emit_gc_safe(self.status_line_received, payload)
                    else:
                        emit_gc_safe(self.hook_received, payload)
        except OSError:
            pass  # client vanished / timed out — drop the connection
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _handle_inject(self, conn: socket.socket, payload: dict) -> None:
        """Forward an stt_inject to the GUI thread and block for the reply.

        Stamps a server-side `request_id`, registers a Future the GUI thread
        resolves via `resolve_inject` (from `AppController.agent_inject_done`
        after the QML paste completes), waits for it, and writes a
        `stt_inject_result` line back on `conn`. A timeout or shutdown resolves
        the Future with an error so the dictation client never hangs.
        """
        request_id = uuid.uuid4().hex
        future: concurrent.futures.Future = concurrent.futures.Future()
        with self._pending_lock:
            self._pending_injects[request_id] = future
        emit_gc_safe(self.stt_inject_received, {**payload, "request_id": request_id})
        try:
            result = future.result(timeout=_INJECT_TIMEOUT_SECONDS)
        except concurrent.futures.TimeoutError:
            result = {"ok": False, "submitted": False, "error": "timeout"}
        except Exception as exc:  # never leave the client without a reply
            result = {"ok": False, "submitted": False, "error": str(exc)}
        finally:
            with self._pending_lock:
                self._pending_injects.pop(request_id, None)
        try:
            conn.sendall(
                (json.dumps({"type": "stt_inject_result", **result}) + "\n").encode()
            )
        except OSError:
            pass  # client gave up waiting — nothing more to do

    def resolve_inject(
        self, request_id: str, ok: bool, submitted: bool, error: str = ""
    ) -> bool:
        """Resolve a pending stt_inject (GUI thread, from agent_inject_done).

        Returns True if `request_id` matched a pending inject — advisory now (all
        callers ignore it; it remains a tested contract documenting whether the id
        was live). Thread-safe: `set_result` is, and the handler thread awaiting
        the Future picks the result up.
        """
        with self._pending_lock:
            future = self._pending_injects.get(request_id)
        if future is not None and not future.done():
            future.set_result({"ok": ok, "submitted": submitted, "error": error})
            return True
        return False
