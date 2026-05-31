"""Minimap content model — receives buffer snapshots/patches from nvim.

Phase 1 of the editor minimap (docs/minimap-prd.md). Ingests
`"minimap"` rpcnotify events emitted by
`runtime/lua/orchestrator/minimap.lua`; exposes the current buffer's
line contents + count so the painter (Phase 2 onward) can render
without round-tripping to nvim.

Wire shape (kept in sync with `minimap.lua`):

  snapshot envelope:
    { op="snapshot", bufnr, line_count, lines: list[str] }

  patch envelope (reserved for Phase 1.5+; the model handles both ops
  from day one so the Lua side can flip on patches without a model
  change):
    { op="patch", bufnr, line_count, first, last, lines: list[str] }
    — replaces `_lines[first:last]` with `lines`; `last` is exclusive.

Threading:

The model is GUI-thread-owned. The pynvim worker emits `minimap_event`
to `MinimapModel.apply` via an explicit `Qt.QueuedConnection`
established in `AppController.__init__` (§4 P2). Calling `apply()`
directly from a non-GUI thread would race the painter — never do that.

Properties exposed to QML:
  - `lineCount` (int, notify=lineCountChanged) — total buffer lines.
    Phase 2's painter binds this to drive its per-line iteration.

Methods callable from the painter (Python-side only — not QML-visible
to avoid the per-call signal/slot marshalling cost):
  - `line_count() -> int` — same value as the property, kept as a
    method for hot-path readers that want zero property overhead.
  - `line_at(i: int) -> str` — single-line accessor with bounds clamping;
    returns "" for out-of-range to keep the painter loop crash-free
    even under stale viewport bookkeeping.

Resilience:

`apply()` wraps the mutation path in try/except — UTF-8 decoding
errors, malformed payloads, or shape regressions in future nvim
versions log + drop rather than crash the GUI thread. Same defensive
posture the capsule / whichkey routes use.
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import Property, QObject, Signal, Slot


log = logging.getLogger(__name__)


# Indent-level palette has four rungs (0..3); deeper nesting clamps to
# `_MAX_INDENT_LEVEL` so a 12-space-indented line doesn't paint with a
# colour the palette doesn't define. The four-level scale matches
# `Theme.color.minimap.indent.level0..level3` — see Theme.qml for the
# rationale on why four rungs is enough.
_MAX_INDENT_LEVEL = 3

# Indent step in COLUMNS. Two leading spaces == one indent level; deeper
# code maps proportionally. Tab characters count as one indent level
# regardless of effective tab width (the painter doesn't know about
# `tabstop`, and at minimap scale the visual difference between 4-space
# and 8-space tabs is invisible). This is a deliberate Phase 2
# simplification — if a user's tab width is very different from 2, the
# silhouette may read slightly differently from the editor's indent
# guides, but the document-shape signal is preserved.
_INDENT_COLUMNS_PER_LEVEL = 2


def _compute_indent_level(line: str) -> int:
    """Map a buffer line to its 0..`_MAX_INDENT_LEVEL` indent rung.

    Empty lines are level 0 (no indent, painter renders them at the
    leftmost rung — visually blends into a gap in the silhouette).
    Mixed-leading-whitespace lines count each tab as one level and
    every `_INDENT_COLUMNS_PER_LEVEL` spaces as one level; pathological
    leading-whitespace patterns clamp to the max rung rather than
    overflowing the palette.

    Pure-function so tests can exercise it without standing up a model.
    Hot path — called once per line per apply(), never inside paint();
    keeps allocations to one int per call (str.lstrip would allocate a
    fresh str per call, which is why this loop walks chars instead).
    """
    columns = 0
    for ch in line:
        if ch == " ":
            columns += 1
        elif ch == "\t":
            # Tab counts as a full level boundary regardless of tab
            # width — see module-level comment for the rationale.
            columns += _INDENT_COLUMNS_PER_LEVEL
        else:
            break
    level = columns // _INDENT_COLUMNS_PER_LEVEL
    if level > _MAX_INDENT_LEVEL:
        return _MAX_INDENT_LEVEL
    return level


class MinimapModel(QObject):
    """Holds the current buffer's line contents for the minimap painter.

    Owned by `AppController`. Receives `apply(payload)` calls on the
    GUI thread (via a queued connection from `NvimBackend.minimap_event`)
    and emits `linesChanged(int first, int last)` whenever content
    mutates. Phase 2's painter connects to `linesChanged` to know which
    rows need repainting; for now Phase 0/1 consumers just listen for
    the signal to call `update()`.
    """

    # Emitted with the (inclusive, exclusive) row range that was
    # mutated. For a snapshot replacing the full buffer the range is
    # (0, new_line_count). For a patch it's the patch's own (first, last).
    # An empty buffer mutation still emits (0, 0) so listeners can
    # distinguish "no change" (no signal) from "now empty" (signal with
    # range zero).
    linesChanged = Signal(int, int)

    # Notify signal for the QML-visible `lineCount` property. Separate
    # from linesChanged because Qt requires a parameterless notify
    # signal for property bindings (no overload selection at the QML
    # binding layer).
    lineCountChanged = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        # Backing store. Always a list — never None — so callers don't
        # need to None-check before iterating.
        self._lines: list[str] = []
        # Cached count so `line_count()` and the QML property don't
        # both pay `len(self._lines)` on every read; updated only
        # inside `apply()`, never directly by callers.
        self._line_count: int = 0
        # Tracks the buffer we last applied an envelope for. Phase 2+
        # painters compare `bufnr()` to invalidate per-buffer caches
        # (e.g. when the user switches buffers); readable via the
        # `bufnr()` accessor without needing a separate signal.
        self._bufnr: int = -1
        # Cached indent levels parallel to `_lines` — Phase 2 of
        # docs/minimap-prd.md. PRD §6 R2.2 mandates we precompute
        # indent here rather than running `str.lstrip()` per line
        # inside paint(): lstrip allocates a fresh str per row, and
        # at 50k+ lines per repaint that allocation churn would
        # blow past the gotcha #10 paint-hot-path budget. Recomputed
        # only inside `apply()` (snapshot replaces the whole array;
        # patch updates the spliced range only — same range the
        # `_lines` splice touches). The painter reads via
        # `indent_level(i)` with the same bounds-clamping discipline
        # `line_at()` uses, so transient stale-bookkeeping windows
        # in the painter's iteration don't crash.
        self._indent_levels: list[int] = []

    # --- QML-visible properties ---------------------------------------
    #
    # Read-only from QML — content arrives via `apply()` from the nvim
    # backend, never set from the QML side. Plain `@Property` is fine
    # because there's no setter to trip pyright's reportRedeclaration.

    @Property(int, notify=lineCountChanged)
    def lineCount(self) -> int:  # noqa: N802 — Qt naming convention
        return self._line_count

    # --- Painter-side accessors (Python only) -------------------------

    def line_count(self) -> int:
        """Plain accessor for the painter — same value as the
        `lineCount` property without the QML marshalling overhead."""
        return self._line_count

    def bufnr(self) -> int:
        """Current buffer number. -1 when no snapshot has been applied.
        Phase 2+ painters compare this to invalidate per-buffer caches
        (e.g. indent-level arrays) when the user switches buffers."""
        return self._bufnr

    def line_at(self, index: int) -> str:
        """Bounded line accessor. Out-of-range returns "" so the
        painter loop can iterate past `line_count` once without
        crashing during a transient stale-bookkeeping window
        (Phase 2's run-coalesced painter occasionally reads one
        past the end while computing run boundaries).
        """
        if 0 <= index < self._line_count:
            return self._lines[index]
        return ""

    def indent_level(self, index: int) -> int:
        """Cached indent level (0..`_MAX_INDENT_LEVEL`) for line `index`.

        Same bounds-clamping contract as `line_at()` — out-of-range
        returns 0 (= no indent / blank-line treatment), keeping the
        Phase 2 painter's per-line iteration crash-free. Reads from
        the precomputed `_indent_levels` array, NOT a fresh
        `_compute_indent_level(line_at(i))` call — that would
        re-allocate the str for every painted row.
        """
        if 0 <= index < len(self._indent_levels):
            return self._indent_levels[index]
        return 0

    # --- Backend → model ----------------------------------------------

    @Slot(dict)
    def apply(self, payload: dict[str, Any]) -> None:
        """Ingest one `"minimap"` envelope.

        Routed via an explicit `Qt.QueuedConnection` from
        `NvimBackend.minimap_event` (set up in AppController) — never
        call this directly from a worker thread. The connection point
        + comment is the single source of truth for the threading
        contract; this method assumes it runs on the GUI thread.

        Unknown / malformed payloads log + drop. The defensive try/except
        guards the cross-thread boundary the same way `_dispatch_notification`
        does on the backend side — a bad envelope from a future nvim
        version must not crash the GUI.
        """
        try:
            op = payload.get("op")
            if op == "snapshot":
                self._apply_snapshot(payload)
            elif op == "patch":
                self._apply_patch(payload)
            else:
                log.warning("minimap: unknown op %r in payload", op)
        except Exception:  # noqa: BLE001 — defensive cross-thread boundary
            log.exception("minimap: apply() failed on payload %r", payload)

    def _apply_snapshot(self, payload: dict[str, Any]) -> None:
        """Replace `_lines` with the snapshot's content.

        The Lua side guarantees `lines` is a list[str] and
        `line_count == len(lines)`. We trust the count but recompute
        from `len(lines)` defensively — if the two drift due to a
        future Lua-side bug, the painter's bounds checks stay correct.
        """
        bufnr = int(payload.get("bufnr", -1))
        lines_raw = payload.get("lines", [])
        if not isinstance(lines_raw, list):
            log.warning("minimap: snapshot.lines not a list: %r", type(lines_raw))
            return
        # Coerce each entry to str. pynvim usually returns str already,
        # but a buffer with non-UTF-8 bytes could in theory yield bytes.
        # The decode keeps the painter from crashing on .lstrip() etc.
        # bytes.decode("utf-8", errors="replace") never raises (errors= absorbs
        # all invalid sequences) so no try/except is needed here.
        new_lines = [
            line.decode("utf-8", errors="replace")
            if isinstance(line, bytes)
            else str(line)
            for line in lines_raw
        ]
        prev_count = self._line_count
        self._lines = new_lines
        self._line_count = len(new_lines)
        self._bufnr = bufnr
        # Recompute the full indent cache — snapshot replaces every
        # line, so every cached level is stale. Done here (in apply,
        # GUI-thread, before the linesChanged emit) so painters
        # connected to that signal can safely read `indent_level()`
        # without races.
        self._indent_levels = [_compute_indent_level(line) for line in new_lines]
        # Snapshot replaces the entire buffer — emit a full-range
        # change so connected painters repaint every row.
        self.linesChanged.emit(0, self._line_count)
        if prev_count != self._line_count:
            self.lineCountChanged.emit()

    def _apply_patch(self, payload: dict[str, Any]) -> None:
        """Splice in a partial update over `[first, last)`.

        Phase 1.5+ surface — the Lua side does not yet emit patches,
        but the model handles them so a future Lua change won't need
        a coupled Python change. `last` is exclusive (Python slice
        semantics); `lines` may be empty (pure deletion) or longer
        than `last - first` (insertion).
        """
        bufnr = int(payload.get("bufnr", -1))
        first = int(payload.get("first", 0))
        last = int(payload.get("last", 0))
        lines_raw = payload.get("lines", [])
        if not isinstance(lines_raw, list):
            log.warning("minimap: patch.lines not a list: %r", type(lines_raw))
            return
        if first < 0 or last < first or last > len(self._lines):
            log.warning(
                "minimap: patch range out of bounds: first=%d last=%d count=%d",
                first,
                last,
                len(self._lines),
            )
            return
        new_lines = [
            line.decode("utf-8", errors="replace")
            if isinstance(line, bytes)
            else str(line)
            for line in lines_raw
        ]
        prev_count = self._line_count
        self._lines[first:last] = new_lines
        self._line_count = len(self._lines)
        self._bufnr = bufnr
        # Recompute the indent cache for the affected slice only —
        # patches mutate `_lines[first:last]`, so only those indent
        # levels can have changed. Touching the whole array would be
        # O(N) per patch and defeat the patch's reason to exist;
        # splicing in just the new levels is O(len(new_lines)) and
        # keeps long-buffer edit cadence cheap.
        new_levels = [_compute_indent_level(line) for line in new_lines]
        self._indent_levels[first:last] = new_levels
        # Emit a range covering the SPLICED region — for an insert
        # that range may extend past `last`. Painters connected via
        # `linesChanged` re-render that range; for a snapshot-style
        # full repaint, listeners can still consult `line_count`.
        affected_end = first + len(new_lines)
        self.linesChanged.emit(first, max(last, affected_end))
        if prev_count != self._line_count:
            self.lineCountChanged.emit()
