"""Bridge client: publish IDE agents to Symmetria Shell + subscribe to state.

Connects to the shell's `agent-bridge.py` hub over its Unix socket
(`/run/user/$UID/symmetria-agents.sock`, overridable via
`SYMMETRIA_AGENT_SOCKET`) and plays BOTH protocol roles on a single
connection:

- **Publisher** — the same JSON-line verbs orchestrator.nvim's
  `bridge.lua` speaks (`hello` / `sync` / `added` / `removed` / `focus` /
  `updated` / `goodbye`), registering this IDE's terminal-hosted agents
  with `nvim_pid = os.getpid()`. The bridge resolves the Hyprland window
  for click-to-focus through the `SYMMETRIA_HOST_WINDOW_PID` environ
  fallback (`app.py::_export_host_window_pid`), and Claude-hook activity
  arrives keyed by ``f"{os.getpid()}_{slot}"`` because the spawn argv
  exports ``SYMMETRIA_AGENT_ID`` with exactly that id (the bridge keys
  agents as ``f"{nvim_pid}_{buf}"`` and we publish ``buf = slot``).
- **Subscriber** — the ``{"type": "subscribe"}`` verb: the bridge then
  writes every consolidated snapshot (``{"agents": [...], "projects":
  [...]}``) back over this same connection, including an immediate one on
  subscribe. `AppController` filters the feed to this IDE's own agents and
  drives the top-bar sparkle animation from `activity_state` — hook-driven
  activity needs zero extra plumbing.

**Reconnect.** The reader thread owns the connect loop: retry with capped
backoff while the bridge is absent, and on every (re)connect replay
``hello`` → ``sync`` (all currently-registered instances) → ``subscribe``.
The bridge restarting (shell reload) therefore self-heals exactly like
orchestrator.nvim's inotify watcher, just poll-based — acceptable because
the backoff caps at a few seconds and bridge restarts are rare.

**Thread discipline (project-standards §1 P0 + §4 P0).**

- One daemon reader thread + explicit `threading.Event` for cooperative
  shutdown — the `NvimBackend` / `SessionHost` pattern.
- GC is suspended around the cross-thread `snapshot_received` emission
  (gotcha #10 — a worker-thread collection racing `QSGRenderThread`
  mid-paint SEGVs).
- Consumers connect with explicit `Qt.QueuedConnection` (the connect
  sites live in `AppController` with grep-able comments).
- All writes go through `_send` under `_sock_lock`; the GUI thread and
  the reader thread (hello/sync/subscribe replay) share the write path.
- The instance registry `_instances` is mutated on the GUI thread and
  read by the reader thread during reconnect-sync — guarded by
  `_instances_lock`.
"""

from __future__ import annotations

import gc
import json
import logging
import os
import socket
import threading

from PySide6.QtCore import QObject, QTimer, Signal, SignalInstance

__all__ = ["AgentBridgeClient"]

log = logging.getLogger(__name__)

# Reconnect backoff: start fast (bridge restarts take ~1s), cap low enough
# that a freshly-started shell picks the IDE up within a few seconds.
_BACKOFF_INITIAL_SECONDS = 0.5
_BACKOFF_MAX_SECONDS = 5.0

# Title updates ride a debounce so OSC-title churn (claude redraws its
# terminal title constantly while streaming) doesn't flood the socket.
# Mirrors orchestrator.nvim's TITLE_DEBOUNCE_MS (bridge.lua).
_TITLE_DEBOUNCE_MS = 200


def _default_socket_path() -> str:
    return os.environ.get("SYMMETRIA_AGENT_SOCKET") or (
        f"/run/user/{os.getuid()}/symmetria-agents.sock"
    )


class AgentBridgeClient(QObject):
    """Publishes this IDE's agents to the shell bridge and consumes snapshots.

    Thread layout:
      GUI thread   — `start`, `stop`, the `notify_*` publish API, signal
                     connection setup.
      reader worker — connect loop + blocking line reads; parses snapshot
                     JSON; emits `snapshot_received(dict)` (auto-queued to
                     the GUI thread).
    """

    # Full consolidated bridge snapshot: {"agents": [...], "projects": [...]}.
    # Wired to AppController._on_bridge_snapshot via explicit Qt.QueuedConnection.
    snapshot_received = Signal(dict)

    # Bridge-routed inject command for one of THIS IDE's agents:
    # {"type": "inject", "request_id": str, "buf": int, "text": str,
    #  "submit": bool}. The hub routes these from an STT requester to the
    # publisher connection that owns the target agent (us). Wired to
    # AppController._on_bridge_inject via explicit Qt.QueuedConnection;
    # the reply goes back through send_inject_result.
    inject_requested = Signal(dict)

    # True on (re)connect after the hello/sync/subscribe replay, False on
    # connection loss. Informational — publishes while disconnected are
    # dropped (the reconnect sync restores bridge-side state).
    connection_changed = Signal(bool)

    def __init__(
        self, parent: QObject | None = None, *, socket_path: str | None = None
    ):
        super().__init__(parent)
        self._socket_path = socket_path or _default_socket_path()
        self._pid = os.getpid()

        self._sock: socket.socket | None = None
        self._sock_lock = threading.Lock()

        # slot -> instance payload (the bridge's per-instance shape, with
        # "buf" == slot). Source of truth for reconnect sync.
        self._instances: dict[int, dict] = {}
        self._instances_lock = threading.Lock()

        self._stop_event = threading.Event()
        self._reader_thread: threading.Thread | None = None

        # Pending debounced title updates: slot -> title. Flushed by a
        # single-shot GUI-thread timer (no cross-thread QTimer access).
        self._pending_titles: dict[int, str] = {}
        self._title_timer = QTimer(self)
        self._title_timer.setSingleShot(True)
        self._title_timer.setInterval(_TITLE_DEBOUNCE_MS)
        self._title_timer.timeout.connect(self._flush_titles)

    # ------------------------------------------------------------------
    # Lifecycle (GUI thread)
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spawn the reader/connect worker. Idempotent."""
        if self._reader_thread is not None and self._reader_thread.is_alive():
            log.warning("AgentBridgeClient.start: already running")
            return
        self._stop_event.clear()
        self._reader_thread = threading.Thread(
            target=self._run_reader_loop,
            name="agent-bridge-reader",
            daemon=True,
        )
        self._reader_thread.start()
        log.info("agent bridge client started (socket=%s)", self._socket_path)

    def stop(self) -> None:
        """Send goodbye, close the socket, join the worker. Idempotent."""
        self._stop_event.set()
        self._send({"type": "goodbye", "nvim_pid": self._pid})
        with self._sock_lock:
            if self._sock is not None:
                try:
                    self._sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=2.0)
            self._reader_thread = None
        log.info("agent bridge client stopped")

    # ------------------------------------------------------------------
    # Publish API (GUI thread)
    # ------------------------------------------------------------------

    def notify_spawn(self, instance: dict) -> None:
        """Register an instance (keyed by its "buf" field) and publish `added`."""
        slot = int(instance["buf"])
        with self._instances_lock:
            self._instances[slot] = dict(instance)
        self._send({"type": "added", "nvim_pid": self._pid, "instance": instance})

    def notify_remove(self, slot: int) -> None:
        with self._instances_lock:
            self._instances.pop(slot, None)
        self._pending_titles.pop(slot, None)
        self._send({"type": "removed", "nvim_pid": self._pid, "buf": slot})

    def notify_focus(self, slot: int) -> None:
        """Mark `slot` active (and every sibling inactive) + publish `focus`."""
        with self._instances_lock:
            for s, inst in self._instances.items():
                inst["active"] = s == slot
        self._send({"type": "focus", "nvim_pid": self._pid, "buf": slot})

    def notify_title(self, slot: int, title: str) -> None:
        """Debounced title update (200ms, matching orchestrator.nvim)."""
        with self._instances_lock:
            if slot not in self._instances:
                return
            self._instances[slot]["title"] = title
        self._pending_titles[slot] = title
        self._title_timer.start()  # restart the debounce window

    def _flush_titles(self) -> None:
        pending, self._pending_titles = self._pending_titles, {}
        for slot, title in pending.items():
            self._send(
                {"type": "updated", "nvim_pid": self._pid, "buf": slot, "title": title}
            )

    def send_inject_result(
        self, request_id: str, ok: bool, submitted: bool, error: str = ""
    ) -> None:
        """Answer a bridge-routed inject command (GUI thread).

        The hub holds the requester's connection open until this arrives
        (or its own timeout fires), so send promptly on every code path —
        a swallowed failure leaves the requester waiting out the timeout.
        """
        self._send(
            {
                "type": "inject_result",
                "nvim_pid": self._pid,
                "request_id": request_id,
                "ok": ok,
                "submitted": submitted,
                "error": error,
            }
        )

    # ------------------------------------------------------------------
    # Wire helpers (any thread; serialised by _sock_lock)
    # ------------------------------------------------------------------

    def _send(self, msg: dict) -> bool:
        """Write one JSON line. Returns False (and drops) when disconnected."""
        with self._sock_lock:
            if self._sock is None:
                return False
            try:
                self._sock.sendall((json.dumps(msg) + "\n").encode())
                return True
            except OSError as exc:
                log.debug("bridge send failed (%s) — dropping until reconnect", exc)
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None
                return False

    def _replay_registration(self) -> None:
        """hello → sync → subscribe, sent on every (re)connect."""
        with self._instances_lock:
            instances = [dict(inst) for inst in self._instances.values()]
        # host_window_pid declares this process as the Hyprland window
        # owning the agents — authoritative over the bridge's /proc-walk
        # heuristic, which misresolves when the IDE is launched from a dev
        # terminal (the walk finds that terminal first) and cannot use the
        # environ fallback for our OWN pid (putenv after exec is invisible
        # in /proc/pid/environ).
        self._send(
            {
                "type": "hello",
                "nvim_pid": self._pid,
                "nvim_socket": "",
                "host_window_pid": self._pid,
            }
        )
        self._send({"type": "sync", "nvim_pid": self._pid, "instances": instances})
        self._send({"type": "subscribe"})

    # ------------------------------------------------------------------
    # Reader worker
    # ------------------------------------------------------------------

    def _run_reader_loop(self) -> None:
        backoff = _BACKOFF_INITIAL_SECONDS
        while not self._stop_event.is_set():
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(self._socket_path)
            except OSError:
                sock.close()
                # Bridge absent (shell down / restarting) — capped backoff.
                if self._stop_event.wait(backoff):
                    return
                backoff = min(backoff * 2, _BACKOFF_MAX_SECONDS)
                continue

            backoff = _BACKOFF_INITIAL_SECONDS
            with self._sock_lock:
                self._sock = sock
            self._replay_registration()
            self._emit_gc_safe(self.connection_changed, True)
            log.info("agent bridge connected (%s)", self._socket_path)

            try:
                reader = sock.makefile("r", encoding="utf-8", errors="replace")
                for line in reader:
                    if self._stop_event.is_set():
                        break
                    text = line.strip()
                    if not text:
                        continue
                    try:
                        payload = json.loads(text)
                    except json.JSONDecodeError:
                        log.warning("bridge snapshot: bad JSON (%.100s)", text)
                        continue
                    # The subscription feed is untyped snapshot lines;
                    # commands the hub routes to us carry a "type" field.
                    if payload.get("type") == "inject":
                        self._emit_gc_safe(self.inject_requested, payload)
                    else:
                        self._emit_gc_safe(self.snapshot_received, payload)
            except OSError:
                pass  # connection dropped — fall through to reconnect
            finally:
                with self._sock_lock:
                    if self._sock is sock:
                        self._sock = None
                try:
                    sock.close()
                except OSError:
                    pass

            if self._stop_event.is_set():
                return
            self._emit_gc_safe(self.connection_changed, False)
            log.info("agent bridge disconnected — retrying")

    @staticmethod
    def _emit_gc_safe(signal: SignalInstance, payload) -> None:
        """Emit a cross-thread signal with the cyclic GC suspended.

        Gotcha #10: a Python 3.14 collection triggered from this worker
        thread while QSGRenderThread is mid-paint SEGVs. The emit allocates
        (queued-connection payload marshalling), so the collector is
        suspended for its duration — same mitigation as SessionHost.
        """
        gc_was_enabled = gc.isenabled()
        if gc_was_enabled:
            gc.disable()
        try:
            signal.emit(payload)
        finally:
            if gc_was_enabled:
                gc.enable()
