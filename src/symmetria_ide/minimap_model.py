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


# Diagnostic severity ranking — when multiple diagnostics land on the
# same line, the painter shows ONE dot (gutter is 4 px wide; stacking
# isn't legible at minimap scale). We pick the highest-severity one,
# with this rank order: error > warn > info > hint > unknown.
#
# The wire format is already a string (translated from the
# `vim.diagnostic.severity` enum on the Lua side) so the model
# doesn't need to know the enum values; it just resolves a string-to-
# string max-by-rank. Phase 4 (PRD §8) calls out R4.2 — "diagnostic
# clustering at minimap scale acceptable" — this dict is what
# implements the clustering deterministically.
_DIAGNOSTIC_RANK: dict[str, int] = {
    "error": 4,
    "warn": 3,
    "info": 2,
    "hint": 1,
}


# Valid git-diff hunk kinds. Anything else from a future gitsigns
# version is dropped at the apply boundary (logged, no crash). The
# Lua side normalises gitsigns' short forms (add/change/delete) to
# the wire format below; keep both sides in sync if extended.
_GIT_KINDS: frozenset[str] = frozenset({"added", "modified", "deleted"})


def _compute_line_metrics(line: str) -> tuple[int, int]:
    """Map a buffer line to (indent_level, content_length) in one walk.

    Phase 4.5 — the minimap painter now clips each block's width to
    the actual content length (chars after leading whitespace), so
    short lines and blank rows render as short bars / gaps and the
    silhouette reflects real document shape. Computing both metrics
    here saves a second per-line walk on every apply.

    - `indent_level`: 0..`_MAX_INDENT_LEVEL` rung (clamped); empty
      lines and pure-whitespace lines are level 0 (no content to
      indent under). Same semantics as the pre-4.5 function.
    - `content_length`: number of characters after leading whitespace.
      Empty lines, pure-whitespace lines, and unindented blank rows
      all return 0 — the painter renders them as a gap. UTF-8
      multi-byte characters count as 1 (Python's `len` over str
      counts codepoints, which is the right unit at minimap scale).

    Pure-function — tests exercise it without standing up a model.
    Hot path — called once per line per apply(), never inside paint().
    Single str walk; no str allocations (str.lstrip would allocate a
    fresh str per call, which we deliberately avoid).
    """
    columns = 0
    leading = 0
    for ch in line:
        if ch == " ":
            columns += 1
            leading += 1
        elif ch == "\t":
            # Tab counts as a full level boundary regardless of tab
            # width — see module-level comment for the rationale.
            columns += _INDENT_COLUMNS_PER_LEVEL
            leading += 1
        else:
            break
    level = columns // _INDENT_COLUMNS_PER_LEVEL
    if level > _MAX_INDENT_LEVEL:
        level = _MAX_INDENT_LEVEL
    content_length = len(line) - leading
    if content_length < 0:
        # Structurally unreachable — the loop's break guarantees
        # leading <= len(line) (we only increment leading while
        # consuming whitespace, then break on the first non-whitespace
        # char). Belt-and-braces guard so the painter never receives
        # a negative width if this logic ever changes.
        content_length = 0
    return level, content_length


def _compute_indent_level(line: str) -> int:
    """Back-compat alias for the indent-only consumer (tests).
    Phase 4.5 replaced the dedicated indent walk with `_compute_line_metrics`
    above; this thin wrapper preserves the older entry point so existing
    tests that exercise the pure-function indent semantics keep working.
    """
    level, _ = _compute_line_metrics(line)
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

    # Viewport range change — Phase 3. Listeners (MinimapView's painter)
    # repaint to move the viewport indicator. Decoupled from linesChanged
    # so a scroll without an edit doesn't trigger a full silhouette
    # re-render; the painter still does one update() either way, but the
    # signal cardinality lets future phases optimise.
    viewportChanged = Signal()

    # Diagnostic / git gutter state changed — Phase 4. The two are
    # distinct cadences (DiagnosticChanged fires from LSP/lint deltas;
    # git hunks update on save/focus/debounced edit) but converge on
    # the same painter step (left-edge gutter). Separate signals so
    # listeners that care about only one source don't get re-evaluated
    # on the other.
    diagnosticsChanged = Signal()
    gitChanged = Signal()

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
        # Parallel cache of content-after-leading-whitespace lengths
        # per line — Phase 4.5. The painter clips each row's block
        # width to `content_length * _CHAR_WIDTH_PX` so short lines
        # render short and blanks render as gaps; without this the
        # silhouette was a wall of full-width bars regardless of
        # actual line content. Recomputed inside apply() alongside
        # `_indent_levels` from a single `_compute_line_metrics`
        # call (one walk per line for both metrics).
        self._content_lengths: list[int] = []
        # Viewport range — Phase 3. `_viewport_first` is the 0-indexed
        # buffer line at the top of the editor's visible region;
        # `_viewport_count` is the number of visible lines. (0, 0)
        # means "no viewport info yet" — the painter draws no
        # indicator until the first apply_viewport arrives, which
        # NvimBackend force-requests at subscribe time so the first
        # paint after editor focus already has bounds.
        self._viewport_first: int = 0
        self._viewport_count: int = 0
        # Phase 4 gutter state. Both are sparse — only rows with an
        # active diagnostic / hunk appear as keys, so empty lookups
        # are O(1) and the painter pays no per-line cost on clean
        # files. Maps are wiped on each apply (the wire format is
        # always a complete fresh list per buffer).
        self._diagnostics: dict[int, str] = {}
        self._git_hunks: dict[int, str] = {}

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

    def viewport_first(self) -> int:
        """0-indexed buffer line at the top of the editor's visible region.
        Phase 3 painter reads this + `viewport_count()` to draw the
        viewport indicator. Returns 0 when no apply_viewport has
        arrived yet — the painter then skips drawing the indicator."""
        return self._viewport_first

    def viewport_count(self) -> int:
        """Number of buffer lines currently visible in the editor.
        Returns 0 before the first apply_viewport — the painter
        treats zero-count as 'no indicator yet' and skips drawing."""
        return self._viewport_count

    def diagnostic_at(self, lnum: int) -> str:
        """Highest-severity diagnostic at 0-indexed buffer line `lnum`,
        or "" if no diagnostic is active there. Phase 4 painter looks
        up each visible row's severity to colour the gutter dot."""
        return self._diagnostics.get(lnum, "")

    def git_at(self, lnum: int) -> str:
        """Git-diff hunk kind at 0-indexed buffer line `lnum`, or ""
        when the line matches HEAD. Phase 4 painter draws the gutter
        bar with the matching colour."""
        return self._git_hunks.get(lnum, "")

    def diagnostic_count(self) -> int:
        """Total number of rows carrying an active diagnostic. Useful
        for tests and for painter early-exit (skip the gutter pass if
        zero — no dots to draw)."""
        return len(self._diagnostics)

    def git_count(self) -> int:
        """Total number of rows in a git hunk. Same early-exit utility
        as `diagnostic_count`."""
        return len(self._git_hunks)

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

    def content_length(self, index: int) -> int:
        """Cached content length (chars after leading whitespace) for
        line `index`. Phase 4.5 — painter uses this to clip block
        width so short lines render as short bars. Same bounds-clamping
        contract as `indent_level()` — out-of-range returns 0 (gap)."""
        if 0 <= index < len(self._content_lengths):
            return self._content_lengths[index]
        return 0

    # --- Backend → model ----------------------------------------------

    @Slot(dict)
    def apply_diagnostics(self, payload: dict[str, Any]) -> None:
        """Ingest one `"minimap_diagnostics"` envelope.

        Phase 4. Payload shape:
            { bufnr: int, entries: [{lnum: int, severity: str}, ...] }

        We collapse multiple diagnostics on the same line to a single
        max-severity dot (PRD §8.3 R4.2) using `_DIAGNOSTIC_RANK`.
        The wire format is always a full fresh list per buffer, so
        the model wipes `_diagnostics` and rebuilds — no delta logic
        needed.

        Equality short-circuit: if the rebuilt dict equals the prior
        state, skip the diagnosticsChanged emit. LSP servers
        re-publish identical sets fairly often (per-keystroke for
        some configs); the dedup keeps QML bindings stable.
        """
        try:
            entries = payload.get("entries", [])
            if not isinstance(entries, list):
                log.warning(
                    "minimap: diagnostics.entries not a list: %r", type(entries)
                )
                return
            new_diag: dict[int, str] = {}
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                lnum_raw = entry.get("lnum")
                sev_raw = entry.get("severity")
                if not isinstance(lnum_raw, int) or not isinstance(sev_raw, str):
                    continue
                if lnum_raw < 0:
                    continue
                # Max-severity-wins on collision.
                existing = new_diag.get(lnum_raw)
                if existing is None or _DIAGNOSTIC_RANK.get(
                    sev_raw, 0
                ) > _DIAGNOSTIC_RANK.get(existing, 0):
                    new_diag[lnum_raw] = sev_raw
            if new_diag == self._diagnostics:
                return
            self._diagnostics = new_diag
            self.diagnosticsChanged.emit()
        except Exception:  # noqa: BLE001 — defensive cross-thread boundary
            log.exception("minimap: apply_diagnostics() failed on payload %r", payload)

    @Slot(dict)
    def apply_git(self, payload: dict[str, Any]) -> None:
        """Ingest one `"minimap_git"` envelope.

        Phase 4. Payload shape:
            { bufnr: int, entries: [{lnum: int, kind: str}, ...] }

        Each gitsigns hunk has already been expanded to per-lnum
        entries on the Lua side, so this method just rebuilds the
        dict — no range expansion needed. Last entry for a given lnum
        wins on collision (the rare overlap case from gitsigns
        emitting both a deletion-marker and a change on the same
        line; either kind is a valid surface).

        Equality short-circuit matches apply_diagnostics's pattern.
        """
        try:
            entries = payload.get("entries", [])
            if not isinstance(entries, list):
                log.warning("minimap: git.entries not a list: %r", type(entries))
                return
            new_git: dict[int, str] = {}
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                lnum_raw = entry.get("lnum")
                kind_raw = entry.get("kind")
                if not isinstance(lnum_raw, int) or not isinstance(kind_raw, str):
                    continue
                if lnum_raw < 0 or kind_raw not in _GIT_KINDS:
                    continue
                new_git[lnum_raw] = kind_raw
            if new_git == self._git_hunks:
                return
            self._git_hunks = new_git
            self.gitChanged.emit()
        except Exception:  # noqa: BLE001 — defensive cross-thread boundary
            log.exception("minimap: apply_git() failed on payload %r", payload)

    @Slot(dict)
    def apply_viewport(self, payload: dict[str, Any]) -> None:
        """Ingest one `"minimap_viewport"` envelope `{first, count}`.

        Phase 3 of docs/minimap-prd.md — drives the viewport indicator.
        Routed via `Qt.QueuedConnection` from
        `NvimBackend.minimap_viewport_event` (set up in
        AppController). Same defensive try/except contract as
        `apply()` — a malformed envelope must not crash the GUI
        thread.

        Equality-short-circuit: only emit viewportChanged when the
        bounds actually changed. Rapid cursor motion inside the
        viewport (j/k within visible region) fires CursorMoved
        repeatedly without changing the viewport range; the Lua
        coalesce flag already drops most of these, but the Python
        side dedups for extra hygiene.
        """
        try:
            first = int(payload.get("first", 0))
            count = int(payload.get("count", 0))
            if first < 0:
                first = 0
            if count < 0:
                count = 0
            if first == self._viewport_first and count == self._viewport_count:
                return
            self._viewport_first = first
            self._viewport_count = count
            self.viewportChanged.emit()
        except Exception:  # noqa: BLE001 — defensive cross-thread boundary
            log.exception("minimap: apply_viewport() failed on payload %r", payload)

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
        # Recompute the full indent + content-length caches via the
        # unified metrics walk — snapshot replaces every line, so
        # every cached value is stale. Building both arrays in a
        # single comprehension means one walk per line, not two.
        # Done here (in apply, GUI-thread, before linesChanged emit)
        # so painters connected to that signal can safely read
        # `indent_level()` / `content_length()` without races.
        metrics = [_compute_line_metrics(line) for line in new_lines]
        self._indent_levels = [m[0] for m in metrics]
        self._content_lengths = [m[1] for m in metrics]
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
        # Recompute the indent + content-length caches for the affected
        # slice only — patches mutate `_lines[first:last]`, so only
        # those metrics can have changed. Touching the whole arrays
        # would be O(N) per patch and defeat the patch's reason to
        # exist; splicing in just the new values is O(len(new_lines))
        # and keeps long-buffer edit cadence cheap. One unified walk
        # per line yields both metrics.
        metrics = [_compute_line_metrics(line) for line in new_lines]
        new_levels = [m[0] for m in metrics]
        new_content = [m[1] for m in metrics]
        self._indent_levels[first:last] = new_levels
        self._content_lengths[first:last] = new_content
        # Emit a range covering the SPLICED region — for an insert
        # that range may extend past `last`. Painters connected via
        # `linesChanged` re-render that range; for a snapshot-style
        # full repaint, listeners can still consult `line_count`.
        affected_end = first + len(new_lines)
        self.linesChanged.emit(first, max(last, affected_end))
        if prev_count != self._line_count:
            self.lineCountChanged.emit()
