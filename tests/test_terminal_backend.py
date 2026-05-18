"""Structural tests for the Phase 2.5 terminal-backend skeleton.

PR 1 only covers the skeleton: signal declarations, NotImplementedError
method stubs, and the pyte import contract. PR 2 fleshes out the actual
PTY + reader-thread implementation and adds lifecycle/round-trip tests.

The tests here are deliberately structural — they validate the public
surface that downstream components (TerminalView in PR 3, AppController
in PR 4) will bind against, so a refactor that accidentally drops a
signal or changes a method signature is caught at lint time rather than
at integration-test time.
"""

from __future__ import annotations

import inspect
import threading

import pytest

from symmetria_ide.terminal_backend import TerminalBackend


# ---------------------------------------------------------------------------
# pyte contract — the dependency is the foundation of every downstream PR;
# pin its surface here so a future pyte release that shifts the `Char`
# namedtuple shape is caught the moment we bump.
# ---------------------------------------------------------------------------


def test_pyte_imports_and_screen_shape() -> None:
    """pyte resolves and its Screen / Char surface matches our assumptions.

    PR 2's reader thread feeds bytes into a `pyte.ByteStream` that
    mutates a `pyte.HistoryScreen`. PR 3's paint loop reads
    `screen.buffer[y][x]` and pulls `.data` (the grapheme) and the
    fg/bg/attr fields off each `Char`. If pyte ever drops or renames
    these fields, every downstream paint-side assumption breaks —
    catching it here means the failure mode is "test fails on import"
    rather than "terminal paints blank silently".
    """
    import pyte

    screen = pyte.Screen(80, 24)
    cell = screen.buffer[0][0]
    # All 9 Char namedtuple fields as of pyte 0.8.x. Pinned in full so any drop or rename
    # surfaces here rather than as a silent paint regression. `reverse` is especially
    # critical — it drives reverse-video (selection highlight) in the terminal renderer.
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
    """The two pyte classes the backend instantiates in PR 2 are available."""
    import pyte

    screen = pyte.HistoryScreen(80, 24, history=1000, ratio=0.5)
    stream = pyte.ByteStream(screen)
    # Smoke test: feeding ASCII updates the screen.
    stream.feed(b"hello")
    assert screen.buffer[0][0].data == "h"


# ---------------------------------------------------------------------------
# TerminalBackend skeleton — signal surface + lifecycle stubs
# ---------------------------------------------------------------------------


@pytest.fixture
def backend():
    """Bare backend, no parent QObject — skeleton has no GUI dependencies."""
    return TerminalBackend()


def test_skeleton_signals_declared(backend):
    """The three v1 signals exist on the class.

    These are the ONLY signals declared in PR 1. `osc_received` (deliverable
    3) and `title_changed` (no v1 consumer) are intentionally absent — if
    they appear here later, it should be because an actual consumer was
    added in the same PR, not because of speculative scaffolding.
    """
    assert hasattr(backend, "screen_dirty")
    assert hasattr(backend, "screen_resized")
    assert hasattr(backend, "closed")


def test_stop_event_exposed_and_unset(backend):
    """`stop_event` mirrors `NvimBackend.stop_event` — a `threading.Event`
    that's initially unset, set only at teardown or worker-exit."""
    assert isinstance(backend.stop_event, threading.Event)
    assert backend.stop_event.is_set() is False


def test_lifecycle_methods_are_stubs(backend):
    """PR 1 leaves `start`/`stop`/`write`/`resize` as NotImplementedError
    stubs. The signatures are locked here so PR 2 fleshes out the body
    without renaming the public surface."""
    with pytest.raises(NotImplementedError, match="PR 2"):
        backend.start("/tmp")
    with pytest.raises(NotImplementedError, match="PR 2"):
        backend.stop()
    with pytest.raises(NotImplementedError, match="PR 2"):
        backend.write(b"hello")
    with pytest.raises(NotImplementedError, match="PR 2"):
        backend.resize(80, 24)


def test_skeleton_carries_locks_for_pr2(backend):
    """PR 2's PTY-write path requires `_stdin_lock`; the reader thread
    requires `_stop_event`. Both are pre-allocated in `__init__` so PR 2
    only adds the thread + fd lifecycle, not the lock primitives.

    A regression that drops these from `__init__` would surface as a
    confusing AttributeError mid-implementation in PR 2 — catching it
    here means the foundation stays stable across PRs.
    """
    assert isinstance(backend._stdin_lock, threading.Lock)
    assert isinstance(backend._stop_event, threading.Event)


def test_module_docstring_references_invariants():
    """The module docstring is the persistence layer for the gotcha
    discipline (#10 GC suspension, §1 P0 daemon+Event, §4 P2
    QueuedConnection). It's load-bearing context for whoever picks
    up PR 2 — assert it didn't get gutted by a "clean up comments" pass.
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
