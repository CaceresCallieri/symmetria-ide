"""Agent session host: spawn the Node SDK sidecar, pump events.

Mirrors the structure of `nvim_backend.py` post-`nvim_events`
extraction. A daemon worker thread reads the subprocess's stdout
line-by-line, parses each JSONL event, and emits
`event_received(dict)` onto the GUI thread through Qt's auto-queued
cross-thread delivery. A second daemon reads stderr and emits
`stderr_line(str)` for diagnostic surfaces (authentication failures,
SDK errors, sidecar-internal logging, etc.).

**Shape.** `start(prompt, cwd)` spawns `node sidecar/dist/index.js`
with stdin kept open, then writes the initial prompt as the first
JSONL `user_message` command via `send_user_message`. The sidecar
runs `@anthropic-ai/claude-agent-sdk`'s `query()` programmatically
and translates SDK messages back to JSONL events on stdout that
`SessionModel._row_from_*` already consumes. stdin is closed only on
`stop()` (user pressed Escape + action="new", or app shutdown),
which triggers the sidecar's clean drain + exit-0.

**Wire protocol.** Mirrored from `sidecar/src/protocol.ts` — keep
both in sync. Inbound (Python → sidecar): one JSON object per line:

    {"type": "user_message", "content": "..."}
    {"type": "permission_response", "request_id": "<uuid>",
     "behavior": "allow" | "deny", "message"?: "..."}

Outbound (sidecar → Python): SDK messages translated to JSONL
events whose top-level `type` and inner fields match what
`SessionModel.apply` already routes (`assistant`, `system`,
`stream_event`, `result`, `rate_limit_event`), plus a sidecar-
synthesized `permission_request` envelope when the SDK's
`canUseTool` callback fires (Step 5).

**Thread discipline (project-standards §1 P0 + §4 P0).**

- Daemon workers + explicit `threading.Event` for cooperative shutdown
  (Standards §1 P0). `NvimBackend` adopted this exact pattern after
  the `nvim_events` refactor; `SessionHost` follows suit.
- GC is suspended around signal emission in `_run_stdout_loop`
  (gotcha #10). Any Python 3.14 allocation on this worker thread can
  race with `QSGRenderThread` during `NvimView.paint()` if it trips
  the cyclic collector; disabling GC inside the tight hot iteration
  closes that window without affecting long-lived state.
- `subprocess.stdin.write` is serialised behind `_stdin_lock` —
  CPython's GIL effectively serialises same-handle writes, but the
  lock documents the contract explicitly and protects against the
  eventual move to a free-threaded build.
- Cross-thread signal payloads are trivially serialisable: `dict` for
  the event, `str` for stderr (Standards §4 P1).
"""

from __future__ import annotations

import gc
import json
import logging
import shlex
import subprocess
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot

log = logging.getLogger(__name__)


def _sidecar_dist_path() -> Path:
    """Resolve the path to the bundled sidecar entry point.

    Repo layout: `sidecar/dist/index.js` sits at the repo root, two
    levels up from this module (`src/symmetria_ide/session_host.py`).
    The bundle is gitignored — `npm run build` regenerates it. If the
    file is missing, `start()` surfaces a clear error pointing the
    user at the install command instead of a cryptic spawn failure.
    """
    return Path(__file__).resolve().parents[2] / "sidecar" / "dist" / "index.js"


# Argv used on every `start()` call. We invoke `node` directly with the
# bundled sidecar entry point — the sidecar's package.json declares
# `"type": "module"` so node resolves the bundle as ESM, and `esbuild`
# externalised `@anthropic-ai/claude-agent-sdk` so the SDK loads from
# `sidecar/node_modules/` at runtime rather than being inlined into the
# bundle (which would be ~2MB and would break native binary opt-deps).
def _sidecar_argv() -> tuple[str, ...]:
    """Return the argv tuple for spawning the sidecar.

    Computed lazily so test environments can stub `_sidecar_dist_path`
    to point at a fixture without import-time path coupling.
    """
    return ("node", str(_sidecar_dist_path()))


# Timeout (seconds) for cooperative SIGTERM shutdown before we SIGKILL.
# Matches NvimBackend's `proc.wait(timeout=…)` budget — if the sidecar
# hasn't exited within this window the process is unresponsive and we
# force termination so the GUI shutdown path doesn't block on it.
_SHUTDOWN_GRACE_SECONDS = 2.0


def parse_stream_json_line(line: str) -> dict | None:
    """Parse one stdout line into a wire-protocol event dict.

    Returns ``None`` for blank / whitespace-only lines and for lines
    that aren't valid JSON — the worker loop logs and continues in
    both cases rather than crashing. Extracted as a free function so
    tests exercise it without spawning a subprocess.

    BOM-tolerance: the sidecar never prepends a BOM in practice, but
    we strip one defensively so a future change doesn't silently
    wedge the parser. Same defence carried over from the prior
    `claude -p` implementation.
    """
    stripped = line.strip().lstrip("\ufeff")
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        log.exception("malformed stream-json line: %r", line[:200])
        return None
    if not isinstance(parsed, dict):
        log.warning("stream-json line is not an object: %r", parsed)
        return None
    return parsed


class SessionHost(QObject):
    """Owns the Node SDK sidecar subprocess and pumps its JSONL output.

    Thread layout:
      GUI thread   — `start`, `stop`, `send_user_message`,
                      `send_permission_response`, signal connection
                      setup, property reads.
      stdout worker — blocks on `proc.stdout.readline`; parses JSONL;
                      emits `event_received(dict)`.
      stderr worker — blocks on `proc.stderr.readline`; emits
                      `stderr_line(str)` for diagnostics.
      render thread — not touched here; belongs to Qt's scene graph.

    Signals are all auto-queued to the GUI thread because the
    receivers (SessionModel, AppController) live there. Do not add
    direct connections to this QObject from other worker threads.
    """

    # Payload is the full parsed JSONL event dict. Wired to
    # `SessionModel.apply` via an explicit `Qt.QueuedConnection` in
    # `AppController.__init__` (Step 6).
    event_received = Signal(dict)

    # Emitted once when the subprocess has exited AND both worker
    # threads have joined. Wired to `SessionModel.on_host_closed`.
    closed = Signal()

    # One stderr line at a time — the sidecar prints lifecycle
    # diagnostics and SDK auth failures here. Surface them to the UI
    # as a dim info row (future) or via the app log (placeholder).
    stderr_line = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._proc: subprocess.Popen[str] | None = None
        self._stdout_worker: threading.Thread | None = None
        self._stderr_worker: threading.Thread | None = None
        # Cooperative-shutdown signal. Set by `stop()` before any
        # close/terminate call, and set unconditionally in each
        # worker's `finally` so both cooperative and crash paths are
        # observable. See NvimBackend's `_stop_event` for the
        # established template.
        self._stop_event = threading.Event()
        # Serialises stdin write+flush. CPython's GIL already serialises
        # same-handle writes, but explicit locking documents the
        # contract and protects against the eventual move off
        # GIL-serialised builds. Standards §1 P1 leans this way.
        self._stdin_lock = threading.Lock()

    # --- Public GUI-thread API ----------------------------------------

    @property
    def stop_event(self) -> threading.Event:
        """Observable shutdown signal — set as soon as teardown begins.

        Exposed for tests and any future coordinator that needs to
        wait on lifecycle without polling `thread.is_alive()`. Same
        contract as `NvimBackend.stop_event`.
        """
        return self._stop_event

    @property
    def is_running(self) -> bool:
        """True while the subprocess is spawned and hasn't signalled stop."""
        return self._proc is not None and not self._stop_event.is_set()

    @Slot(str)
    def start(self, prompt: str, cwd: Path | None = None) -> None:
        """Spawn the Node sidecar; deliver `prompt` as first user message.

        The sidecar keeps stdin open across turns — we write the
        initial prompt as the first `user_message` command via
        `send_user_message`, then subsequent composer submits become
        additional JSONL lines on the same stdin stream. The sidecar
        exits cleanly only when stdin closes (via `stop()`) or on
        SDK error.

        No-op if called while a subprocess is already spawned.
        """
        if self._proc is not None:
            log.warning("SessionHost.start called while already running — ignored")
            return
        if not prompt:
            log.warning("SessionHost.start with empty prompt — not spawning")
            return
        # Build artifact must exist before we attempt to spawn. The
        # sidecar's `dist/index.js` is gitignored — `npm run build`
        # regenerates it. Bail with a clear log message instead of
        # surfacing a confusing `node: cannot find module` error from
        # a child process.
        dist_path = _sidecar_dist_path()
        if not dist_path.exists():
            log.error(
                "sidecar bundle not found at %s — run `cd sidecar && npm install && npm run build`",
                dist_path,
            )
            self._stop_event.set()
            self.closed.emit()
            return
        # Reset the stop signal so `is_running` and `_run_stdout_loop`
        # see a clean slate — a prior `stop()` call sets this and it must
        # be cleared before starting workers or they will exit immediately
        # after the first event (the event-loop break at line ~352).
        self._stop_event.clear()
        argv = list(_sidecar_argv())
        log.info("spawning sidecar: %s", " ".join(shlex.quote(a) for a in argv))
        try:
            self._proc = subprocess.Popen(  # noqa: S603 — argv is list, not shell
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=1,  # line-buffered text mode
                text=True,
                encoding="utf-8",
                cwd=str(cwd) if cwd is not None else None,
            )
        except FileNotFoundError:
            log.exception(
                "failed to spawn sidecar — is `node` installed and on PATH? (need >=20)"
            )
            self._stop_event.set()
            self.closed.emit()
            return
        except Exception:
            log.exception("failed to spawn sidecar")
            self._stop_event.set()
            self.closed.emit()
            return
        self._stdout_worker = threading.Thread(
            target=self._run_stdout_loop,
            name="session-host-stdout",
            daemon=True,
        )
        self._stderr_worker = threading.Thread(
            target=self._run_stderr_loop,
            name="session-host-stderr",
            daemon=True,
        )
        self._stdout_worker.start()
        self._stderr_worker.start()
        # Deliver the initial user turn on the now-alive stdin stream.
        # Factoring the write through `send_user_message` means there's
        # one JSON envelope shape + one `_stdin_lock`-serialised write
        # path — this function doesn't duplicate them.
        self.send_user_message(prompt)

    @Slot()
    def stop(self) -> None:
        """Tear down cooperatively: set stop, terminate, join workers.

        Shutdown order (project-standards §4 P0):
          1. Set `_stop_event` so observers see shutdown-in-progress.
          2. `terminate()` the subprocess — SIGTERM on POSIX.
          3. `wait(timeout=...)`; fall through to `kill()` if the
             subprocess doesn't exit within grace.
          4. Close stdin so any blocked `send_user_message` unblocks.
          5. Join both workers with a short timeout; we don't block
             GUI shutdown indefinitely on a misbehaving subprocess.

        Safe to call multiple times and safe to call before `start`.
        """
        self._stop_event.set()
        proc = self._proc
        if proc is not None:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                log.debug("proc.terminate failed", exc_info=True)
            try:
                proc.wait(timeout=_SHUTDOWN_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                log.warning("sidecar did not exit on SIGTERM — sending SIGKILL")
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    log.debug("proc.kill failed", exc_info=True)
                try:
                    proc.wait(timeout=_SHUTDOWN_GRACE_SECONDS)
                except Exception:  # noqa: BLE001
                    log.debug("proc.wait after kill failed", exc_info=True)
            except Exception:  # noqa: BLE001
                log.debug("proc.wait failed", exc_info=True)
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
            except Exception:  # noqa: BLE001
                log.debug("proc.stdin.close failed", exc_info=True)
        for worker in (self._stdout_worker, self._stderr_worker):
            if worker is not None:
                worker.join(timeout=1.0)
        self._proc = None
        self._stdout_worker = None
        self._stderr_worker = None

    @Slot(str)
    def send_user_message(self, text: str) -> None:
        """Write a JSONL `user_message` command to the sidecar's stdin.

        Envelope shape (defined in `sidecar/src/protocol.ts` —
        keep this in sync with the `InboundCommand` union):

            {"type": "user_message", "content": "<text>"}

        The sidecar wraps this in an `SDKUserMessage` and pushes it
        onto the SDK's prompt async iterable, which makes the next
        turn proceed.

        Serialisation: write + flush is wrapped in `_stdin_lock` so
        interleaved writes from concurrent callers can't corrupt the
        line framing. GIL already serialises same-handle writes in
        practice; the lock is explicit documentation and future-
        proofing for free-threaded builds.

        Silently returns if there's no running subprocess — callers
        should not need to branch on `is_running`.
        """
        self._write_command({"type": "user_message", "content": text})

    @Slot(str, str)
    def send_permission_response(self, request_id: str, behavior: str) -> None:
        """Write a JSONL `permission_response` command to the sidecar's stdin.

        Envelope shape (mirrors `InboundCommand` in
        `sidecar/src/protocol.ts`):

            {"type": "permission_response",
             "request_id": "<uuid>",
             "behavior": "allow" | "deny"}

        The sidecar matches `request_id` against its pending
        `canUseTool` promise map and resolves with the corresponding
        `PermissionResult`, unblocking whichever tool call was
        awaiting approval.

        Validates `behavior` and silently drops invalid values rather
        than raising — keeps the QML invocation surface tolerant of
        accidental coercion. Same `_stdin_lock` discipline as
        `send_user_message`.
        """
        if behavior not in ("allow", "deny"):
            log.warning(
                "send_permission_response: invalid behavior %r — dropped", behavior
            )
            return
        self._write_command(
            {
                "type": "permission_response",
                "request_id": request_id,
                "behavior": behavior,
            }
        )

    def _write_command(self, payload: dict) -> None:
        """Serialise + flush a JSONL command on stdin under the lock.

        Single source of truth for the wire-write path — both
        `send_user_message` and `send_permission_response` go through
        here. Silently returns if the subprocess is gone; surfaces
        broken-pipe via `log.exception` rather than re-raising so a
        racing-shutdown caller doesn't crash the GUI thread.
        """
        proc = self._proc
        if proc is None or proc.stdin is None:
            log.debug("_write_command with no running subprocess — dropped")
            return
        stdin = proc.stdin  # local alias keeps pyright's Optional narrowing
        line = json.dumps(payload) + "\n"
        with self._stdin_lock:
            try:
                stdin.write(line)
                stdin.flush()
            except (BrokenPipeError, OSError):
                log.exception("sidecar stdin write failed — subprocess gone?")

    # --- Worker threads -----------------------------------------------

    def _run_stdout_loop(self) -> None:
        """Iterate stdout line-by-line, parse JSONL, emit events.

        GC is suspended for the whole loop body so the Python 3.14
        incremental collector can't race with `QSGRenderThread`
        painting (gotcha #10). The allocation surface on this thread
        includes `line.strip()`, `json.loads` (which builds a fresh
        dict graph for every event), and the queued-connection copy
        Qt makes when we `emit`. Any of those is enough to trip
        `gc.threshold` under sustained streaming.
        """
        proc = self._proc
        if proc is None or proc.stdout is None:
            self._stop_event.set()
            self.closed.emit()
            return
        try:
            gc_was_enabled = gc.isenabled()
            if gc_was_enabled:
                gc.disable()
            try:
                while True:
                    line = proc.stdout.readline()
                    if not line:
                        break  # EOF — subprocess closed stdout
                    event = parse_stream_json_line(line)
                    if event is None:
                        continue
                    self.event_received.emit(event)
                    if self._stop_event.is_set():
                        break
            finally:
                if gc_was_enabled:
                    gc.enable()
        except Exception:  # noqa: BLE001
            if not self._stop_event.is_set():
                log.exception("sidecar stdout loop crashed")
        finally:
            # Set unconditionally — covers both cooperative stop and
            # crash-exit paths. Anyone blocking on `stop_event.wait()`
            # is unblocked in either case.
            self._stop_event.set()
            self.closed.emit()

    def _run_stderr_loop(self) -> None:
        """Iterate stderr line-by-line, emit stderr_line for each.

        No GC discipline here — stderr volume is low and we don't
        allocate inside the hot path beyond `line.rstrip` and the
        emit. Breaking the mirror with `_run_stdout_loop` is
        deliberate: GC-suspending a thread that mostly blocks on I/O
        would grow the heap without bound.
        """
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for line in proc.stderr:
                if self._stop_event.is_set():
                    break
                self.stderr_line.emit(line.rstrip())
        except Exception:  # noqa: BLE001
            if not self._stop_event.is_set():
                log.exception("sidecar stderr loop crashed")
