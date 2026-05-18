"""Terminal backend: spawn a PTY-attached shell, pump pyte screen updates.

PR 1 — skeleton only. Signal declarations + method stubs raising
`NotImplementedError`. Implementation of PTY lifecycle, pyte
ByteStream integration, and the daemon reader thread lands in PR 2.
The signal + method surface here is what `TerminalView` (PR 3) and
`AppController` (PR 4) bind against, so getting the shape right
early lets downstream wiring start without churn.

Architectural invariants this module must obey once fleshed out (see
CLAUDE.md "The terminal pane" section once PR 5 lands, plus
`docs/phases.md` Phase 2.5 deliverable 2):

- Reader thread is `daemon=True` AND owns `_stop_event`
  (`.claude/project-standards.md` §1 P0).
- Cross-thread signal connects MUST use `Qt.QueuedConnection` with a
  grep-able one-line comment at the connect site (§4 P2).
- GC suspended around any worker-thread signal emit whose payload
  construction allocates Python objects (CLAUDE.md gotcha #10) —
  pyte's `feed()` + screen-state extraction is allocation-heavy and
  Python 3.14's incremental cyclic GC will race the QSGRenderThread
  if left enabled.
"""

from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QObject, Signal, Slot


log = logging.getLogger(__name__)


class TerminalBackend(QObject):
    """Owns the PTY + shell + pyte emulator for one terminal pane.

    Thread layout (PR 2): a daemon reader thread blocks on
    `select.select([master_fd, self_pipe_r], ...)`, reads bytes from
    the PTY master, feeds them into `pyte.ByteStream` which mutates
    `pyte.HistoryScreen` in place, then emits `screen_dirty` via
    `Qt.QueuedConnection` to the GUI thread for repaint.

    Why a dedicated reader thread instead of `QSocketNotifier` on the
    master fd: QSocketNotifier dispatches read-ready on the GUI
    thread, which would put pyte parsing on the paint-critical path.
    A worker thread keeps parsing off the GUI thread and matches the
    codebase's existing daemon-+-Event concurrency discipline
    (`NvimBackend`, `SessionHost`), so the GC-suspension recipe from
    gotcha #10 applies uniformly.
    """

    # Dirty row indices since the previous flush. Payload is the set
    # copied from `pyte.HistoryScreen.dirty` (the worker clears the
    # screen's dirty set immediately after copying). The v1 consumer
    # (`TerminalView.update()`) treats the payload as advisory and
    # repaints in full; the carried payload exists so a future v2
    # optimization can do partial-row repaints via `update(QRect)`.
    screen_dirty = Signal(set)

    # Terminal cell dimensions changed — emitted from `resize()`
    # AFTER pyte's screen has been re-laid-out and the TIOCSWINSZ
    # ioctl has been pushed to the slave PTY. Payload is `(cols, rows)`
    # so any future status surface that wants to display the active
    # shell dimensions can bind directly.
    screen_resized = Signal(int, int)

    # Shell process exited — EOF on the master fd, or the user typed
    # `exit`. One-shot signal: the backend cannot be restarted in
    # place. `AppController` is expected to construct a fresh
    # `TerminalBackend` when the user wants a new shell.
    closed = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        # Cooperative shutdown signal — set at the top of `stop()` and
        # in the reader thread's `finally` block, so observers can
        # wait on the backend's lifecycle without polling. Mirrors
        # `NvimBackend._stop_event`. Standards §1 P0.
        self._stop_event = threading.Event()
        # Lock guarding writes to the master fd. PR 2 uses this around
        # `os.write(master_fd, ...)` so the GUI thread (calls to
        # `write()`) and the reader thread (self-pipe wakeup byte at
        # shutdown) don't interleave. Same shape as `SessionHost`'s
        # `_stdin_lock`.
        self._stdin_lock = threading.Lock()
        # Populated by `start()` in PR 2 — kept as instance attrs so
        # `stop()` / `write()` / `resize()` can find them, and so
        # tests can assert on lifecycle state without poking modules.
        self._master_fd: int | None = None
        self._proc = None  # subprocess.Popen, declared lazily to avoid the import
        self._screen = None  # pyte.HistoryScreen
        self._stream = None  # pyte.ByteStream
        self._worker: threading.Thread | None = None
        self._self_pipe_r: int | None = None
        self._self_pipe_w: int | None = None

    @property
    def stop_event(self) -> threading.Event:
        """Shutdown signal — set as soon as teardown begins or the
        reader thread exits. Mirrors `NvimBackend.stop_event` for
        consistency in test scaffolding and any future coordinator
        that waits on the backend's lifecycle."""
        return self._stop_event

    # --- Lifecycle -----------------------------------------------------

    def start(self, cwd: str) -> None:
        """Spawn the shell under a PTY, attach pyte, start the reader.

        PR 2 will:
          - Allocate a PTY pair via `os.openpty()`.
          - Spawn `$SHELL` (fallback `/bin/bash`) as a login shell
            (`-l`) so the user's rc-files run — required for
            deliverable 3's OSC 7 hook to be installable from a
            normal `.zshrc` / `.bashrc`.
          - `preexec_fn=os.setsid` so the shell session is reachable
            via `killpg` at shutdown (orphan-process insurance).
          - `env={**os.environ, "TERM": "xterm-256color",
            "COLORTERM": "truecolor"}`.
          - `cwd=cwd` (the AppController-supplied initial path —
            NOT `displayedRoot`; the terminal lives upstream of the
            anchor transformation).
          - Instantiate `pyte.HistoryScreen(cols, rows)` and
            `pyte.ByteStream(screen)`.
          - Start a `daemon=True` reader thread blocking on
            `select.select([master_fd, self_pipe_r], [], [])`.
        """
        del cwd  # consumed in PR 2
        raise NotImplementedError("PR 2 — implementation pending")

    def stop(self) -> None:
        """Tear down: signal worker, killpg the shell, join thread.

        PR 2 will:
          1. Set `_stop_event` FIRST so any concurrent observer sees
             shutdown-in-progress before the OS round-trips start.
          2. Write a single byte to `_self_pipe_w` so the reader's
             `select()` returns and it can observe `_stop_event`.
          3. `os.killpg(os.getpgid(proc.pid), SIGTERM)` with a 2s
             grace, falling back to `SIGKILL`. Process-group kill
             reaps any children the shell spawned (e.g. a running
             `vim` or `htop` inside the terminal).
          4. Join the worker with timeout.
        """
        raise NotImplementedError("PR 2 — implementation pending")

    # --- GUI-thread-facing API -----------------------------------------

    @Slot(bytes)
    def write(self, data: bytes) -> None:
        """Forward bytes to the shell's stdin (PTY master).

        Called from the GUI thread by `TerminalView.keyPressEvent`
        after translating Qt key events to terminal escape sequences.
        PR 2 will hold `_stdin_lock` around
        `os.write(self._master_fd, data)`.
        """
        del data  # consumed in PR 2
        raise NotImplementedError("PR 2 — implementation pending")

    @Slot(int, int)
    def resize(self, cols: int, rows: int) -> None:
        """Re-lay-out pyte + push `TIOCSWINSZ` to the slave PTY.

        Called from the GUI thread when the terminal pane's geometry
        changes. PR 2 will:
          1. `self._screen.resize(rows, cols)` — pyte's argument
             order is `(lines, columns)`, inverted from our public
             API. The inversion is intentionally absorbed here so
             every caller (QML resize, AppController test scaffold)
             stays Pythonic `(cols, rows)`.
          2. `fcntl.ioctl(master_fd, termios.TIOCSWINSZ,
                          struct.pack("HHHH", rows, cols, 0, 0))` so
             the shell's `$LINES` / `$COLUMNS` reflow and any TUI
             (htop, fzf, vim) inside the terminal redraws cleanly.
          3. Emit `screen_resized(cols, rows)`.
        """
        del cols, rows  # consumed in PR 2
        raise NotImplementedError("PR 2 — implementation pending")
