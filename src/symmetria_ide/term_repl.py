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
    {"type": "stop"}                                  -> clean shutdown + quit

  Stdout events:

    {"id": "...", "type": "ack", "status": "ok"}
    {"id": "...", "type": "snapshot", "rows": [...], "cursor": [r, c],
                  "cols": N, "rows_count": N}
    {"type": "closed"}                                -> shell exited
    {"id": "...", "type": "error", "message": "...",
                  "command_type": "..."}

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
import sys
import threading
from pathlib import Path
from typing import Any

from PySide6.QtCore import QCoreApplication, QObject, Qt, Signal, Slot

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

    def _on_closed(self) -> None:
        # Shell exited (EOF on master fd or `exit` typed). We do NOT
        # auto-quit the app — the parent process may want to query final
        # screen state via `snapshot`, then send `stop` explicitly.
        self._emit_event({"type": "closed"})

    def _shutdown(self, req_id: Any, suppress_ack: bool = False) -> None:
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
        line = json.dumps(event, ensure_ascii=False)
        with self._stdout_lock:
            sys.stdout.write(line + "\n")
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
