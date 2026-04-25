"""Agent session host: spawn `claude -p --output-format stream-json`, pump events.

Mirrors the structure of `nvim_backend.py` post-`nvim_events`
extraction. A daemon worker thread reads the subprocess's stdout
line-by-line, parses each JSONL event, and emits
`event_received(dict)` onto the GUI thread through Qt's auto-queued
cross-thread delivery. A second daemon reads stderr and emits
`stderr_line(str)` for diagnostic surfaces (authentication failures,
CLI warnings, etc.).

**Shape.** `start(prompt, cwd)` spawns `claude -p --input-format
stream-json --output-format stream-json` with stdin kept open, then
writes the initial prompt as the first JSONL user envelope via
`send_user_message`. The subprocess stays alive across turns —
every subsequent composer submit is another `send_user_message` call
writing a new JSONL line. stdin is closed only on `stop()` (user
pressed Escape + action="new", or app shutdown), which triggers a
clean exit-0 and one final `result` event.

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


# Argv tail used on every `start()` call. `-p` is required to enable
# non-interactive streaming output; `--output-format stream-json`
# produces one JSON event per stdout line; `--input-format stream-json`
# reads JSONL user envelopes on stdin (one per turn) so the subprocess
# stays alive across multiple user messages instead of exiting after
# the first response; `--include-partial-messages` emits
# content_block_delta frames as Claude generates them (gives the UI a
# streaming feel); `--verbose` surfaces hook output and session-init
# detail in the event stream — useful during exploration, revisit once
# the pane has richer diagnostics of its own.
_CLAUDE_BASE_ARGV: tuple[str, ...] = (
    "claude",
    "-p",
    "--input-format",
    "stream-json",
    "--output-format",
    "stream-json",
    "--include-partial-messages",
    "--verbose",
)


# Timeout (seconds) for cooperative SIGTERM shutdown before we SIGKILL.
# Matches NvimBackend's `proc.wait(timeout=…)` budget — if `claude`
# hasn't exited within this window the process is unresponsive and we
# force termination so the GUI shutdown path doesn't block on it.
_SHUTDOWN_GRACE_SECONDS = 2.0


def parse_stream_json_line(line: str) -> dict | None:
    """Parse one stdout line into a stream-json event dict.

    Returns ``None`` for blank / whitespace-only lines and for lines
    that aren't valid JSON — the worker loop logs and continues in
    both cases rather than crashing. Extracted as a free function so
    tests exercise it without spawning a subprocess.

    BOM-tolerance: `claude`'s stream never prepends a BOM in
    practice, but we strip one defensively so a future change
    doesn't silently wedge the parser.
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
    """Owns a `claude -p` subprocess and pumps its stream-json output.

    Thread layout:
      GUI thread   — `start`, `stop`, `send_user_message`, signal
                      connection setup, property reads.
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

    # One stderr line at a time — `claude` prints auth failures and
    # warnings here. Surface them to the UI as a dim info row (future)
    # or via the app log (placeholder).
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
        """Spawn `claude` in stream-json mode; deliver `prompt` as first turn.

        The subprocess is configured with `--input-format stream-json`
        so stdin stays open across turns — we write the initial prompt
        as the first JSONL user envelope via `send_user_message`, then
        subsequent composer submits become additional JSONL lines on
        the same stdin stream. `claude` only exits when stdin closes
        (via `stop()`) or on error.

        No-op if called while a subprocess is already spawned.
        """
        if self._proc is not None:
            log.warning("SessionHost.start called while already running — ignored")
            return
        if not prompt:
            log.warning("SessionHost.start with empty prompt — not spawning")
            return
        argv = list(_CLAUDE_BASE_ARGV)
        log.info("spawning claude: %s", " ".join(shlex.quote(a) for a in argv))
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
                "failed to spawn claude — is the `claude` CLI installed and on PATH?"
            )
            self._stop_event.set()
            self.closed.emit()
            return
        except Exception:
            log.exception("failed to spawn claude")
            self._stop_event.set()
            self.closed.emit()
            raise
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
                log.warning("claude did not exit on SIGTERM — sending SIGKILL")
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
        """Write a JSONL user message to the subprocess's stdin.

        Envelope shape (empirically verified against the real `claude`
        CLI — see docs/phases.md's Step 1 protocol discovery):

            {"type": "user", "message": {"role": "user", "content": "<text>"}}

        The inner `"role": "user"` is NOT optional — a prior iteration
        omitted it and the subprocess silently discarded the turn.
        Every field in this envelope is load-bearing; do not trim it
        down without re-running the protocol check.

        Serialisation: write + flush is wrapped in `_stdin_lock` so
        interleaved writes from concurrent callers can't corrupt the
        line framing. GIL already serialises same-handle writes in
        practice; the lock is explicit documentation and future-
        proofing for free-threaded builds.

        Silently returns if there's no running subprocess — callers
        should not need to branch on `is_running`.
        """
        proc = self._proc
        if proc is None or proc.stdin is None:
            log.debug("send_user_message with no running subprocess — dropped")
            return
        payload = json.dumps(
            {"type": "user", "message": {"role": "user", "content": text}}
        )
        line = payload + "\n"
        with self._stdin_lock:
            try:
                proc.stdin.write(line)
                proc.stdin.flush()
            except (BrokenPipeError, OSError):
                log.exception("send_user_message failed — subprocess gone?")

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
                log.exception("stream-json stdout loop crashed")
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
                log.exception("stream-json stderr loop crashed")
