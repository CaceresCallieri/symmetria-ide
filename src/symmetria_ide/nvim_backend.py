"""NeoVim backend: spawn `nvim --embed`, pump `redraw` events.

Runs pynvim's blocking event loop in a worker thread. Redraw events
update the `Grid` in place; on every `flush` event and every capsule
notification, Qt signals cross into the GUI thread (queued connections
handle the thread hop automatically).

The GUI side calls `input(keys)` to forward keystrokes, and
`resize(cols, rows)` when the visible grid dimensions change.

Event dispatch (the `_h_*` handlers and `_dispatch_redraw` /
`_dispatch_notification`) lives in `nvim_events.py` so this module can
focus on worker-thread lifecycle, subprocess spawning, and the
GUI-facing API. Extracted handlers are re-bound as methods at class
scope below so the existing test scaffold (which exercises
`backend._h_cmdline_show(...)` etc. directly) keeps working without
changes. `_REDRAW_HANDLERS` is re-exported at module scope for the
same reason — dispatch tests mutate the table via
`nvim_backend._REDRAW_HANDLERS[...]`.
"""

from __future__ import annotations

import gc
import logging
import threading
from pathlib import Path
from typing import Any

import pynvim
from PySide6.QtCore import QObject, Signal, Slot

from . import nvim_events
from .grid import Grid

# Re-exported so tests can do `nvim_backend._REDRAW_HANDLERS[...]`.
# Must be the SAME dict as `nvim_events._REDRAW_HANDLERS` — Python imports
# mutable objects by reference, so mutations via either namespace are
# visible to `_dispatch_redraw`.
from .nvim_events import _REDRAW_HANDLERS  # noqa: F401


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
    # More reliable than grid_scroll events: WinScrolled fires for any
    # viewport change, not just those where NeoVim uses the scroll-shift
    # redraw optimization.
    viewport_scrolled = Signal(int)

    # Emitted when nvim reports a mode change OR updates mode_info.
    # Payload is the resolved mode descriptor dict — the relevant keys
    # for rendering are `cursor_shape` ("block" | "vertical" |
    # "horizontal"), `cell_percentage` (int, 0-100, for bar/underline
    # thickness), and `blinkwait` / `blinkon` / `blinkoff` (ints in ms).
    # We resolve here rather than sending the full mode_info list + idx
    # so the view doesn't need to worry about ordering between the two
    # events: either one arriving triggers a re-emit with the current
    # resolved view. Empty dict means "no info yet" — view should fall
    # back to a solid block cursor.
    cursor_mode_updated = Signal(dict)

    capsule_updated = Signal(dict)
    cmdline_updated = Signal(dict)
    popupmenu_updated = Signal(dict)
    completions_updated = Signal(dict)
    # Native which-key overlay payload. Shape:
    #   { op: "show"|"hide", mode, trail, can_go_back, items: [...] }
    # Each item is { key, desc, is_group, icon, icon_color }.
    # See `runtime/lua/orchestrator/whichkey/init.lua` for the emitter.
    whichkey_event = Signal(dict)
    # File manager toggle-overlay lifecycle. Payload shape:
    #   { op: "show"|"hide"|"toggle", initialPath?: string }
    # The overlay floats above NvimView; the user's compositor is not
    # involved (unlike the standalone Symmetria File Manager which spawns
    # a separate window). Source of truth is AppController.fmVisible.
    # The `"fm"` rpcnotify channel is preserved as a stable contract for
    # a future Lua-side opener (e.g. a `:SymFm` user command); no
    # currently-installed Lua emitter exists post-decoupling.
    fm_event = Signal(dict)
    # Window-navigation bridge. Lua emits via rpcnotify when <C-h/j/k/l>
    # is pressed at the edge of nvim's window splits — payload is
    # `{op:"move", dir:"left|right|up|down"}` for spillover, or
    # `{op:"debug", event:"keymap_install", reason:...}` for diagnostic
    # traces. Same routing pattern as fm_event.
    nav_event = Signal(dict)
    # Project-anchor lifecycle from `:SymmetriaAnchor` / `:SymmetriaUnanchor`.
    # Payload: `{op:"set", path?:string}` or `{op:"clear"}`. AppController
    # routes "set" to anchor_to_path (or anchor_to_current_cwd when path
    # is absent) and "clear" to release_anchor. The PRIMARY anchor trigger
    # is a Qt application-scope shortcut that calls those Slots directly
    # — this RPC channel is the secondary (scripted) surface.
    anchor_event = Signal(dict)
    # Editor minimap content channel — Phase 1 of docs/minimap-prd.md.
    # Lua emitter at runtime/lua/orchestrator/minimap.lua pushes full
    # buffer snapshots (and, post-Phase 1.5, patches) over the "minimap"
    # rpcnotify channel; the payload is the dict envelope documented in
    # `MinimapModel.apply`'s docstring. AppController connects this to
    # MinimapModel.apply via explicit Qt.QueuedConnection (§4 P2 — the
    # signal originates on the pynvim worker thread).
    minimap_event = Signal(dict)
    closed = Signal()

    # --- Dispatch bindings --------------------------------------------
    #
    # Each assignment below makes a free function from `nvim_events`
    # behave as a bound method of `NvimBackend`. Python's descriptor
    # protocol handles the self-binding at attribute-access time:
    # `backend._h_cmdline_show(...)` resolves to
    # `nvim_events._h_cmdline_show(backend, ...)` — identical semantics
    # to before extraction. Tests that call these directly, or shadow
    # them via `backend._dispatch_notification = capture_state`, keep
    # working without modification.
    _dispatch_redraw = nvim_events._dispatch_redraw
    _dispatch_notification = nvim_events._dispatch_notification
    # Helper used by _h_mode_info_set and _h_mode_change — not a dispatch
    # entrypoint itself, but bound here so tests can call it directly.
    _resolved_mode_info = nvim_events._resolved_mode_info

    # Per-event handlers (one per NeoVim UI event name):
    _h_grid_resize = nvim_events._h_grid_resize
    _h_grid_clear = nvim_events._h_grid_clear
    _h_grid_line = nvim_events._h_grid_line
    _h_grid_scroll = nvim_events._h_grid_scroll
    _h_grid_cursor_goto = nvim_events._h_grid_cursor_goto
    _h_hl_attr_define = nvim_events._h_hl_attr_define
    _h_default_colors_set = nvim_events._h_default_colors_set
    _h_mode_info_set = nvim_events._h_mode_info_set
    _h_mode_change = nvim_events._h_mode_change
    _h_flush = nvim_events._h_flush
    _h_cmdline_show = nvim_events._h_cmdline_show
    _h_cmdline_pos = nvim_events._h_cmdline_pos
    _h_cmdline_hide = nvim_events._h_cmdline_hide
    _h_popupmenu_show = nvim_events._h_popupmenu_show
    _h_popupmenu_select = nvim_events._h_popupmenu_select
    _h_popupmenu_hide = nvim_events._h_popupmenu_hide

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
        # Cooperative-shutdown signal. The worker is `daemon=True` so
        # interpreter exit alone won't hang, but the daemon-only contract
        # is silent: the main thread can't cleanly wait for the nvim loop
        # to finish, and tests can't assert "stop() actually unblocked."
        # `_stop_event` is set (a) at the top of `stop()`, before any RPC
        # or close call, and (b) in the worker's `finally` block — so
        # both the cooperative and crash-exit paths are observable.
        # Standards §1 P0 — "every long-running thread is daemon=True
        # OR owns an explicit shutdown Event"; we satisfy both.
        self._stop_event = threading.Event()
        self._mode_info: list[dict[str, Any]] = []
        self._mode_idx: int = 0

    @property
    def stop_event(self) -> threading.Event:
        """Shutdown signal — set as soon as teardown begins or the worker exits.

        Exposed for tests and any future coordinator that needs to wait
        on the nvim backend's lifecycle without polling `_worker.is_alive()`.
        """
        return self._stop_event

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
            log.exception("failed to spawn nvim — is nvim installed and on PATH?")
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

        `_stop_event` is set FIRST so any concurrent observer (tests,
        health-check loop) sees shutdown-in-progress before the RPC
        round-trip even starts.
        """
        self._stop_event.set()
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
            if not self._stop_event.is_set():
                log.exception("nvim event loop crashed")
        finally:
            # Set unconditionally — covers both the cooperative stop()
            # path and the "nvim crashed / closed unexpectedly" path, so
            # anyone blocking on stop_event.wait(...) is unblocked either way.
            self._stop_event.set()
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
            self._nvim.subscribe("whichkey")
            self._nvim.subscribe("minimap")
            log.info(
                "subscribed to 'capsule' + 'completions' + 'scroll' + 'whichkey' "
                "+ 'minimap' notifications"
            )
        except Exception:  # noqa: BLE001
            log.exception("subscribe(capsule/completions) failed")
        # Force a minimap snapshot re-push to plug the subscribe race
        # (CLAUDE.md gotcha #2). The Lua side fires its first BufEnter
        # snapshot during nvim startup — before this subscribe call —
        # so without the explicit re-request the minimap stays empty
        # until the user's first edit. Same mitigation the capsule
        # channel uses below.
        try:
            self._nvim.exec_lua(
                "if _G.symmetria_minimap_push_snapshot then "
                "_G.symmetria_minimap_push_snapshot() end"
            )
            log.info("requested initial minimap snapshot")
        except Exception:  # noqa: BLE001
            log.exception("initial minimap push request failed")
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
        # GC is suspended for the entire handler, not just _dispatch_redraw.
        # Python 3.14's incremental cyclic GC can fire from ANY allocation
        # on this worker thread — including logging.debug() inside pynvim's
        # msgpack session handler BEFORE we even reach _dispatch_redraw.
        # A crash trace showed GC mid-`logging.__init__.py:1498 debug` from
        # `session.py:269 handler`, racing with QSGRenderThread inside
        # `_paint_row`. Widening the gc.disable window to cover the whole
        # notification entrypoint closes that window without affecting
        # the existing _dispatch_redraw guard (which is now redundant when
        # entered through here, but kept for defence-in-depth and because
        # some code paths invoke _dispatch_redraw directly in tests).
        gc_was_enabled = gc.isenabled()
        if gc_was_enabled:
            gc.disable()
        try:
            self._dispatch_notification(name, args)
        finally:
            if gc_was_enabled:
                gc.enable()

    def _on_err(self, msg: str) -> None:
        log.warning("nvim stderr: %s", msg.rstrip())

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

    @Slot(str)
    def set_current_dir(self, path: str) -> None:
        """Change nvim's working directory to `path`.

        Goes through the `nvim_set_current_dir` RPC (via
        `nvim.api.set_current_dir`) rather than a `:cd <path>` command
        string — the API call accepts a raw path without us having to
        worry about shell-style escaping of spaces or special chars.
        Marshalled through `nvim.async_call` per gotcha #1 (pynvim is
        not thread-safe). No-op when nvim hasn't been spawned yet
        (matches `input` / `resize`); the caller's slot may fire before
        `start()` returns and we don't want to crash that path.

        Does NOT touch open buffers. Wiping or refreshing buffers on a
        project change is a deliberate non-goal here — that belongs to
        the future session-open flow, where the user has explicitly
        chosen to switch project context. This method only updates
        `:pwd` so file pickers, `:find`, and git-from-nvim follow the
        IDE's displayed project root.
        """
        nvim = self._nvim
        if nvim is None or not path:
            return

        def _do() -> None:
            try:
                nvim.api.set_current_dir(path)
            except Exception:  # noqa: BLE001
                log.exception("nvim_set_current_dir failed for %r", path)

        try:
            nvim.async_call(_do)
        except Exception:  # noqa: BLE001
            log.exception("async_call(set_current_dir) failed")

    @Slot(str)
    def edit_file(self, path: str) -> None:
        """Open `path` in the current window — mode-independent.

        Uses `nvim_cmd` (via `nvim.api.cmd`) instead of feeding
        `:execute 'edit ...'` through `input()`. The keystroke route is
        mode-dependent: in terminal mode `:` is a printable char that
        gets shipped down the PTY to the running shell; in insert mode
        the whole command would be typed into the buffer; in
        operator-pending mode it's meaningless. `nvim_cmd` runs above
        mode dispatch in nvim's command layer.

        Structured `{cmd, args}` form means nvim never re-parses a
        composed string, so paths with spaces / `%` / `#` / `[` need no
        `fnameescape` ceremony — the path is already an opaque arg.

        Marshalled via `nvim.async_call` per gotcha #1. No-op when nvim
        hasn't spawned yet (matches `input` / `resize` / `set_current_dir`).
        No `bang=True`: a dirty current buffer should error out the same
        way `:edit foo` does at the cmdline, not silently discard work.
        """
        nvim = self._nvim
        if nvim is None or not path:
            return

        def _do() -> None:
            try:
                nvim.api.cmd({"cmd": "edit", "args": [path]}, {})
            except Exception:  # noqa: BLE001
                log.exception("nvim_cmd edit failed for %r", path)

        try:
            nvim.async_call(_do)
        except Exception:  # noqa: BLE001
            log.exception("async_call(edit_file) failed")

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
