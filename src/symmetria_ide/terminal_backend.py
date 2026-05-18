"""Terminal backend: spawn a PTY-attached shell, pump pyte screen updates.

Owns the PTY pair, the shell subprocess, the pyte emulator, and the
daemon reader thread that feeds shell output into pyte and emits
`screen_dirty` cross-thread to the GUI. The public surface (signals
+ Slot methods) is what `TerminalView` (PR 3) and `AppController`
(PR 4) bind against.

Architectural invariants this module obeys (regression risk if any
of these slips — re-read CLAUDE.md "The terminal pane" section once
PR 5 lands, plus `docs/phases.md` Phase 2.5 deliverable 2):

- Reader thread is `daemon=True` AND owns `_stop_event`
  (`.claude/project-standards.md` §1 P0). Mirrors `NvimBackend` and
  `SessionHost`. The daemon flag covers interpreter-exit hangs; the
  Event makes shutdown observable for tests + future coordinators.
- Cross-thread signal connects MUST use `Qt.QueuedConnection` with a
  grep-able one-line comment at the connect site (§4 P2). That connect
  lives in `AppController.__init__` (PR 4) — not here.
- GC suspended (`gc.disable()` / `gc.enable()`) around the worker's
  `pyte.feed()` + emit window (CLAUDE.md gotcha #10). pyte allocates
  freely per cell update, and Python 3.14's incremental cyclic GC will
  race the `QSGRenderThread` if left enabled. Widening the suspension
  to cover the whole feed-and-emit window matches the recipe
  `NvimBackend._on_notification` already uses.
- `screen_dirty` payload is `frozenset[int]` — Qt's QueuedConnection
  passes set objects by reference (not copied), so emitting a mutable
  set leaves the same object live in two threads' GC graphs and
  expands the gotcha #10 race window. frozenset is immutable;
  PySide6 raises TypeError at emit-time if the contract is violated.
"""

from __future__ import annotations

import fcntl
import gc
import logging
import os
import select
import signal
import struct
import subprocess
import termios
import threading

import pyte
from PySide6.QtCore import QObject, Signal, Slot


log = logging.getLogger(__name__)


# Default screen dimensions used at start() time, before the QML
# geometry resolves and pushes a real resize. 80x24 is the kernel /
# xterm canonical default — any TUI launched immediately after start
# sees a sane geometry until the first `resize()` Slot fires.
_DEFAULT_COLS = 80
_DEFAULT_ROWS = 24

# Per-iteration read size from the master fd. 64 KiB is the standard
# pipe buffer ceiling on Linux; reading larger chunks does not improve
# throughput because the kernel won't deliver more than the buffer.
_READ_CHUNK_BYTES = 65536

# Scrollback ring depth. pyte's HistoryScreen keeps `history` lines of
# scrollback divided into top/bottom buckets per `ratio`. 1000 lines
# is enough for typical shell sessions without making the in-memory
# Char namedtuple footprint silly.
_SCROLLBACK_LINES = 1000


class TerminalBackend(QObject):
    """Owns the PTY + shell + pyte emulator for one terminal pane.

    Thread layout: a daemon reader thread blocks on
    `select.select([master_fd, self_pipe_r], ...)`, reads bytes from
    the PTY master, feeds them into `pyte.ByteStream` which mutates
    `pyte.HistoryScreen` in place, then emits `screen_dirty` via
    `Qt.QueuedConnection` to the GUI thread for repaint. The
    self-pipe is the cooperative shutdown channel — `stop()` writes
    a byte into it so the reader's `select()` returns and observes
    `_stop_event`.

    Why a dedicated reader thread instead of `QSocketNotifier` on the
    master fd: QSocketNotifier dispatches read-ready on the GUI
    thread, which would put pyte parsing on the paint-critical path.
    A worker thread keeps parsing off the GUI thread and matches the
    codebase's existing daemon-+-Event concurrency discipline
    (`NvimBackend`, `SessionHost`), so the GC-suspension recipe from
    gotcha #10 applies uniformly.
    """

    # Dirty row indices since the previous flush. Payload is the
    # frozenset copied from `pyte.HistoryScreen.dirty`; the worker
    # clears the screen's dirty set immediately after copying. The
    # v1 consumer (`TerminalView.update()`) treats the payload as
    # advisory and repaints in full; the carried payload exists so
    # a future v2 optimization can do partial-row repaints via
    # `update(QRect)`. frozenset (not set) is load-bearing — see
    # the module docstring.
    screen_dirty = Signal(frozenset)

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
        # Lock guarding writes to the master fd. Used by `write()` so
        # the GUI thread (user keystrokes) and any future writer
        # don't interleave bytes mid-escape-sequence. Same shape as
        # `SessionHost`'s `_stdin_lock`.
        self._stdin_lock = threading.Lock()
        # Populated by `start()` — kept as instance attrs so
        # `stop()` / `write()` / `resize()` can find them, and so
        # tests can assert on lifecycle state.
        self._master_fd: int | None = None
        self._proc: subprocess.Popen[bytes] | None = None
        self._screen: pyte.HistoryScreen | None = None
        self._stream: pyte.ByteStream | None = None
        self._worker: threading.Thread | None = None
        # Self-pipe: `stop()` writes a byte into `_self_pipe_w` so the
        # reader's `select()` returns and it can observe `_stop_event`.
        # This is the standard Linux pattern for waking a blocked
        # select; signal-based wakeup would be racier on multi-threaded
        # Python (signals only deliver to the main thread).
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

        Idempotent: a second call while a shell is already live returns
        immediately. To spawn a new shell after one exits, construct a
        fresh `TerminalBackend` — the closed-signal contract documents
        this is one-shot per instance.

        Initial screen dimensions are `_DEFAULT_COLS` × `_DEFAULT_ROWS`
        (80×24). The QML side calls `resize()` as soon as its geometry
        resolves — typically within a frame or two of mount.
        """
        if self._proc is not None:
            return

        # Allocate the PTY pair. The kernel inherits 80×24 winsz by
        # default; we'll push our explicit dims via ioctl before exec
        # so the shell's $LINES / $COLUMNS reflect reality from the
        # first prompt.
        master_fd, slave_fd = os.openpty()

        # Self-pipe for cooperative shutdown wake-up of select().
        self_pipe_r, self_pipe_w = os.pipe()

        # Set TIOCSWINSZ on the master BEFORE spawning so the child's
        # initial environment sees the right dims. struct.pack format
        # is (rows, cols, xpixel, ypixel) — only the first two matter
        # for terminal apps.
        try:
            fcntl.ioctl(
                master_fd,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", _DEFAULT_ROWS, _DEFAULT_COLS, 0, 0),
            )
        except OSError:
            log.exception("initial TIOCSWINSZ failed")

        # pyte instances. HistoryScreen keeps scrollback; ByteStream
        # tokenises raw bytes (handles UTF-8 multi-byte) and feeds
        # the screen.
        self._screen = pyte.HistoryScreen(
            _DEFAULT_COLS,
            _DEFAULT_ROWS,
            history=_SCROLLBACK_LINES,
            ratio=0.5,
        )
        self._stream = pyte.ByteStream(self._screen)
        # Force a full-screen repaint on the first frame so the QML
        # paint loop has something to draw before the shell emits its
        # first prompt redraw.
        self._screen.dirty.update(range(_DEFAULT_ROWS))

        # Shell launch. Login shell (-l) so the user's rc-files run —
        # required for Phase 2.5 deliverable 3's OSC 7 hook to be
        # installable from a normal `.zshrc` / `.bashrc`. setsid puts
        # the shell into its own session+process group so `killpg` at
        # shutdown reaps any TUIs (vim, htop) the user launched inside.
        shell = os.environ.get("SHELL") or "/bin/bash"
        env = {
            **os.environ,
            "TERM": "xterm-256color",
            "COLORTERM": "truecolor",
        }
        try:
            self._proc = subprocess.Popen(
                [shell, "-l"],
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=cwd,
                env=env,
                preexec_fn=os.setsid,  # noqa: PLW1509 — setsid is exactly what we want here
                close_fds=True,
            )
        except OSError:
            # Spawn failed — close the fds we opened so we don't leak.
            os.close(master_fd)
            os.close(slave_fd)
            os.close(self_pipe_r)
            os.close(self_pipe_w)
            self._screen = None
            self._stream = None
            raise

        # Parent retains master only; child inherited the slave and
        # the parent's slave handle is no longer needed.
        os.close(slave_fd)

        self._master_fd = master_fd
        self._self_pipe_r = self_pipe_r
        self._self_pipe_w = self_pipe_w

        # Reader thread. daemon=True per §1 P0; the cooperative shape
        # is _stop_event + self-pipe (NOT bare daemon-only).
        self._worker = threading.Thread(
            target=self._run_reader_loop,
            name="terminal-reader",
            daemon=True,
        )
        self._worker.start()

    def stop(self) -> None:
        """Tear down: signal worker, killpg the shell, join thread, close fds.

        Idempotent: safe to call before `start()` or after a previous
        `stop()`. Order is load-bearing:
          1. Set `_stop_event` FIRST so any concurrent observer sees
             shutdown-in-progress before any blocking syscall starts.
          2. Wake the reader's `select()` via the self-pipe so it can
             observe the stop flag and exit cleanly.
          3. `killpg` the shell's process group so any nested
             children (`vim`, `htop`, etc.) get reaped, then `wait()`
             with a 2-second grace before SIGKILL.
          4. Join the worker with a timeout — daemon=True covers the
             interpreter-exit path if the join races.
          5. Close fds last; closing them before the worker exits
             would race the reader's `os.read()`.
        """
        if self._proc is None and self._worker is None:
            return

        self._stop_event.set()

        # Wake the reader's select(). If the pipe is already closed
        # (e.g. worker raced ahead), OSError is benign here.
        if self._self_pipe_w is not None:
            try:
                os.write(self._self_pipe_w, b"x")
            except OSError:
                pass

        # Kill the shell's process group.
        proc = self._proc
        if proc is not None:
            try:
                pgid = os.getpgid(proc.pid)
                try:
                    os.killpg(pgid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    try:
                        proc.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        log.warning("shell didn't exit after SIGKILL")
            except ProcessLookupError:
                # Shell already exited on its own — fine.
                pass
            except OSError:
                log.exception("killpg failed during shutdown")

        # Join the worker.
        if self._worker is not None:
            self._worker.join(timeout=2.0)

        # Close all fds we own.
        for attr in ("_master_fd", "_self_pipe_r", "_self_pipe_w"):
            fd = getattr(self, attr)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                setattr(self, attr, None)

        self._proc = None
        self._worker = None
        self._screen = None
        self._stream = None

    # --- GUI-thread-facing API -----------------------------------------

    @Slot(bytes)
    def write(self, data: bytes) -> None:
        """Forward bytes to the shell's stdin (PTY master).

        Called from the GUI thread by `TerminalView.keyPressEvent`
        (PR 3) after translating Qt key events to terminal escape
        sequences. The lock keeps multi-byte sequences (e.g. arrow
        keys = 3 bytes) from interleaving with any future
        secondary writer.
        """
        if self._master_fd is None or not data:
            return
        with self._stdin_lock:
            master_fd = self._master_fd
            if master_fd is None:
                # Raced with stop() — drop the write silently.
                return
            try:
                os.write(master_fd, data)
            except OSError:
                log.exception("write to master fd failed")

    @Slot(int, int)
    def resize(self, cols: int, rows: int) -> None:
        """Re-lay-out pyte + push `TIOCSWINSZ` to the slave PTY.

        Called from the GUI thread when the terminal pane's geometry
        changes. pyte's argument order is `(lines, columns)` —
        inverted from our public Pythonic `(cols, rows)`. The
        inversion is absorbed here so every caller stays consistent.

        After the resize, every row is marked dirty so the next paint
        repaints in full (any TUI inside the shell will also emit its
        own redraw in response to SIGWINCH, but our forced full
        repaint covers the gap until that arrives).
        """
        if self._screen is None or self._master_fd is None:
            return
        if cols <= 0 or rows <= 0:
            return

        self._screen.resize(rows, cols)  # pyte takes (lines, columns)

        try:
            fcntl.ioctl(
                self._master_fd,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", rows, cols, 0, 0),
            )
        except OSError:
            log.exception("TIOCSWINSZ failed")

        # Force a full repaint — pyte's dirty set may have been empty
        # before resize, but every cell's screen coordinates just
        # changed semantically.
        self._screen.dirty.update(range(rows))

        self.screen_resized.emit(cols, rows)

    # --- Worker thread -------------------------------------------------

    def _run_reader_loop(self) -> None:
        """Select on master_fd + self_pipe; feed pyte; emit screen_dirty.

        GC is suspended across the entire `feed` + dirty-extract +
        emit window (CLAUDE.md gotcha #10): pyte allocates per-cell
        on every update, and Python 3.14's cyclic GC racing
        `QSGRenderThread` mid-paint is the documented SEGV class
        `NvimBackend._on_notification` already mitigates. Mirroring
        that recipe here keeps the concurrency discipline uniform.

        The loop exits on:
          - `_stop_event` set + self-pipe wake-up (cooperative `stop()`)
          - EOF from `os.read(master_fd)` (shell exited on its own)
          - OSError on `select` or `read` (fd was closed externally)
        """
        assert self._master_fd is not None
        assert self._self_pipe_r is not None
        assert self._stream is not None
        assert self._screen is not None

        master_fd = self._master_fd
        self_pipe_r = self._self_pipe_r
        stream = self._stream
        screen = self._screen

        try:
            while not self._stop_event.is_set():
                try:
                    ready, _, _ = select.select([master_fd, self_pipe_r], [], [])
                except OSError:
                    # fd closed during shutdown — clean exit.
                    break

                if self_pipe_r in ready:
                    # Drain the wake-up byte(s). Loop because stop()
                    # may write more than one before we observe.
                    try:
                        os.read(self_pipe_r, 64)
                    except OSError:
                        pass
                    if self._stop_event.is_set():
                        break

                if master_fd in ready:
                    try:
                        data = os.read(master_fd, _READ_CHUNK_BYTES)
                    except OSError:
                        break
                    if not data:
                        # EOF — shell exited.
                        break

                    # GC suspended across feed + dirty-extract + emit.
                    # See gotcha #10 + module docstring.
                    gc_was_enabled = gc.isenabled()
                    if gc_was_enabled:
                        gc.disable()
                    try:
                        stream.feed(data)
                        if screen.dirty:
                            dirty_snapshot = frozenset(screen.dirty)
                            screen.dirty.clear()
                            self.screen_dirty.emit(dirty_snapshot)
                    finally:
                        if gc_was_enabled:
                            gc.enable()
        except Exception:
            if not self._stop_event.is_set():
                log.exception("terminal reader loop crashed")
        finally:
            # Set unconditionally — covers cooperative stop() and the
            # "shell exited / fd closed" paths so anyone blocking on
            # stop_event.wait() is unblocked either way. Matches the
            # NvimBackend._run_loop finally pattern.
            self._stop_event.set()
            self.closed.emit()
