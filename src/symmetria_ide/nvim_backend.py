"""NeoVim backend: spawn `nvim --embed`, pump `redraw` events.

Runs pynvim's blocking event loop in a worker thread. Redraw events
update the `Grid` in place; on every `flush` event and every capsule
notification, Qt signals cross into the GUI thread (queued connections
handle the thread hop automatically).

The GUI side calls `input(keys)` to forward keystrokes, and
`resize(cols, rows)` when the visible grid dimensions change.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

import pynvim
from PySide6.QtCore import QObject, Signal, Slot

from .grid import Grid


log = logging.getLogger(__name__)


# Directory containing our Lua runtime (init.lua, etc.).
_RUNTIME_DIR = Path(__file__).resolve().parent.parent.parent / "runtime"


class NvimBackend(QObject):
    """Owns the NeoVim process and its Grid state.

    Thread layout: pynvim's `run_loop` blocks in `_worker`, receiving
    redraw notifications and capsule `rpcnotify` messages. Every `flush`
    event emits `redraw_flushed`, which QML connects to to trigger a
    repaint. Every capsule payload emits `capsule_updated(dict)`.
    """

    redraw_flushed = Signal()
    # Emitted when the active window's topline changes. Drives the
    # viewport scroll animation. Payload is the line delta: positive =
    # content scrolls up (Ctrl-d), negative = content scrolls down
    # (Ctrl-u). Fed by the WinScrolled autocmd in runtime/init.lua.
    # Preferred over grid_scroll_performed because WinScrolled fires
    # reliably for any viewport change, not just the ones where NeoVim
    # uses the grid_scroll redraw optimization.
    viewport_scrolled = Signal(int)
    # Emitted from _h_grid_scroll before the in-place mutation. Kept
    # for parity/diagnostics even though the view now prefers
    # viewport_scrolled as its source of truth.
    grid_scroll_performed = Signal(int, int, int, int, int, int, int)
    capsule_updated = Signal(dict)
    cmdline_updated = Signal(dict)
    popupmenu_updated = Signal(dict)
    completions_updated = Signal(dict)
    closed = Signal()

    def __init__(
        self,
        cols: int = 120,
        rows: int = 30,
        runtime_dir: Path | None = None,
        clean: bool = False,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._cols = cols
        self._rows = rows
        self._runtime_dir = runtime_dir or _RUNTIME_DIR
        self._clean = clean
        self.grid = Grid()
        self._nvim: pynvim.Nvim | None = None
        self._worker: threading.Thread | None = None
        self._stopping = False
        self._mode_info: list[dict[str, Any]] = []

    # --- Lifecycle -----------------------------------------------------

    def start(self) -> None:
        """Spawn nvim, attach UI, start the event thread.

        `--embed` gives us the msgpack-RPC channel over stdio; `-n`
        skips swapfile creation. We load our `runtime/` first via `--cmd
        luafile` so capsule emission is wired before the user's own
        init.lua runs — their config then overrides normally.

        Pass `symmetria_clean=True` to force `--clean` for isolation
        testing (bypasses user config entirely). Default is False so
        NeoVim motions and plugins match the user's everyday setup.
        """
        if self._nvim is not None:
            return
        argv = [
            "nvim",
            "--embed",
            "-n",
            "--cmd",
            f"set rtp^={self._runtime_dir}",
            "--cmd",
            f"luafile {self._runtime_dir / 'init.lua'}",
        ]
        if self._clean:
            argv.insert(3, "--clean")
        log.info("spawning nvim: %s", argv)
        try:
            self._nvim = pynvim.attach("child", argv=argv)
        except Exception:
            log.exception(
                "failed to spawn nvim — is nvim installed and on PATH?"
            )
            raise
        # rgb=true: NeoVim sends rgb hex values (no color indices).
        # ext_linegrid=true: use the modern grid_line-based protocol.
        # ext_cmdline=true: NeoVim stops drawing the `:` prompt inside
        #   the grid and instead fires cmdline_show/_pos/_hide events
        #   that our native QML overlay renders.
        # ext_popupmenu=true: same extraction for wildmenu autocomplete.
        self._nvim.ui_attach(
            self._cols,
            self._rows,
            rgb=True,
            ext_linegrid=True,
            ext_cmdline=True,
            ext_popupmenu=True,
        )
        self._worker = threading.Thread(
            target=self._run_loop,
            name="nvim-event-loop",
            daemon=True,
        )
        self._worker.start()

    def stop(self) -> None:
        """Tear down: schedule nvim to quit, wait for worker to exit.

        Called from the GUI thread on app shutdown. We can't call RPC
        methods directly — they'd raise the same cross-thread error
        `input`/`resize` would. Instead, marshal `quit` via async_call,
        then let the worker exit naturally when nvim closes the channel.
        """
        self._stopping = True
        nvim = self._nvim
        if nvim is not None:
            def _quit() -> None:
                try:
                    nvim.command("qa!")
                except Exception:  # noqa: BLE001
                    log.debug("nvim qa! failed on shutdown", exc_info=True)

            try:
                nvim.async_call(_quit)
            except Exception:  # noqa: BLE001
                log.debug("async_call(quit) failed", exc_info=True)
            try:
                nvim.close()
            except Exception:  # noqa: BLE001
                log.debug("nvim.close failed on shutdown", exc_info=True)
        if self._worker is not None:
            self._worker.join(timeout=1.0)
        self._nvim = None
        self._worker = None

    # --- Worker thread -------------------------------------------------

    def _run_loop(self) -> None:
        assert self._nvim is not None
        try:
            self._nvim.run_loop(
                request_cb=self._on_request,
                notification_cb=self._on_notification,
                setup_cb=self._on_loop_setup,
                err_cb=self._on_err,
            )
        except EOFError:
            # nvim exited normally (e.g. user typed `:q` inside the
            # editor). The channel closes, pynvim raises EOFError. This
            # isn't a crash — log at DEBUG, not ERROR.
            log.debug("nvim closed its RPC channel (normal exit)")
        except Exception:  # noqa: BLE001
            if not self._stopping:
                log.exception("nvim event loop crashed")
        finally:
            self._stopping = True
            self.closed.emit()

    def _on_loop_setup(self) -> None:
        """Runs on the loop thread before notifications start arriving.

        Subscribing here (not in `start`) is required: pynvim only
        delivers notifications for event names we've explicitly asked
        about, and the subscribe call must run on the loop thread.

        After subscribing we eagerly request the current capsule state —
        `init.lua` has already fired its initial `M.push_state()` during
        nvim startup (before we subscribed), so without this round-trip
        we'd see an empty status bar until the first mode change.
        """
        assert self._nvim is not None
        try:
            self._nvim.subscribe("capsule")
            self._nvim.subscribe("completions")
            self._nvim.subscribe("scroll")
            log.info(
                "subscribed to 'capsule' + 'completions' + 'scroll' notifications"
            )
        except Exception:  # noqa: BLE001
            log.exception("subscribe(capsule/completions) failed")
        try:
            self._nvim.exec_lua(
                "if _G.symmetria_push_state then _G.symmetria_push_state() end"
            )
            log.info("requested initial capsule push")
        except Exception:  # noqa: BLE001
            log.debug("initial push_state call failed", exc_info=True)

    def _on_request(self, name: str, args: list[Any]) -> Any:  # noqa: ARG002
        """Handle an RPC request from NeoVim.

        NeoVim's UI client protocol does not send requests to the UI
        (only notifications), so this handler is intentionally a no-op.
        Returning None is correct — pynvim sends a nil reply.
        """
        log.debug("rpc request: %s", name)
        return None

    def _on_notification(self, name: str, args: list[Any]) -> None:
        if name == "redraw":
            self._dispatch_redraw(args)
            return
        if name == "capsule":
            if not args or not isinstance(args[0], dict):
                log.warning(
                    "capsule notification with unexpected payload: %r", args
                )
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
                log.warning(
                    "scroll notification with unexpected payload: %r", args
                )
                return
            try:
                delta = int(args[0].get("delta", 0))
            except (TypeError, ValueError):
                log.warning("scroll payload has non-int delta: %r", args[0])
                return
            if delta != 0:
                self.viewport_scrolled.emit(delta)
            return
        log.debug("unhandled notification: %s (args=%r)", name, args)

    def _on_err(self, msg: str) -> None:
        log.warning("nvim stderr: %s", msg.rstrip())

    def _dispatch_redraw(self, batches: list[Any]) -> None:
        """Apply one `redraw` notification's batches to the grid.

        Each batch is `[event_name, *args_lists]`. NeoVim packs
        multiple identical events into one batch for efficiency (the
        first entry is the name, every subsequent entry is one call's
        args), so we iterate call-by-call.
        """
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

    # --- Redraw handlers (invoked from _dispatch_redraw) ---------------

    def _h_grid_resize(self, grid: int, cols: int, rows: int) -> None:  # noqa: ARG002
        self.grid.resize(cols, rows)

    def _h_grid_clear(self, grid: int) -> None:  # noqa: ARG002
        self.grid.clear()

    def _h_grid_line(
        self,
        grid: int,  # noqa: ARG002
        row: int,
        col_start: int,
        cells: list,
        wrap: bool = False,  # noqa: ARG002, FBT002
    ) -> None:
        self.grid.apply_line(row, col_start, cells)

    def _h_grid_scroll(
        self,
        grid: int,  # noqa: ARG002
        top: int,
        bot: int,
        left: int,
        right: int,
        rows: int,
        cols: int,  # noqa: ARG002 — redundant with `right - left`; NeoVim sends it anyway.
    ) -> None:
        # Emit pre-mutation: the view needs the scroll delta + the grid
        # dimensions as they were *before* the shift so it can size its
        # scrollback buffer and accumulate the pending delta. The actual
        # cell snapshot happens later on flush when grid_line events for
        # the scrolled-in region have landed.
        self.grid_scroll_performed.emit(
            top, bot, left, right, rows, self.grid.cols, self.grid.rows
        )
        self.grid.scroll(top, bot, left, right, rows)

    def _h_grid_cursor_goto(self, grid: int, row: int, col: int) -> None:  # noqa: ARG002
        self.grid.set_cursor(row, col)

    def _h_hl_attr_define(
        self,
        hl_id: int,
        rgb_attr: dict,
        _cterm_attr: dict | None = None,
        _info: list | None = None,
    ) -> None:
        self.grid.define_hl(hl_id, rgb_attr)

    def _h_default_colors_set(
        self,
        fg: int,
        bg: int,
        sp: int,
        _cterm_fg: int = 0,
        _cterm_bg: int = 0,
    ) -> None:
        self.grid.set_default_colors(fg, bg, sp)

    def _h_mode_info_set(self, _cursor_style_enabled: bool, mode_info: list) -> None:
        self._mode_info = mode_info

    def _h_mode_change(self, mode: str, _mode_idx: int) -> None:
        self.grid.mode = mode

    def _h_flush(self) -> None:
        self.redraw_flushed.emit()

    # --- Ext-cmdline / ext-popupmenu handlers --------------------------
    #
    # These events arrive through the same `redraw` notification as
    # grid_line/grid_scroll, but they describe cmdline + wildmenu state
    # the native UI renders on top of the grid. NeoVim still owns the
    # cmdline *logic* (typing, history, completion); we only render.

    def _h_cmdline_show(
        self,
        content: list,
        pos: int,
        firstc: str,
        prompt: str,
        indent: int,  # noqa: ARG002 — we don't render prompt indent yet
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
        self.cmdline_updated.emit({
            "kind": "show",
            "text": "".join(parts),
            "pos": int(pos or 0),
            "firstchar": str(firstc or ""),
            "prompt": str(prompt or ""),
            "level": int(level or 0),
        })

    def _h_cmdline_pos(self, pos: int, level: int, *_rest: Any) -> None:
        # *_rest swallows any trailing args newer NeoVim versions may
        # add — keeps the handler forward-compatible.
        self.cmdline_updated.emit({
            "kind": "pos",
            "pos": int(pos or 0),
            "level": int(level or 0),
        })

    def _h_cmdline_hide(self, level: int = 0, *_rest: Any) -> None:
        # Some NeoVim versions pass `abort` (0.9) then added more
        # fields; *_rest absorbs whatever else comes through.
        self.cmdline_updated.emit({
            "kind": "hide",
            "level": int(level or 0),
        })

    def _h_popupmenu_show(
        self,
        items: list,
        selected: int,
        row: int,  # noqa: ARG002 — cmdline-anchored popup is positioned by QML
        col: int,  # noqa: ARG002
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
        self.popupmenu_updated.emit({
            "kind": "show",
            "items": flattened,
            "selected": int(selected if selected is not None else -1),
        })

    def _h_popupmenu_select(self, selected: int) -> None:
        self.popupmenu_updated.emit({
            "kind": "select",
            "selected": int(selected if selected is not None else -1),
        })

    def _h_popupmenu_hide(self) -> None:
        self.popupmenu_updated.emit({"kind": "hide"})

    # --- GUI-thread-facing API -----------------------------------------
    #
    # pynvim requires all RPC calls to run on the thread that owns its
    # event loop — the worker thread in our case. Calling from the GUI
    # thread raises `NvimError: request from non-main thread`. Every
    # method below marshals its work through `nvim.async_call`, which is
    # thread-safe and queues the callback onto the loop thread.

    @Slot(str)
    def input(self, keys: str) -> None:
        """Forward a NeoVim keycode string (e.g. `i`, `<Esc>`) to nvim."""
        nvim = self._nvim
        if nvim is None or not keys:
            return

        def _do() -> None:
            try:
                nvim.input(keys)
            except Exception:  # noqa: BLE001
                log.exception("nvim.input failed for %r", keys)

        try:
            nvim.async_call(_do)
        except Exception:  # noqa: BLE001
            log.exception("async_call(input) failed")

    @Slot(int, int)
    def resize(self, cols: int, rows: int) -> None:
        """Tell nvim to re-lay-out to this cell dimension."""
        nvim = self._nvim
        if nvim is None:
            return
        if cols == self._cols and rows == self._rows:
            return
        self._cols = cols
        self._rows = rows

        def _do() -> None:
            try:
                nvim.ui_try_resize(cols, rows)
            except Exception:  # noqa: BLE001
                log.exception("ui_try_resize failed")

        try:
            nvim.async_call(_do)
        except Exception:  # noqa: BLE001
            log.exception("async_call(resize) failed")


# Map redraw event name → bound-method lookup. Keeping this outside the
# class avoids an attribute lookup per event on a hot path.
_REDRAW_HANDLERS = {
    "grid_resize": NvimBackend._h_grid_resize,
    "grid_clear": NvimBackend._h_grid_clear,
    "grid_line": NvimBackend._h_grid_line,
    "grid_scroll": NvimBackend._h_grid_scroll,
    "grid_cursor_goto": NvimBackend._h_grid_cursor_goto,
    "hl_attr_define": NvimBackend._h_hl_attr_define,
    "default_colors_set": NvimBackend._h_default_colors_set,
    "mode_info_set": NvimBackend._h_mode_info_set,
    "mode_change": NvimBackend._h_mode_change,
    "flush": NvimBackend._h_flush,
    "cmdline_show": NvimBackend._h_cmdline_show,
    "cmdline_pos": NvimBackend._h_cmdline_pos,
    "cmdline_hide": NvimBackend._h_cmdline_hide,
    "popupmenu_show": NvimBackend._h_popupmenu_show,
    "popupmenu_select": NvimBackend._h_popupmenu_select,
    "popupmenu_hide": NvimBackend._h_popupmenu_hide,
}
