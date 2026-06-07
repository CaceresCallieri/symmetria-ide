"""Headless TerminalBackend driver — REPL over JSONL on stdin/stdout.

Wraps `TerminalBackend` in a `QCoreApplication` (no GUI) and exposes its
keystroke / snapshot / lifecycle surface as a newline-delimited-JSON
protocol. Lets external scripts (debugging, end-to-end tests, future
agent tooling) programmatically drive a Symmetria-IDE-flavored shell.

Run:
    PYTHONPATH=src python -m symmetria_ide.term_repl

Protocol (one JSON object per line, both directions):

  Stdin commands (each may carry an optional ``"id": "..."`` for ack
  matching; types missing required fields produce an `error` event):

    {"type": "start",  "cwd": "/home/jc"}
    {"type": "write",  "data": "\\u0005"}            -> bytes via JSON unicode
    {"type": "write_b64", "data_b64": "BQ=="}        -> bytes via base64
    {"type": "resize", "cols": 120, "rows": 40}
    {"type": "snapshot"}                              -> current viewport text
    {"type": "wait_for", "pattern": "...",            -> wait for regex in viewport
                         "timeout_ms": 5000}
    {"type": "stop"}                                  -> clean shutdown + quit

  Stdout events:

    {"id": "...", "type": "ack", "status": "ok"}
    {"id": "...", "type": "snapshot", "rows": [...], "cursor": [r, c],
                  "cols": N, "rows_count": N}
    {"id": "...", "type": "match", "elapsed_ms": N}  -> wait_for matched
    {"id": "...", "type": "timeout"}                  -> wait_for timed out
    {"type": "closed"}                                -> shell exited
    {"id": "...", "type": "error", "message": "...",
                  "command_type": "..."}

`wait_for` semantics: ack-on-completion, NOT ack-on-receipt. The
response is exactly one of `match` or `timeout`, carrying the same
`id` as the request. Patterns are Python regexes searched against the
viewport text (rows joined by `\\n`). Single-watch at a time —
concurrent `wait_for` commands receive an `error` envelope.

Single-shell, single-client by design — multiple concurrent writers to
the PTY would interleave bytes mid-escape-sequence. Spawn a fresh
process per debugging session.

Concurrency discipline mirrors the IDE proper:
  - Stdin reader runs on a `daemon=True` thread (§1 P0)
  - Cross-thread dispatch via `Qt.QueuedConnection` (§4 P2)
  - All backend interaction happens on the main Qt thread, never from
    the stdin reader thread directly
"""

from __future__ import annotations

import base64
import json
import logging
import re
import sys
import threading
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QCoreApplication,
    QElapsedTimer,
    QObject,
    Qt,
    QTimer,
    Signal,
    Slot,
)

from .jsonl_transport import encode_jsonl_line
from .terminal_backend import TerminalBackend


log = logging.getLogger(__name__)


def _snapshot_rows(screen: Any) -> list[str]:
    """Render pyte's current viewport buffer to a list of strings, one
    per row. Trailing spaces are stripped so the JSON payload stays
    compact (most rows in a fresh shell are empty after the prompt)."""
    return [
        "".join(screen.buffer[y][x].data for x in range(screen.columns)).rstrip()
        for y in range(screen.lines)
    ]


class TermRepl(QObject):
    """JSONL protocol coordinator.

    Lifecycle: stdin reader thread parses JSONL → emits `command_received`
    cross-thread → queued dispatch onto the main Qt thread → calls
    TerminalBackend methods → backend signals fire → stdout events
    written from main thread.

    Construction is decoupled from `start_stdin_loop()` so tests can
    drive `_dispatch_command` directly without spawning the reader
    thread.
    """

    # Cross-thread emit from the stdin reader → queued onto main thread.
    # Same shape as `AppController.session_event_received`.
    command_received = Signal(dict)

    def __init__(self) -> None:
        super().__init__()
        self._backend = TerminalBackend()
        self._stdout_lock = threading.Lock()
        # Stdin reader thread → main thread for command dispatch.
        # Qt.QueuedConnection per project-standards §4 P2.
        self.command_received.connect(
            self._dispatch_command, Qt.ConnectionType.QueuedConnection
        )
        # Backend `closed` is emitted from its reader thread — queued
        # connection mirrors the IDE's wiring in AppController.
        self._backend.closed.connect(
            self._on_closed, Qt.ConnectionType.QueuedConnection
        )
        # wait_for state — single-watch at a time (concurrent waits are
        # rejected with an error envelope rather than queued; see the
        # protocol note in the module docstring). All members are set
        # together by `_start_wait_for` and cleared by `_cancel_wait_for`.
        self._wait_req_id: Any = None
        self._wait_regex: re.Pattern[str] | None = None
        self._wait_started: QElapsedTimer | None = None
        self._wait_timeout_timer: QTimer | None = None

    def start_stdin_loop(self) -> None:
        """Spawn the daemon thread that consumes stdin."""
        thread = threading.Thread(
            target=self._stdin_loop, name="term-repl-stdin", daemon=True
        )
        thread.start()

    # --- Stdin reader (runs on daemon thread) --------------------------

    def _stdin_loop(self) -> None:
        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue
            try:
                cmd = json.loads(line)
            except json.JSONDecodeError as exc:
                self._emit_event({"type": "error", "message": f"json parse: {exc}"})
                continue
            if not isinstance(cmd, dict):
                self._emit_event(
                    {"type": "error", "message": "command must be a JSON object"}
                )
                continue
            self.command_received.emit(cmd)
        # stdin EOF — request clean shutdown of the main loop. The
        # synthetic `_eof` marker tells `_shutdown` to skip the trailing
        # ack: the client has closed its end of the pipe so the ack
        # would be unread noise (and would clutter test output captured
        # via subprocess.PIPE).
        self.command_received.emit({"type": "stop", "_eof": True})

    # --- Main thread (Qt event loop) -----------------------------------

    @Slot(dict)
    def _dispatch_command(self, cmd: dict[str, Any]) -> None:
        cmd_type = cmd.get("type", "")
        req_id = cmd.get("id")
        try:
            if cmd_type == "start":
                cwd = cmd.get("cwd") or str(Path.home())
                self._backend.start(cwd)
                self._ack(req_id)
            elif cmd_type == "write":
                data = cmd.get("data", "")
                if not isinstance(data, str):
                    raise ValueError("'data' must be a JSON string")
                self._backend.write(data.encode("utf-8"))
                self._ack(req_id)
            elif cmd_type == "write_b64":
                data_b64 = cmd.get("data_b64", "")
                if not isinstance(data_b64, str):
                    raise ValueError("'data_b64' must be a JSON string")
                self._backend.write(base64.b64decode(data_b64))
                self._ack(req_id)
            elif cmd_type == "resize":
                cols = int(cmd.get("cols", 80))
                rows = int(cmd.get("rows", 24))
                self._backend.resize(cols, rows)
                self._ack(req_id)
            elif cmd_type == "snapshot":
                self._emit_snapshot(req_id)
            elif cmd_type == "wait_for":
                pattern = cmd.get("pattern", "")
                if not isinstance(pattern, str):
                    raise ValueError("'pattern' must be a JSON string")
                timeout_ms = int(cmd.get("timeout_ms", 5000))
                self._start_wait_for(req_id, pattern, timeout_ms)
            elif cmd_type == "stop":
                # `_eof` marker is set by the stdin reader on stdin close;
                # in that case the client has already gone away and the
                # ack would be unread noise. Skip it for protocol cleanliness.
                self._shutdown(req_id, suppress_ack=bool(cmd.get("_eof")))
            else:
                raise ValueError(f"unknown command type: {cmd_type!r}")
        except Exception as exc:  # noqa: BLE001 — REPL must not crash on bad input
            log.exception("unhandled error dispatching %r command", cmd_type)
            self._emit_event(
                {
                    "id": req_id,
                    "type": "error",
                    "message": str(exc),
                    "command_type": cmd_type,
                }
            )

    def _ack(self, req_id: Any) -> None:
        self._emit_event({"id": req_id, "type": "ack", "status": "ok"})

    def _emit_snapshot(self, req_id: Any) -> None:
        # `_backend._screen` is a deliberate reach across visibility —
        # this REPL is a debug surface, not production, and exposing the
        # full screen-as-text is its whole purpose. A future `snapshot`
        # method on TerminalBackend could formalize this, but YAGNI for
        # the v1 single-consumer case.
        screen = self._backend._screen  # noqa: SLF001
        if screen is None:
            self._emit_event(
                {
                    "id": req_id,
                    "type": "error",
                    "message": "no active shell — call start first",
                    "command_type": "snapshot",
                }
            )
            return
        self._emit_event(
            {
                "id": req_id,
                "type": "snapshot",
                "rows": _snapshot_rows(screen),
                "cursor": [screen.cursor.y, screen.cursor.x],
                "cols": screen.columns,
                "rows_count": screen.lines,
            }
        )

    # --- wait_for: subscribe to screen updates until pattern or timeout -

    def _check_pattern(self, regex: re.Pattern[str]) -> bool:
        screen = self._backend._screen  # noqa: SLF001 — debug surface
        if screen is None:
            return False
        # Join rows with `\n` so the regex can span lines via `.` if the
        # user passes the DOTALL flag, but in the common case patterns
        # match within a single row and the newlines are inert.
        return bool(regex.search("\n".join(_snapshot_rows(screen))))

    def _start_wait_for(self, req_id: Any, pattern: str, timeout_ms: int) -> None:
        if self._wait_regex is not None:
            self._emit_event(
                {
                    "id": req_id,
                    "type": "error",
                    "message": "another wait_for is in progress",
                    "command_type": "wait_for",
                }
            )
            return
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            self._emit_event(
                {
                    "id": req_id,
                    "type": "error",
                    "message": f"invalid regex: {exc}",
                    "command_type": "wait_for",
                }
            )
            return

        # Set state BEFORE connecting: _on_wait_screen_dirty reads
        # self._wait_regex on dispatch; setting it first makes the slot
        # provably safe regardless of Qt's queued-delivery timing.
        elapsed = QElapsedTimer()
        elapsed.start()
        self._wait_req_id = req_id
        self._wait_regex = regex
        self._wait_started = elapsed

        # queued: TerminalBackend reader → TermRepl main thread (§4 P2)
        self._backend.screen_dirty.connect(
            self._on_wait_screen_dirty, Qt.ConnectionType.QueuedConnection
        )

        if self._check_pattern(regex):
            self._finish_wait_for_match(elapsed_ms=0)
            return

        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(self._on_wait_timeout)
        timer.start(timeout_ms)
        self._wait_timeout_timer = timer

    @Slot(frozenset)
    def _on_wait_screen_dirty(self, _: frozenset[int]) -> None:
        regex = self._wait_regex
        if regex is None:
            return
        if self._check_pattern(regex):
            elapsed_ms = self._wait_started.elapsed() if self._wait_started else 0
            self._finish_wait_for_match(elapsed_ms=elapsed_ms)

    def _on_wait_timeout(self) -> None:
        if self._wait_regex is None:
            return
        req_id = self._wait_req_id
        self._cancel_wait_for()
        self._emit_event({"id": req_id, "type": "timeout"})

    def _finish_wait_for_match(self, elapsed_ms: int) -> None:
        req_id = self._wait_req_id
        self._cancel_wait_for()
        self._emit_event({"id": req_id, "type": "match", "elapsed_ms": elapsed_ms})

    def _cancel_wait_for(self) -> None:
        try:
            self._backend.screen_dirty.disconnect(self._on_wait_screen_dirty)
        except (RuntimeError, TypeError):
            # Already disconnected — Qt raises RuntimeError on
            # PySide6, TypeError on older bindings. Either is fine.
            pass
        if self._wait_timeout_timer is not None:
            self._wait_timeout_timer.stop()
            self._wait_timeout_timer = None
        self._wait_req_id = None
        self._wait_regex = None
        self._wait_started = None

    def _on_closed(self) -> None:
        # Shell exited (EOF on master fd or `exit` typed). Cancel any
        # pending wait_for first — the screen_dirty signal won't fire
        # again, so the timer would eventually produce a stale timeout
        # event onto a half-closed stdout if left running.
        self._cancel_wait_for()
        # We do NOT auto-quit the app — the parent process may want to
        # query final screen state via `snapshot`, then send `stop`
        # explicitly.
        self._emit_event({"type": "closed"})

    def _shutdown(self, req_id: Any, suppress_ack: bool = False) -> None:
        # Cancel any pending wait_for so its timer + signal connection
        # don't outlive the backend. Without this, a wait_for in flight
        # at shutdown time would emit a stale `timeout` event onto a
        # closed stdout (or worse, segfault when the timer fires after
        # QCoreApplication.quit).
        self._cancel_wait_for()
        try:
            self._backend.stop()
        finally:
            if not suppress_ack:
                self._ack(req_id)
            QCoreApplication.quit()

    def _emit_event(self, event: dict[str, Any]) -> None:
        # Stdout writes are serialized via lock because _stdin_loop
        # (daemon thread) calls _emit_event directly for parse errors,
        # concurrent with the main thread writing acks and snapshots.
        # Both threads share the same sys.stdout file object; the lock
        # prevents interleaved JSON lines.
        line = encode_jsonl_line(event, ensure_ascii=False)
        with self._stdout_lock:
            sys.stdout.write(line)
            sys.stdout.flush()


def main() -> int:
    # Arm crash traceback capture before any Qt / pyte import paths run.
    # Logs + crashes both go to stderr; stdout stays a clean JSON event stream.
    import faulthandler

    faulthandler.enable(file=sys.stderr, all_threads=True)
    logging.basicConfig(
        level=logging.WARNING,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    app = QCoreApplication(sys.argv)
    repl = TermRepl()
    repl.start_stdin_loop()
    return int(app.exec())


if __name__ == "__main__":
    sys.exit(main())
