"""NeoVim event dispatch: `redraw` handlers + notification routing.

Extracted from `nvim_backend.py` (issue #4) so the backend module can
focus on worker-thread lifecycle, subprocess spawning, and the GUI-facing
API (`input`, `resize`, `stop`). Everything here is concerned with
*interpreting* msgpack-RPC events from nvim and updating backend state
or emitting Qt signals.

Why free functions bound as methods (not a separate class): the dispatch
test scaffold (`tests/test_nvim_backend_dispatch.py`) exercises
`backend._h_cmdline_show(...)`, `backend._dispatch_redraw(...)`, and
mutates `nvim_backend._REDRAW_HANDLERS["flush"] = ...` directly. Keeping
these as free functions means `NvimBackend._h_* = nvim_events._h_*` at
class scope binds them via Python's descriptor protocol — `backend._h_*`
calls still resolve through the normal method-binding machinery, and
the `_REDRAW_HANDLERS` dict is the same object across both modules
(mutations in tests propagate). A separate class would have forced a
rewrite of the entire test scaffold.

TYPE_CHECKING guards the `NvimBackend` import to prevent a circular
import at runtime. Each handler's first parameter is annotated as
`NvimBackend` purely for type-checkers; at runtime it's just `self`
bound to the backend instance.
"""

from __future__ import annotations

import gc
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .nvim_backend import NvimBackend


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Redraw handlers
# ---------------------------------------------------------------------------
#
# Each `_h_*` below is invoked once per call within a `redraw` batch. The
# first arg (`self`) is the `NvimBackend` instance — bound by the class
# attribute assignment in `nvim_backend.py`. Handler names mirror the
# NeoVim UI event names (`grid_resize` → `_h_grid_resize`, etc.).


def _h_grid_resize(self: NvimBackend, grid: int, cols: int, rows: int) -> None:  # noqa: ARG001
    self.grid.resize(cols, rows)


def _h_grid_clear(self: NvimBackend, grid: int) -> None:  # noqa: ARG001
    self.grid.clear()


def _h_grid_line(
    self: NvimBackend,
    grid: int,  # noqa: ARG001
    row: int,
    col_start: int,
    cells: list,
    wrap: bool = False,  # noqa: ARG001, FBT002
    *_rest: Any,  # absorbs any arg added beyond wrap in future NeoVim versions (gotcha #9)
) -> None:
    self.grid.apply_line(row, col_start, cells)


def _h_grid_scroll(
    self: NvimBackend,
    grid: int,  # noqa: ARG001
    top: int,
    bot: int,
    left: int,
    right: int,
    rows: int,
    cols: int,  # noqa: ARG001 — redundant with `right - left`; NeoVim sends it anyway.
) -> None:
    self.grid.scroll(top, bot, left, right, rows)


def _h_grid_cursor_goto(self: NvimBackend, grid: int, row: int, col: int) -> None:  # noqa: ARG001
    self.grid.set_cursor(row, col)


def _h_hl_attr_define(
    self: NvimBackend,
    hl_id: int,
    rgb_attr: dict,
    _cterm_attr: dict | None = None,
    _info: list | None = None,
) -> None:
    self.grid.define_hl(hl_id, rgb_attr)


def _h_default_colors_set(
    self: NvimBackend,
    fg: int,
    bg: int,
    sp: int,
    _cterm_fg: int = 0,
    _cterm_bg: int = 0,
) -> None:
    self.grid.set_default_colors(fg, bg, sp)


def _h_mode_info_set(
    self: NvimBackend,
    _cursor_style_enabled: bool,
    mode_info: list,
) -> None:
    self._mode_info = mode_info
    # Re-emit the resolved descriptor — mode_info_set can arrive
    # either before the first mode_change (startup) or after it
    # (e.g. user runs `:set guicursor=...` mid-session). Either way
    # the view wants the current resolved view, not the raw list.
    self.cursor_mode_updated.emit(_resolved_mode_info(self))


def _h_mode_change(self: NvimBackend, mode: str, mode_idx: int) -> None:
    self.grid.mode = mode
    self._mode_idx = int(mode_idx) if mode_idx is not None else 0
    self.cursor_mode_updated.emit(_resolved_mode_info(self))


def _resolved_mode_info(self: NvimBackend) -> dict[str, Any]:
    """Look up the current mode's cursor descriptor.

    Returns a best-effort dict. Missing keys (some mode_info entries
    from older nvim versions omit blink fields) are left absent so
    the view can apply its own defaults without special-casing.
    """
    if not self._mode_info:
        return {}
    idx = self._mode_idx
    if not (0 <= idx < len(self._mode_info)):
        return {}
    entry = self._mode_info[idx]
    if not isinstance(entry, dict):
        return {}
    # Shallow copy — the mode_info list is owned by the worker
    # thread and we don't want GUI-side mutations to leak back.
    return dict(entry)


def _h_flush(self: NvimBackend) -> None:
    self.redraw_flushed.emit()


# --- Ext-cmdline / ext-popupmenu handlers --------------------------------
#
# These events arrive through the same `redraw` notification as
# grid_line/grid_scroll, but they describe cmdline + wildmenu state
# the native UI renders on top of the grid. NeoVim still owns the
# cmdline *logic* (typing, history, completion); we only render.


def _h_cmdline_show(
    self: NvimBackend,
    content: list,
    pos: int,
    firstc: str,
    prompt: str,
    indent: int,  # noqa: ARG001 — we don't render prompt indent yet
    level: int,
    _hl_id: int = 0,  # NeoVim 0.10+ passes firstchar hl_id here
) -> None:
    # content is [[attrs, text], ...] in older NeoVim and
    # [[attrs, text, hl_id], ...] in 0.10+. Flatten for MVP;
    # per-chunk highlights can come later by preserving the tuples.
    parts: list[str] = []
    for chunk in content or ():
        if isinstance(chunk, (list, tuple)) and len(chunk) >= 2:
            parts.append(str(chunk[1]))
    self.cmdline_updated.emit(
        {
            "kind": "show",
            "text": "".join(parts),
            "pos": int(pos or 0),
            "firstchar": str(firstc or ""),
            "prompt": str(prompt or ""),
            "level": int(level or 0),
        }
    )


def _h_cmdline_pos(self: NvimBackend, pos: int, level: int, *_rest: Any) -> None:
    # *_rest swallows any trailing args newer NeoVim versions may
    # add — keeps the handler forward-compatible.
    self.cmdline_updated.emit(
        {
            "kind": "pos",
            "pos": int(pos or 0),
            "level": int(level or 0),
        }
    )


def _h_cmdline_hide(self: NvimBackend, level: int = 0, *_rest: Any) -> None:
    # Some NeoVim versions pass `abort` (0.9) then added more
    # fields; *_rest absorbs whatever else comes through.
    self.cmdline_updated.emit(
        {
            "kind": "hide",
            "level": int(level or 0),
        }
    )


def _h_popupmenu_show(
    self: NvimBackend,
    items: list,
    selected: int,
    row: int,  # noqa: ARG001 — cmdline-anchored popup is positioned by QML
    col: int,  # noqa: ARG001
    _grid: int = -1,
    *_rest: Any,
) -> None:
    flattened: list[dict[str, str]] = []
    for it in items or ():
        if not isinstance(it, (list, tuple)):
            continue
        word = str(it[0]) if len(it) >= 1 else ""
        kind = str(it[1]) if len(it) >= 2 else ""
        menu = str(it[2]) if len(it) >= 3 else ""
        # `info` (it[3]) can be large documentation; omit for now.
        flattened.append({"word": word, "kind": kind, "menu": menu})
    self.popupmenu_updated.emit(
        {
            "kind": "show",
            "items": flattened,
            "selected": int(selected if selected is not None else -1),
        }
    )


def _h_popupmenu_select(self: NvimBackend, selected: int) -> None:
    self.popupmenu_updated.emit(
        {
            "kind": "select",
            "selected": int(selected if selected is not None else -1),
        }
    )


def _h_popupmenu_hide(self: NvimBackend) -> None:
    self.popupmenu_updated.emit({"kind": "hide"})


# ---------------------------------------------------------------------------
# Redraw-event → handler lookup table
# ---------------------------------------------------------------------------
#
# Keeping this at module scope avoids an attribute lookup per event on
# the hot path. Tests mutate this dict (e.g. installing a recording
# wrapper at `_REDRAW_HANDLERS["flush"]`) — import-by-reference from
# `nvim_backend.py` means both namespaces point at the same dict, so
# mutations via either name are observed by `_dispatch_redraw`.
_REDRAW_HANDLERS: dict[str, Callable[..., None]] = {
    "grid_resize": _h_grid_resize,
    "grid_clear": _h_grid_clear,
    "grid_line": _h_grid_line,
    "grid_scroll": _h_grid_scroll,
    "grid_cursor_goto": _h_grid_cursor_goto,
    "hl_attr_define": _h_hl_attr_define,
    "default_colors_set": _h_default_colors_set,
    "mode_info_set": _h_mode_info_set,
    "mode_change": _h_mode_change,
    "flush": _h_flush,
    "cmdline_show": _h_cmdline_show,
    "cmdline_pos": _h_cmdline_pos,
    "cmdline_hide": _h_cmdline_hide,
    "popupmenu_show": _h_popupmenu_show,
    "popupmenu_select": _h_popupmenu_select,
    "popupmenu_hide": _h_popupmenu_hide,
}


# ---------------------------------------------------------------------------
# Dispatch entrypoints
# ---------------------------------------------------------------------------


def _dispatch_redraw(self: NvimBackend, batches: list[Any]) -> None:
    """Apply one `redraw` notification's batches to the grid.

    Each batch is `[event_name, *args_lists]`. NeoVim packs
    multiple identical events into one batch for efficiency (the
    first entry is the name, every subsequent entry is one call's
    args), so we iterate call-by-call.

    GC is suspended for the duration. `apply_line` is allocation-heavy
    (one `Cell` per updated grid position; ~3600/frame on a 120x30
    grid), and Python 3.14 tracks even tuples-of-primitives so every
    allocation counts toward `gc.threshold`. Collection cycles that
    fire mid-dispatch race with the Qt scene-graph render thread
    running `paint()` — the crash trace shows `Cell.__init__` →
    GC on the worker thread while `_paint_row` sits in a
    `painter.setPen(...)` C++ call on `QSGRenderThread`. Deferring
    GC to outside this critical section closes the window. The
    outer `_on_notification` wrapper (in `nvim_backend.py`) also
    suspends GC; the redundancy is intentional for tests that call
    `_dispatch_redraw` directly without going through
    `_on_notification`.
    """
    gc_was_enabled = gc.isenabled()
    if gc_was_enabled:
        gc.disable()
    try:
        for batch in batches:
            event = batch[0]
            calls = batch[1:]
            handler = _REDRAW_HANDLERS.get(event)
            if handler is None:
                continue
            for call in calls:
                try:
                    handler(self, *call)
                except Exception:  # noqa: BLE001
                    log.exception("failed to apply %s %r", event, call)
    finally:
        if gc_was_enabled:
            gc.enable()


def _dispatch_notification(self: NvimBackend, name: str, args: list[Any]) -> None:
    """Route one pynvim notification to the right signal or handler.

    GC suspension is handled by the outer `_on_notification` wrapper
    (in `nvim_backend.py`); this function assumes GC is already
    disabled when called through that entrypoint. Tests call it
    directly and accept that GC may be enabled during the call —
    the invariant they check (gotcha #10) is enforced by
    `_on_notification`, not here.
    """
    if name == "redraw":
        _dispatch_redraw(self, args)
        return
    if name == "capsule":
        if not args or not isinstance(args[0], dict):
            log.warning("capsule notification with unexpected payload: %r", args)
            return
        payload: dict = args[0]
        log.debug("capsule notification: %r", payload)
        self.capsule_updated.emit(payload)
        return
    if name == "completions":
        if not args or not isinstance(args[0], dict):
            log.warning(
                "completions notification with unexpected payload: %r",
                args,
            )
            return
        self.completions_updated.emit(args[0])
        return
    if name == "scroll":
        if not args or not isinstance(args[0], dict):
            log.warning("scroll notification with unexpected payload: %r", args)
            return
        try:
            delta = int(args[0].get("delta", 0))
        except (TypeError, ValueError):
            log.warning("scroll payload has non-int delta: %r", args[0])
            return
        if delta != 0:
            self.viewport_scrolled.emit(delta)
        return
    if name == "whichkey":
        if not args or not isinstance(args[0], dict):
            log.warning("whichkey notification with unexpected payload: %r", args)
            return
        self.whichkey_event.emit(args[0])
        return
    if name == "fm":
        if not args or not isinstance(args[0], dict):
            log.warning("fm notification with unexpected payload: %r", args)
            return
        self.fm_event.emit(args[0])
        return
    if name == "nav":
        if not args or not isinstance(args[0], dict):
            log.warning("nav notification with unexpected payload: %r", args)
            return
        self.nav_event.emit(args[0])
        return
    if name == "anchor":
        if not args or not isinstance(args[0], dict):
            log.warning("anchor notification with unexpected payload: %r", args)
            return
        self.anchor_event.emit(args[0])
        return
    if name == "minimap":
        # Editor minimap content channel (Phase 1 of docs/minimap-prd.md).
        # Lua emits full-buffer snapshots; AppController routes the
        # signal to MinimapModel.apply via Qt.QueuedConnection. Payload
        # is the dict envelope documented in MinimapModel.apply's
        # docstring; defensive shape check matches the other branches.
        if not args or not isinstance(args[0], dict):
            log.warning("minimap notification with unexpected payload: %r", args)
            return
        self.minimap_event.emit(args[0])
        return
    if name == "minimap_viewport":
        # Editor minimap viewport channel (Phase 3). Lua emits
        # `{first, count}` on cursor/scroll motion; AppController
        # routes the signal to MinimapModel.apply_viewport via
        # Qt.QueuedConnection. Payload shape documented in
        # MinimapModel.apply_viewport's docstring.
        if not args or not isinstance(args[0], dict):
            log.warning(
                "minimap_viewport notification with unexpected payload: %r", args
            )
            return
        self.minimap_viewport_event.emit(args[0])
        return
    if name == "minimap_diagnostics":
        # Editor minimap diagnostic channel (Phase 4). Lua emits a
        # list of `{lnum, severity}` entries on DiagnosticChanged.
        # Routed to MinimapModel.apply_diagnostics.
        if not args or not isinstance(args[0], dict):
            log.warning(
                "minimap_diagnostics notification with unexpected payload: %r",
                args,
            )
            return
        self.minimap_diagnostics_event.emit(args[0])
        return
    if name == "minimap_git":
        # Editor minimap git-diff channel (Phase 4). Lua reads from
        # gitsigns.nvim and emits per-lnum hunk entries; routed to
        # MinimapModel.apply_git.
        if not args or not isinstance(args[0], dict):
            log.warning("minimap_git notification with unexpected payload: %r", args)
            return
        self.minimap_git_event.emit(args[0])
        return
    log.debug("unhandled notification: %s (args=%r)", name, args)
