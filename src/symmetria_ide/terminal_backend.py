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
import urllib.parse
from collections.abc import Callable
from pathlib import Path

import pyte
from PySide6.QtCore import QObject, Signal, Slot


log = logging.getLogger(__name__)


# Path to the Symmetria-IDE-managed shell-init scripts that auto-inject
# the OSC 7 cwd emitter. Phase 2.5 deliverable 3.
_SHELL_INIT_DIR = (
    Path(__file__).resolve().parent.parent.parent / "runtime" / "symmetria-shell"
)


def _shell_launch_args(shell_path: str) -> tuple[list[str], dict[str, str]]:
    """Build (argv, env_additions) tuple to spawn `shell_path` with
    Symmetria's OSC 7 auto-injection enabled.

    zsh: ZDOTDIR redirects rcfile lookup to runtime/symmetria-shell/zsh/,
    which has a .zshenv + .zshrc that source the user's real files first
    and then append the chpwd hook. Same pattern Kitty / Ghostty / WezTerm
    use for zsh shell integration.

    bash: --rcfile points at runtime/symmetria-shell/bash/init.sh, which
    sources the user's login + interactive rcfiles manually then installs
    the PROMPT_COMMAND hook. We pass `-i` (interactive) WITHOUT `-l`
    because bash ignores --rcfile when invoked as a login shell. The
    init script handles `.bash_profile` / `.profile` sourcing explicitly
    to make up for the dropped login semantics.

    Other shells (fish, nu, etc.): plain `[shell, "-l"]` with no env
    injection — log a warning so the user knows OSC 7 won't fire until
    they manually install a chpwd hook in their shell's config. Adding
    auto-injection for these is a v2 follow-up; the wire format is
    shell-independent, only the hook installation differs.
    """
    shell_name = os.path.basename(shell_path)

    if shell_name == "zsh":
        zdotdir = str(_SHELL_INIT_DIR / "zsh")
        return [shell_path, "-l"], {"ZDOTDIR": zdotdir}

    if shell_name == "bash":
        rcfile = str(_SHELL_INIT_DIR / "bash" / "init.sh")
        # `-i` without `-l`: bash ignores --rcfile in login mode, so the
        # init.sh sources login files manually instead.
        return [shell_path, "--rcfile", rcfile, "-i"], {}

    log.warning(
        "No Symmetria OSC 7 auto-injection for shell %r — "
        "terminal-driven cwd sync will not work until the user manually "
        "installs a chpwd hook. zsh and bash are auto-injected in v1.",
        shell_name,
    )
    return [shell_path, "-l"], {}


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

# Cap on the partial-OSC buffer carried across reader-loop iterations.
# A well-formed OSC 7 sequence (file://host/path) almost never exceeds
# a few hundred bytes in practice (path length is the only variable
# component); 4 KiB tolerates wildly long paths with margin. Deliberate
# undersize vs `_READ_CHUNK_BYTES` (64 KiB) — any OSC 7 exceeding 4 KiB
# is certainly garbled binary output, not a real filesystem path. If the
# buffer ever grows past this, the input is almost certainly malformed
# (unterminated escape sequence emitted by a buggy shell hook or
# garbled binary output) — we drop the buffer to recover.
_OSC_BUFFER_CAP = 4096


class _AnswerbackHistoryScreen(pyte.HistoryScreen):
    """HistoryScreen wired to actually answer DA1 / DSR queries.

    pyte's stock `Screen` already builds the correct CSI response
    strings inside `report_device_attributes` (DA1, `ESC [ ? 6 c` for
    VT102 identity) and `report_device_status` (DSR mode 5 -> terminal
    status, mode 6 -> cursor position `ESC [ <row> ; <col> R`). Both
    end with `self.write_process_input(reply)` — which is a NO-OP stub
    by default. Programs that issue these queries (fzf's `--height`
    mode used by `zoxide query -i`, vim's `xterm-bg` detection, less,
    btop, …) block waiting for the reply and either hang forever or
    fall back to broken defaults when none arrives.

    Empirical context: `zoxide query -i` invoked from this terminal
    hung indefinitely until we wired up DSR responses — direct
    `fzf < /etc/passwd` renders in ~30ms because fullscreen fzf does
    not issue DSR; only the `--height` codepath does. Diagnostic
    scripts under `tools/diag_*.py` reproduce both before/after.

    Threading: `write_process_input` runs synchronously inside
    `pyte.feed()`, which only runs on `TerminalBackend`'s reader
    thread. The callback we pass is `TerminalBackend.write`, which
    serialises via `_stdin_lock` against main-thread keystroke writes.

    GC: this call lands inside the gc-suspended window in
    `_run_reader_loop` (gotcha #10) — the small `bytes` allocation
    from `data.encode("latin-1")` is safe there.
    """

    def __init__(
        self,
        columns: int,
        lines: int,
        history: int = 100,
        ratio: float = 0.5,
        write_callback: Callable[[bytes], None] | None = None,
    ) -> None:
        super().__init__(columns, lines, history=history, ratio=ratio)
        self._write_callback = write_callback

    def write_process_input(self, data: str) -> None:
        # pyte responses are pure ASCII CSI sequences; latin-1 is a
        # safe 1:1 codec for bytes 0-255 if any future pyte release
        # broadens the response alphabet. Empty payload is a noop.
        if not data or self._write_callback is None:
            return
        self._write_callback(data.encode("latin-1"))


def _parse_osc7(buffer: bytes, new_data: bytes) -> tuple[bytes, list[str], bytes]:
    """Extract OSC 7 sequences from a byte stream, return cleaned bytes + paths.

    OSC 7 wire format (xterm spec): `ESC ] 7 ; file://<host>/<path> TERM`
    where TERM is either BEL (`0x07`) or ST (`ESC \\` = `0x1b 0x5c`). The
    shell-init scripts use ST; some emulators / tools emit BEL; we accept
    both defensively. Phase 2.5 deliverable 3.

    `buffer` is the partial-OSC carryover from the previous reader-loop
    iteration (an OSC sequence can span multiple `os.read` returns when
    the kernel splits the read across a chunk boundary). `new_data` is
    the fresh bytes just read from the master fd. The returned tuple is
    `(cleaned_data, paths, remaining_partial)`:

      - `cleaned_data` — bytes with all completed OSC 7 sequences stripped,
        safe to feed to pyte. The cleaned stream still contains every
        non-OSC-7 byte from the input, in order, so the rendered terminal
        output is unchanged.
      - `paths` — list of cwd paths extracted from each completed OSC 7
        sequence, in the order they appeared. The caller emits one signal
        per path. Percent-encoded paths (e.g. `%20` for space) are decoded
        via `urllib.parse.unquote` so the routed payload is a clean
        filesystem path.
      - `remaining_partial` — any unterminated `ESC ] 7 ;` prefix at the
        very end of the input. The caller passes this back as `buffer`
        on the next call so the sequence can complete across reads.

    Pure function — no state, no I/O. The reader loop owns the buffer
    instance attribute. Trivially testable with synthetic byte streams.
    """
    combined = buffer + new_data
    paths: list[str] = []
    output = bytearray()
    i = 0
    n = len(combined)

    while i < n:
        esc_idx = combined.find(b"\x1b]7;", i)
        if esc_idx == -1:
            # No more OSC 7 starts in this chunk — flush remainder
            # to output and return with no partial.
            output.extend(combined[i:])
            return bytes(output), paths, b""

        # Bytes before the OSC start pass through unchanged.
        output.extend(combined[i:esc_idx])

        # Look for the earliest terminator (BEL or ST) after the OSC body.
        # NOTE: if the path itself contained ESC-\ (impossible on Linux
        # filesystems — ESC is not a valid filename byte), the ST search
        # would truncate the path silently at that byte. Theoretical only.
        bel_idx = combined.find(b"\x07", esc_idx + 4)
        st_idx = combined.find(b"\x1b\\", esc_idx + 4)

        if bel_idx == -1 and st_idx == -1:
            # Unterminated — save partial for the next iteration. If it
            # never completes (buggy emitter, garbled stream), the cap
            # below catches the runaway buffer.
            partial = combined[esc_idx:]
            if len(partial) > _OSC_BUFFER_CAP:
                log.warning(
                    "OSC 7 partial buffer exceeded %d bytes — dropping "
                    "(unterminated sequence from emitter?). Drop length=%d",
                    _OSC_BUFFER_CAP,
                    len(partial),
                )
                return bytes(output), paths, b""
            return bytes(output), paths, partial

        # Pick the earlier of the two terminators if both are present.
        if bel_idx == -1:
            term_idx, term_len = st_idx, 2
        elif st_idx == -1:
            term_idx, term_len = bel_idx, 1
        elif bel_idx < st_idx:
            term_idx, term_len = bel_idx, 1
        else:
            term_idx, term_len = st_idx, 2

        # Payload is the bytes between `ESC ] 7 ;` and the terminator.
        # Expected shape: `file://<host>/<path>` (host may be empty).
        payload = combined[esc_idx + 4 : term_idx]
        if payload.startswith(b"file://"):
            # Split on the first `/` after the `file://` prefix to skip
            # the host portion. RFC 8089: hostname may be empty (the
            # bare `file:///path` form is canonical for local files).
            host_end = payload.find(b"/", 7)
            if host_end != -1:
                path_bytes = payload[host_end:]
                try:
                    decoded = path_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    log.debug("OSC 7 path bytes not valid UTF-8: %r", path_bytes)
                else:
                    # `unquote` is a no-op on already-unencoded paths
                    # (the shell-init scripts pass $PWD raw — see the
                    # HACK comment in the emitter sources). Future
                    # percent-encoded emitters work transparently here.
                    cwd = urllib.parse.unquote(decoded)
                    # Normalize trailing slash so "/tmp/" and "/tmp" compare
                    # equal in AppController._cwd. Preserve "/" for root.
                    cwd = cwd.rstrip("/") or "/"
                    # A bare "/" almost certainly means a malformed OSC 7
                    # emitter sent file:/// with no path — skip it to avoid
                    # overwriting the anchor's _cwd with root.
                    if cwd != "/":
                        paths.append(cwd)

        # Skip past the terminator and continue scanning for more OSC 7
        # sequences in the same chunk (some shells emit multiple per
        # prompt — e.g. if PROMPT_COMMAND runs more than one hook).
        i = term_idx + term_len

    return bytes(output), paths, b""


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

    # Shell-side cwd announcement (Phase 2.5 deliverable 3). Fired
    # whenever the shell emits an OSC 7 sequence — typically on every
    # prompt redraw after a `cd`, via the `chpwd_functions` (zsh) or
    # `PROMPT_COMMAND` (bash) hook installed by `runtime/symmetria-
    # shell/`. Payload is the local filesystem path extracted from
    # `file://<host>/<path>`. `AppController` routes this into the
    # existing `cwd` capsule pipeline so the file tree + anchor
    # machinery work unchanged.
    osc7_received = Signal(str)

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
        # Carryover buffer for OSC 7 sequences split across reader-loop
        # iterations (an `os.read` chunk boundary can land mid-escape).
        # `_parse_osc7` returns the unterminated trailing partial; we
        # prepend it to the next read. Always owned by the reader thread.
        self._osc_buffer: bytes = b""

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

        Failure semantics: `subprocess.Popen` may raise `OSError` if the
        shell binary is missing or a resource limit is hit. In that case,
        the fds we opened are closed and `_screen`/`_stream` are reset to
        None BEFORE the exception propagates — there's nothing to clean
        up at the caller. The `closed` signal is NOT emitted on this
        path because semantically the shell never lived; `closed` means
        "the shell exited", which presupposes it spawned. AppController
        (PR 4) handles spawn failure as an exception-catch path distinct
        from the closed-signal path.
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
        # the screen. `_AnswerbackHistoryScreen` adds DA1/DSR replies
        # — fzf's `--height` mode (used by `zoxide query -i`) and
        # several TUIs (vim xterm-bg, less, btop) hang without them.
        # Callback is `self.write` so replies share `_stdin_lock`
        # with main-thread keystroke writes; see the subclass docstring.
        self._screen = _AnswerbackHistoryScreen(
            _DEFAULT_COLS,
            _DEFAULT_ROWS,
            history=_SCROLLBACK_LINES,
            ratio=0.5,
            write_callback=self.write,
        )
        self._stream = pyte.ByteStream(self._screen)
        # Force a full-screen repaint on the first frame so the QML
        # paint loop has something to draw before the shell emits its
        # first prompt redraw.
        self._screen.dirty.update(range(_DEFAULT_ROWS))

        # Shell launch with Symmetria-managed OSC 7 auto-injection. The
        # shell type ($SHELL basename) determines the injection mechanism:
        # zsh uses ZDOTDIR redirect to runtime/symmetria-shell/zsh/, bash
        # uses --rcfile pointing at runtime/symmetria-shell/bash/init.sh,
        # other shells (fish, nu, etc.) get a plain login-shell spawn
        # with a log warning — terminal works but the file tree won't
        # follow shell-side `cd` until the user manually installs a
        # chpwd hook. setsid puts the shell into its own session+process
        # group so `killpg` at shutdown reaps any TUIs (vim, htop) the
        # user launched inside.
        shell = os.environ.get("SHELL") or "/bin/bash"
        argv, env_additions = _shell_launch_args(shell)
        env = {
            **os.environ,
            "TERM": "xterm-256color",
            "COLORTERM": "truecolor",
            **env_additions,
        }
        try:
            self._proc = subprocess.Popen(
                argv,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=cwd,
                env=env,
                start_new_session=True,  # puts child in its own session+process group (killpg-reachable)
                # start_new_session calls setsid() but does NOT make the slave
                # PTY the controlling TTY of the new session. Without that,
                # any subprocess that opens /dev/tty (fzf inside a zle widget,
                # vim's xterm-bg detection, etc.) gets ENXIO and either fails
                # outright or silently falls back to stderr. preexec_fn runs
                # in the child after setsid + dup2 but before exec — at that
                # point fd 0 IS the slave PTY (subprocess dup2'd it), so the
                # ioctl makes it the ctty. fork-safety note: preexec_fn is
                # generally unsafe in multi-threaded Python, but this body is
                # a single syscall with no Python state / locks — safe.
                preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0),
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
        if not data:
            return
        if self._master_fd is None:
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

        # Capture to locals: (a) avoids repeated attr lookup in the hot path,
        # (b) the reader retains these references after stop() clears the
        # instance attrs -- intentional, avoids use-after-free from the
        # reader's own perspective while it finishes draining.
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
                    # Drain the wake-up byte. Multiple concurrent stop()
                    # calls (race between natural shell exit and explicit
                    # stop()) may each write one byte -- drain all pending.
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

                    # Extract OSC 7 sequences before pyte sees them.
                    # Phase 2.5 deliverable 3 — shell-side `chpwd` hook
                    # emits `ESC ] 7 ; file://host/cwd ESC \\` on every
                    # prompt; we route the path to AppController via
                    # `osc7_received` and strip the bytes so pyte
                    # doesn't render them as garbage. `_parse_osc7` is
                    # buffer-aware: a sequence split across an
                    # `os.read` chunk boundary stitches on the next
                    # iteration via `_osc_buffer`.
                    cleaned, paths, self._osc_buffer = _parse_osc7(
                        self._osc_buffer, data
                    )

                    # GC suspended across osc7_received emit + feed +
                    # dirty-extract + screen_dirty emit. See gotcha #10
                    # + module docstring. `osc7_received.emit` is inside
                    # the window because QueuedConnection arg-copying
                    # allocates a Python str wrapper on the worker thread
                    # — the same GC race class as stream.feed. Conditional
                    # disable/enable matches NvimBackend._on_notification
                    # and session_host.py — do NOT make this unconditional
                    # (an outer gc.disable caller would have its GC
                    # wrongly re-enabled by the finally).
                    gc_was_enabled = gc.isenabled()
                    if gc_was_enabled:
                        gc.disable()
                    try:
                        for path in paths:
                            self.osc7_received.emit(path)
                        stream.feed(cleaned)
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
