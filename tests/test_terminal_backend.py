"""Tests for the Phase 2.5 terminal backend.

Mix of structural assertions (signal surface, gc-suspension discipline,
killpg shutdown, frozenset emit contract, OSC 7 intercept ordering) and
integration-style lifecycle tests that spawn a real `/bin/sh` subprocess
under a real PTY.

Phase 2.5 deliverable 3 additions: `test_reader_loop_intercepts_osc7_before_pyte`
pins that `_parse_osc7` runs before `stream.feed`; `test_osc_buffer_pre_allocated`
pins that `_osc_buffer` is initialised in `__init__`; and
`test_reader_loop_emits_osc7_inside_gc_window` pins that `osc7_received.emit`
is inside the `gc.disable()` window (gotcha #10 extension).

Lifecycle tests pin `SHELL=/bin/sh` via `monkeypatch.setenv` so they're
hermetic against whatever interactive shell the developer's environment
prefers — POSIX sh starts fast and doesn't source heavyweight rc files.
Without that pin, a `-l zsh` invocation can spend 100-500ms sourcing
.zshrc + plugin managers, which makes the test suite slow and flaky
across machines.
"""

from __future__ import annotations

import inspect
import struct
import termios
import threading
import time
from unittest import mock

import pytest

from symmetria_ide.terminal_backend import (
    _DECCKM_MODE,
    _DEFAULT_COLS,
    _DEFAULT_ROWS,
    TerminalBackend,
    _AnswerbackHistoryScreen,
    _TerminalScreen,
)


# ---------------------------------------------------------------------------
# pyte contract — the dependency is the foundation of every downstream PR;
# pin its surface here so a future pyte release that shifts the `Char`
# namedtuple shape is caught the moment we bump.
# ---------------------------------------------------------------------------


def test_pyte_imports_and_screen_shape() -> None:
    """pyte resolves and its Screen / Char surface matches our assumptions.

    The reader thread feeds bytes into a `pyte.ByteStream` that mutates
    a `pyte.HistoryScreen`. The paint loop (PR 3) will read
    `screen.buffer[y][x]` and pull `.data` (the grapheme) plus the
    fg/bg/attr fields off each `Char`. If pyte ever drops or renames
    these fields, every downstream paint-side assumption breaks —
    catching it here means the failure mode is "test fails on import"
    rather than "terminal paints blank silently".
    """
    import pyte

    screen = pyte.Screen(80, 24)
    cell = screen.buffer[0][0]
    # All 9 Char namedtuple fields as of pyte 0.8.x. Pinned in full so any
    # drop or rename surfaces here rather than as a silent paint regression.
    # `reverse` is especially critical — it drives reverse-video (selection
    # highlight) in the terminal renderer.
    for field in (
        "data",
        "fg",
        "bg",
        "bold",
        "italics",
        "underscore",
        "reverse",
        "strikethrough",
        "blink",
    ):
        assert hasattr(cell, field), f"pyte.Char missing field: {field}"


def test_pyte_history_screen_and_byte_stream_constructible() -> None:
    """The two pyte classes the backend instantiates are available."""
    import pyte

    screen = pyte.HistoryScreen(80, 24, history=1000, ratio=0.5)
    stream = pyte.ByteStream(screen)
    # Smoke test: feeding ASCII updates the screen.
    stream.feed(b"hello")
    assert screen.buffer[0][0].data == "h"


# ---------------------------------------------------------------------------
# TerminalBackend — public surface
# ---------------------------------------------------------------------------


@pytest.fixture
def backend():
    """Bare backend, no parent QObject. Caller is responsible for stop()
    if they call start()."""
    return TerminalBackend()


def test_signals_declared(backend):
    """The four v1 signals exist on the class.

    `osc7_received` was added in Phase 2.5 deliverable 3 PR 2 — paths
    extracted from shell-emitted OSC 7 sequences are routed through
    this signal into AppController's cwd capsule pipeline. `title_changed`
    (no v1 consumer) remains intentionally absent.
    """
    assert hasattr(backend, "screen_dirty")
    assert hasattr(backend, "screen_resized")
    assert hasattr(backend, "closed")
    assert hasattr(backend, "osc7_received")


def test_stop_event_exposed_and_unset(backend):
    """`stop_event` mirrors `NvimBackend.stop_event` — a `threading.Event`
    that's initially unset, set only at teardown or worker-exit."""
    assert isinstance(backend.stop_event, threading.Event)
    assert backend.stop_event.is_set() is False


# ---------------------------------------------------------------------------
# DA1 / DSR answerback — `fzf --height` (used by `zoxide query -i`) hangs
# indefinitely without these replies, even when /dev/tty is otherwise fine
# (TIOCSCTTY ioctl in place). Stock pyte builds the right reply strings
# but hands them to a no-op `write_process_input`; our subclass routes
# them back to the master fd via a callback. Empirically diagnosed via
# tools/diag_zoxide_query.py — see post-TIOCSCTTY active memory note.
# ---------------------------------------------------------------------------


def test_answerback_dsr_cursor_position_reply() -> None:
    """A DSR-cursor (`ESC [ 6 n`) query routes the reply through the callback.

    The reply MUST match `ESC [ <row> ; <col> R` in 1-indexed coords
    (pyte's stock contract). At cursor (0, 0) after a fresh screen,
    the reply is `\\x1b[1;1R`. If a future pyte release changes
    `report_device_status`'s 1-indexing, this test catches it.
    """
    import pyte

    received: list[bytes] = []
    screen = _AnswerbackHistoryScreen(
        80, 24, history=100, ratio=0.5, write_callback=received.append
    )
    stream = pyte.ByteStream(screen)
    stream.feed(b"\x1b[6n")
    assert received == [b"\x1b[1;1R"]


def test_answerback_da1_primary_attributes_reply() -> None:
    """A DA1 (`ESC [ c`) query routes back the VT102 identity `ESC [ ? 6 c`.

    `zoxide query -i`'s fzf doesn't currently issue DA1, but several
    other TUIs do (vim's xterm-bg-color detection, less). Pinned so a
    pyte regression that drops the DA1 path surfaces here.
    """
    import pyte

    received: list[bytes] = []
    screen = _AnswerbackHistoryScreen(
        80, 24, history=100, ratio=0.5, write_callback=received.append
    )
    stream = pyte.ByteStream(screen)
    stream.feed(b"\x1b[c")
    assert received == [b"\x1b[?6c"]


def test_answerback_noop_when_callback_missing() -> None:
    """With no callback wired, the screen silently drops DSR replies.

    Defensive default — keeps the subclass safely constructible in
    unit tests (or hypothetical future contexts that want pyte without
    a backing fd) without leaking writes to stdout/stderr."""
    import pyte

    screen = _AnswerbackHistoryScreen(80, 24, history=100, ratio=0.5)
    stream = pyte.ByteStream(screen)
    stream.feed(b"\x1b[6n")  # must NOT raise


def test_locks_pre_allocated(backend):
    """`_stdin_lock` (GUI-thread `write()` serialisation) and
    `_stop_event` (cooperative shutdown signal) are constructed in
    `__init__` so both are ready BEFORE any reader thread sees them.

    A regression that lazy-allocates either could race the reader's
    first iteration — the contract is "lifecycle primitives exist
    before `start()` could possibly run".
    """
    assert isinstance(backend._stdin_lock, threading.Lock)
    assert isinstance(backend._stop_event, threading.Event)


def test_module_docstring_references_invariants():
    """The module docstring is the persistence layer for the four
    invariants (gotcha #10 GC suspension, §1 P0 daemon+Event,
    §4 P2 QueuedConnection, frozenset payload contract). It's
    load-bearing context for whoever picks up the next PR — assert it
    didn't get gutted by a "clean up comments" pass.
    """
    import symmetria_ide.terminal_backend as module

    doc = inspect.getdoc(module) or ""
    assert "gotcha #10" in doc, "GC discipline reference missing from module docstring"
    assert "§1 P0" in doc, (
        "daemon+Event discipline reference missing from module docstring"
    )
    assert "§4 P2" in doc, (
        "QueuedConnection discipline reference missing from module docstring"
    )
    assert "frozenset" in doc, (
        "frozenset payload contract reference missing from module docstring"
    )


# ---------------------------------------------------------------------------
# Structural assertions — pin the discipline that's hard to test directly
# without a Qt event loop, by reading the source.
# ---------------------------------------------------------------------------


def test_reader_loop_uses_gc_suspension():
    """The reader loop's `feed` + emit window MUST be inside a
    `gc.disable()` / `gc.enable()` window. CLAUDE.md gotcha #10's
    decisive fix: pyte allocates per-cell on every update, and the
    incremental cyclic GC racing `QSGRenderThread` mid-paint is the
    documented SEGV class. Removing the guard reintroduces the crash.
    """
    src = inspect.getsource(TerminalBackend._run_reader_loop)
    assert "gc.disable" in src, "gotcha #10 GC suspension missing in reader loop"
    assert "gc.enable" in src, "gotcha #10 GC re-enable missing in reader loop"


def test_reader_loop_emits_frozenset():
    """`screen_dirty.emit(...)` MUST pass a frozenset, not a raw set.
    Project-standards §4 P1: signal payloads must be
    trivially-serialisable, and Qt's QueuedConnection passes set
    objects by reference (not copied) — emitting a mutable set leaves
    the same object live in two threads' GC graphs, expanding the
    gotcha #10 race window. PySide6 also enforces this at emit time
    because the signal is declared `Signal(frozenset)`.
    """
    src = inspect.getsource(TerminalBackend._run_reader_loop)
    assert "frozenset(" in src, "screen_dirty payload must be a frozenset (§4 P1)"


def test_stop_uses_killpg():
    """`stop()` MUST use `killpg` (process-group kill), not just
    `proc.terminate()` or `proc.kill()`. The shell is a session leader
    via `preexec_fn=os.setsid` so any TUIs (vim, htop, fzf) the user
    spawned inside become its children. Killing the parent alone
    orphans those children, leaking PIDs and possibly fds. killpg
    reaps the whole process group.
    """
    src = inspect.getsource(TerminalBackend.stop)
    assert "killpg" in src, "process-group kill missing — children would orphan"


def test_reader_thread_uses_self_pipe_wakeup():
    """The reader's `select()` MUST observe a self-pipe alongside
    `_master_fd` so `stop()` can wake it cooperatively. Without the
    self-pipe, a blocked `select` only returns when the shell emits
    output — `stop()` would hang for arbitrary time on an idle shell.
    """
    src = inspect.getsource(TerminalBackend._run_reader_loop)
    assert "self_pipe_r" in src, (
        "self-pipe wakeup missing — stop() would block on idle shell"
    )


def test_reader_loop_intercepts_osc7_before_pyte():
    """The reader loop MUST run `_parse_osc7` BEFORE `stream.feed` so
    OSC 7 bytes are stripped before pyte sees them. Otherwise pyte
    would render the escape sequence as a control-char glob in the
    grid. Also asserts the buffer is carried across iterations via
    `self._osc_buffer` so fragmented sequences stitch correctly.
    """
    src = inspect.getsource(TerminalBackend._run_reader_loop)
    parse_idx = src.find("_parse_osc7(")
    feed_idx = src.find("stream.feed(")
    assert parse_idx >= 0, "reader loop must call _parse_osc7"
    assert feed_idx >= 0, "reader loop must still call stream.feed"
    assert parse_idx < feed_idx, (
        "_parse_osc7 must run BEFORE stream.feed — otherwise pyte renders OSC 7 as garbage"
    )
    assert "self._osc_buffer" in src, (
        "reader loop must carry _osc_buffer across iterations for fragmented sequences"
    )
    assert "osc7_received.emit" in src, (
        "reader loop must emit osc7_received for each extracted path"
    )


def test_osc_buffer_pre_allocated(backend):
    """`_osc_buffer` is the partial-OSC carryover. Must be initialised
    in `__init__` (not lazily) so the reader thread's first iteration
    sees a bytes-typed attr instead of AttributeError or None."""
    assert isinstance(backend._osc_buffer, bytes)
    assert backend._osc_buffer == b""


def test_reader_loop_emits_osc7_inside_gc_window():
    """GC must be suspended BEFORE `osc7_received.emit` fires — the same
    window that covers `stream.feed`. `osc7_received.emit(path)` causes
    Qt's QueuedConnection machinery to allocate a Python str wrapper and
    an event object on the worker thread; Python 3.14's cyclic GC can
    race `QSGRenderThread` during that allocation (same SEGV class as
    gotcha #10). Pin the call order structurally so a future refactor
    that moves the emit outside the window surfaces here immediately.

    Searches for the CALL `self.osc7_received.emit(` (not the comment
    that mentions it) so the assertion is insensitive to comment text.
    """
    src = inspect.getsource(TerminalBackend._run_reader_loop)
    # Use the GC-preamble line as the "window opened" marker rather than
    # gc.disable() itself — the preamble `gc_was_enabled = gc.isenabled()`
    # is the first thing the window does, and it appears before the actual
    # `gc.disable()` call that follows the if-check.
    gc_preamble_idx = src.find("gc_was_enabled = gc.isenabled()")
    # Search for the ACTUAL CALL (not comment mentions of it).
    emit_idx = src.find("self.osc7_received.emit(")
    feed_idx = src.find("stream.feed(")
    assert gc_preamble_idx >= 0, "gc_was_enabled preamble missing from reader loop"
    assert emit_idx >= 0, "self.osc7_received.emit( call missing from reader loop"
    assert feed_idx >= 0, "stream.feed( call missing from reader loop"
    assert gc_preamble_idx < emit_idx, (
        "osc7_received.emit must be INSIDE the gc.disable() window "
        "(gotcha #10: QueuedConnection arg allocation races QSGRenderThread)"
    )
    assert gc_preamble_idx < feed_idx, (
        "stream.feed must also be inside the gc.disable() window"
    )


# ---------------------------------------------------------------------------
# Lifecycle — spawn a real /bin/sh under a real PTY, then tear it down.
# ---------------------------------------------------------------------------


@pytest.fixture
def started_backend(monkeypatch):
    """Backend with a live `/bin/sh` shell. Cleans up on test exit.

    Forces SHELL=/bin/sh because POSIX sh starts fast (no rc-files
    sourced under `-l` worth speaking of) and is deterministic across
    machines. Without this monkeypatch, an interactive zsh with plugin
    managers could add 100-500ms per test.
    """
    monkeypatch.setenv("SHELL", "/bin/sh")
    b = TerminalBackend()
    b.start(cwd="/tmp")
    try:
        yield b
    finally:
        b.stop()


def test_start_stop_lifecycle(monkeypatch):
    """start() spawns a real shell and populates lifecycle attrs;
    stop() reaps everything cleanly."""
    monkeypatch.setenv("SHELL", "/bin/sh")
    b = TerminalBackend()

    b.start(cwd="/tmp")
    assert b._proc is not None
    assert b._proc.poll() is None  # still alive
    assert b._master_fd is not None
    assert b._worker is not None
    assert b._worker.is_alive()
    assert b._screen is not None
    assert b._stream is not None

    b.stop()
    assert b._proc is None
    assert b._worker is None
    assert b._master_fd is None
    assert b._screen is None
    assert b._stop_event.is_set()


def test_start_is_idempotent(started_backend):
    """A second start() on a live backend is a no-op (does NOT spawn
    a second shell)."""
    first_proc = started_backend._proc
    first_master_fd = started_backend._master_fd
    started_backend.start(cwd="/tmp")
    assert started_backend._proc is first_proc
    assert started_backend._master_fd is first_master_fd


def test_stop_is_idempotent_when_unstarted():
    """stop() on an unstarted backend must not raise, and a second
    stop() after a real one must also be a no-op."""
    b = TerminalBackend()
    b.stop()  # never started
    b.stop()  # double-stop after no-op


def test_reader_thread_is_daemon(started_backend):
    """§1 P0: long-running threads must be daemon=True. Mirrors
    NvimBackend's worker discipline."""
    assert started_backend._worker is not None
    assert started_backend._worker.daemon is True
    assert started_backend._worker.is_alive()


def test_write_round_trip(started_backend):
    """write() forwards bytes to the shell; the shell's response
    flows back through the reader and lands in pyte's buffer."""
    sentinel = "symmetria_sentinel_42"
    started_backend.write(f"echo {sentinel}\r".encode())

    deadline = time.monotonic() + 3.0
    found = False
    while time.monotonic() < deadline and not found:
        # pyte.HistoryScreen.buffer is a dict-of-dict {row: {col: Char}}
        buf = started_backend._screen.buffer
        for row_idx in range(_DEFAULT_ROWS):
            row = buf[row_idx]
            row_text = "".join(row[col].data for col in row)
            if sentinel in row_text:
                found = True
                break
        if not found:
            time.sleep(0.05)

    assert found, f"echo output {sentinel!r} did not appear in pyte buffer within 3s"


def test_resize_pushes_winch(started_backend):
    """resize() updates pyte's dimensions AND pushes TIOCSWINSZ to the
    slave PTY so the shell's $LINES / $COLUMNS reflow. Mocking
    fcntl.ioctl lets us assert the exact call without depending on
    the shell's response."""
    new_cols, new_rows = 100, 30

    with mock.patch("symmetria_ide.terminal_backend.fcntl.ioctl") as mock_ioctl:
        started_backend.resize(new_cols, new_rows)

    # Exactly one ioctl call from resize() — the initial TIOCSWINSZ in
    # start() ran before this patch took effect.
    mock_ioctl.assert_called_once()
    fd_arg, op_arg, payload = mock_ioctl.call_args[0]
    assert fd_arg == started_backend._master_fd
    assert op_arg == termios.TIOCSWINSZ
    # struct format is (rows, cols, xpixel, ypixel).
    unpacked = struct.unpack("HHHH", payload)
    assert unpacked == (new_rows, new_cols, 0, 0)

    # pyte's internal dims also updated.
    assert started_backend._screen.lines == new_rows
    assert started_backend._screen.columns == new_cols


def test_resize_emits_screen_resized(started_backend):
    """resize() emits screen_resized(cols, rows) AFTER both pyte and
    the kernel are in sync. Hooking a Python slot via direct connect
    captures the payload without needing a Qt event loop."""
    captured: list[tuple[int, int]] = []
    started_backend.screen_resized.connect(
        lambda cols, rows: captured.append((cols, rows))
    )

    started_backend.resize(120, 40)
    assert captured == [(120, 40)]


def test_resize_rejects_invalid_dims(started_backend):
    """Defensive: zero or negative dims are a programming bug, not a
    crash trigger. resize() drops them silently."""
    # Just verifying it doesn't raise — pyte.resize would ValueError
    # on rows=0 if it ran.
    started_backend.resize(0, 24)
    started_backend.resize(80, 0)
    started_backend.resize(-1, -1)


def test_write_before_start_is_noop():
    """write() on an unstarted backend must not raise — covers the
    race where focus/key events arrive before start() completes."""
    b = TerminalBackend()
    b.write(b"hello")  # must not raise


def test_default_screen_dims_constants():
    """The 80x24 default is the kernel / xterm canonical — pinned
    here so a future change is visible at review time, since downstream
    PRs (TerminalView's first-paint, AppController's pre-warm
    expectations) implicitly assume the default."""
    assert _DEFAULT_COLS == 80
    assert _DEFAULT_ROWS == 24


# ---------------------------------------------------------------------------
# Alt-screen buffer (_TerminalScreen) — hosting full-screen TUIs (nvim,
# lazygit, htop). Pure tests via ByteStream, like the answerback tests.
# ---------------------------------------------------------------------------


def _alt_screen(cols: int = 20, rows: int = 5) -> tuple:
    """Build a (_TerminalScreen, ByteStream) pair for alt-screen tests."""
    import pyte

    screen = _TerminalScreen(cols, rows, history=100, ratio=0.5)
    return screen, pyte.ByteStream(screen)


def test_alt_screen_swaps_and_restores_buffer():
    """Entering the alt screen (DECSET 1049) hides the primary buffer and
    presents a fresh one; leaving (DECRST 1049) restores the primary."""
    screen, stream = _alt_screen()
    stream.feed(b"PRIMARY LINE")
    assert screen.display[0].strip() == "PRIMARY LINE"

    stream.feed(b"\x1b[?1049h")  # enter alt
    assert screen._in_alt_screen is True
    assert screen.display[0].strip() == ""  # fresh, empty
    stream.feed(b"ALT CONTENT")
    assert screen.display[0].strip() == "ALT CONTENT"

    stream.feed(b"\x1b[?1049l")  # leave alt
    assert screen._in_alt_screen is False
    assert screen.display[0].strip() == "PRIMARY LINE"  # primary intact


def test_alt_screen_saves_and_restores_cursor_mode_1049():
    """Mode 1049 saves the cursor on entry and restores it on exit."""
    screen, stream = _alt_screen()
    stream.feed(b"\x1b[3;5H")  # cursor to row 3 col 5 (0-based 2,4)
    before = (screen.cursor.x, screen.cursor.y)

    stream.feed(b"\x1b[?1049h")
    stream.feed(b"\x1b[1;1H")  # move cursor inside alt screen
    stream.feed(b"\x1b[?1049l")
    assert (screen.cursor.x, screen.cursor.y) == before


def test_alt_screen_legacy_mode_47_does_not_save_cursor():
    """Legacy mode 47 swaps the buffer but does NOT save/restore the
    cursor (only 1049/1048 do). Also, mode 47 must NOT home the cursor
    on entry — it just switches the buffer; the cursor stays put."""
    screen, stream = _alt_screen()
    stream.feed(b"\x1b[3;5H")  # cursor to row 3 col 5 (0-based: 2, 4)
    cursor_before_entry = (screen.cursor.x, screen.cursor.y)
    stream.feed(b"\x1b[?47h")
    assert screen._in_alt_screen is True
    # Mode 47 must NOT home the cursor on entry (unlike mode 1049).
    assert (screen.cursor.x, screen.cursor.y) == cursor_before_entry
    stream.feed(b"\x1b[1;1H")  # move cursor inside alt
    moved = (screen.cursor.x, screen.cursor.y)
    stream.feed(b"\x1b[?47l")
    # Cursor NOT restored — stays where it was left in the alt screen.
    assert (screen.cursor.x, screen.cursor.y) == moved


def test_alt_screen_no_scrollback_pollution():
    """Scrolling inside the alt screen must NOT push lines into the
    history deques — otherwise quitting nvim leaves its content in the
    shell's scrollback."""
    screen, stream = _alt_screen()
    top_before = len(screen.history.top)
    stream.feed(b"\x1b[?1049h")
    for _ in range(30):  # force many scrolls inside alt
        stream.feed(b"line\r\n")
    assert len(screen.history.top) == top_before  # no pollution
    stream.feed(b"\x1b[?1049l")


def test_alt_screen_resize_clips_saved_primary_columns():
    """Resizing while in the alt screen clips the saved primary buffer's
    columns so a later restore doesn't render stale over-wide cells."""
    screen, stream = _alt_screen(cols=20, rows=5)
    stream.feed(b"X" * 20)  # fill primary row 0 to full width
    stream.feed(b"\x1b[?1049h")
    screen.resize(5, 10)  # (lines, columns) — narrow to 10 cols
    # Saved primary buffer's row 0 must have no cells at col >= 10.
    assert screen._primary_buffer is not None
    over_wide = [c for c in screen._primary_buffer[0] if c >= 10]
    assert over_wide == []


def test_decckm_mode_tracked_in_screen_mode():
    """DECSET 1 (DECCKM) lands in screen.mode as the shifted value the
    backend's reader mirrors into application_cursor_keys."""
    screen, stream = _alt_screen()
    assert _DECCKM_MODE not in screen.mode
    stream.feed(b"\x1b[?1h")
    assert _DECCKM_MODE in screen.mode
    stream.feed(b"\x1b[?1l")
    assert _DECCKM_MODE not in screen.mode


def test_application_cursor_keys_defaults_false():
    """Before any feed, the backend reports normal cursor-key mode."""
    b = TerminalBackend()
    assert b.application_cursor_keys is False


def test_start_honors_explicit_argv(monkeypatch):
    """start(argv=...) runs the given program, NOT $SHELL — proven by
    pointing SHELL at a nonexistent path: if argv weren't honored the
    spawn would raise OSError trying to exec the bogus shell."""
    monkeypatch.setenv("SHELL", "/nonexistent/shell-should-not-run")
    b = TerminalBackend()
    # /bin/cat with no args reads its stdin (the PTY) forever → stays alive.
    b.start(cwd="/tmp", argv=["/bin/cat"])
    try:
        assert b._proc is not None
        assert b._proc.poll() is None  # alive → argv was used, not $SHELL
    finally:
        b.stop()
