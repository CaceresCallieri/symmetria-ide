"""NeoVim backend: attach to an `nvim --listen` socket, relay chrome rpcnotify.

NeoVim runs as a TUI **inside the QMLTermWidget editor surface** — the
QMLTermSession in Main.qml spawns it with `nvim --listen <socket>`, and nvim
draws its own grid in that terminal widget. This module attaches an RPC
connection to that socket purely for:

  * CONTROL — `input`, `edit_file`, `set_current_dir` (the IDE driving nvim;
    the seam the long-arc feature migration rides on); and
  * DATA — the rpcnotify chrome channels (`capsule`, `cmdline`, `completions`,
    `whichkey`, `minimap*`, `nav`, `anchor`, `fm`) that `runtime/init.lua` +
    the orchestrator modules emit. The IDE renders these as native overlays.

It deliberately does NOT `ui_attach` — there is no grid/redraw protocol here
(nvim renders the grid in the terminal). The custom grid renderer, scroll/
cursor animations, and the ext_linegrid/ext_cmdline redraw handlers that the
embed model used are gone; the command line is now relayed by an IN-PROCESS
`vim.ui_attach` inside init.lua over the `cmdline` channel (the noice.nvim
mechanism — see `runtime/init.lua`).

Thread layout: pynvim's blocking `run_loop` runs in `_worker`; the socket
attach (with a retry budget, since the editor nvim may not have bound the
socket the instant we start) also happens on that worker so the GUI thread
never blocks. Notifications cross into the GUI thread via Qt signals (queued
connections handle the hop). GC is suspended around the notification handler
(gotcha #10) — the same recipe `SessionHost` uses.
"""

from __future__ import annotations

import gc
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

import pynvim
from PySide6.QtCore import QObject, Signal, Slot


log = logging.getLogger(__name__)


# Directory containing our Lua runtime (init.lua, etc.). AppController uses
# this to build the editor nvim's launch argv; kept here as the canonical
# location since this module owns the nvim integration contract.
_RUNTIME_DIR = Path(__file__).resolve().parent.parent.parent / "runtime"

# Socket-attach retry budget. The editor QMLTermSession spawns
# `nvim --listen <sock>` at QML engine-load time (before AppController.start),
# so the socket usually exists by the time we attach — but the file only
# appears once nvim's startup reaches the `--listen` bind, so we still poll on
# the worker thread (NOT the GUI thread) so the UI never freezes waiting.
_SOCKET_WAIT_TIMEOUT_S = 5.0
_SOCKET_POLL_INTERVAL_S = 0.05


class NvimBackend(QObject):
    """Owns the RPC connection to the terminal-hosted nvim.

    No grid state, no rendering — just the control surface (`input`,
    `edit_file`, `set_current_dir`) plus the chrome rpcnotify relays
    emitted as Qt signals onto the GUI thread.
    """

    # --- Chrome rpcnotify relays (emitted from the worker thread) ------
    # Status-bar capsules (mode/file/branch/project/pos/cwd) from init.lua.
    capsule_updated = Signal(dict)
    # Command line — relayed by the in-process vim.ui_attach in init.lua.
    # Payload `{kind: "show"|"pos"|"hide", ...}` matches CmdlineState.apply.
    cmdline_updated = Signal(dict)
    # Our getcompletion()-based cmdline completion list.
    completions_updated = Signal(dict)
    # Native which-key overlay payload (orchestrator.whichkey emitter).
    whichkey_event = Signal(dict)
    # File-manager toggle channel (stable contract; no live Lua emitter
    # post-decoupling — the primary FM trigger is a Qt app shortcut).
    fm_event = Signal(dict)
    # Window-navigation bridge: <C-h/j/k/l> edge spillover between panes.
    nav_event = Signal(dict)
    # Project-anchor lifecycle from :SymmetriaAnchor / :SymmetriaUnanchor.
    anchor_event = Signal(dict)
    # Editor minimap channels (content / viewport indicator / diagnostics /
    # git gutter) — all driven by orchestrator.minimap, renderer-independent.
    minimap_event = Signal(dict)
    minimap_viewport_event = Signal(dict)
    minimap_diagnostics_event = Signal(dict)
    minimap_git_event = Signal(dict)
    closed = Signal()
    # Emitted (worker thread) once the RPC channel is attached + subscribed.
    # Session restore waits on this to replay saved editor buffers, since the
    # worker may attach AFTER AppController.start() has returned. Fires on
    # every (re)attach — consumers guard one-shot work themselves.
    attached = Signal()

    # rpcnotify channel name -> the Signal attribute that relays it. The
    # worker subscribes to every key and re-emits args[0] on the matching
    # signal. This flat table replaced the old embed model's redraw state
    # machine (since-deleted) — with no ui_attach there are no grid/redraw
    # events to parse, only these notification channels.
    _CHANNEL_TO_SIGNAL: dict[str, str] = {
        "capsule": "capsule_updated",
        "cmdline": "cmdline_updated",
        "completions": "completions_updated",
        "whichkey": "whichkey_event",
        "fm": "fm_event",
        "nav": "nav_event",
        "anchor": "anchor_event",
        "minimap": "minimap_event",
        "minimap_viewport": "minimap_viewport_event",
        "minimap_diagnostics": "minimap_diagnostics_event",
        "minimap_git": "minimap_git_event",
    }

    # Lua globals re-invoked after subscribe to plug the subscribe-race
    # (gotcha #2): init.lua / orchestrator fire their first push during nvim
    # startup, BEFORE we attach + subscribe, so we explicitly re-request.
    _INITIAL_PUSH_GLOBALS: tuple[str, ...] = (
        "symmetria_push_state",
        "symmetria_minimap_push_snapshot",
        "symmetria_minimap_push_viewport",
        "symmetria_minimap_push_diagnostics",
        "symmetria_minimap_push_git",
    )

    def __init__(
        self,
        socket_path: str,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._socket_path = socket_path
        self._nvim: pynvim.Nvim | None = None
        self._worker: threading.Thread | None = None
        # Cooperative-shutdown signal. Worker is daemon=True (interpreter
        # exit won't hang) AND owns this Event so shutdown is observable.
        # Set at the top of stop() and in the worker's finally. §1 P0.
        self._stop_event = threading.Event()

    @property
    def stop_event(self) -> threading.Event:
        """Shutdown signal — set as teardown begins or the worker exits."""
        return self._stop_event

    @property
    def is_attached(self) -> bool:
        """True once the RPC channel is live (the worker has attached). Lets
        session restore choose between an immediate replay (already attached —
        the manual Ctrl+Shift+S path) and waiting on the `attached` signal
        (the reload path, where the worker is still connecting)."""
        return self._nvim is not None

    # --- Lifecycle -----------------------------------------------------

    def start(self) -> None:
        """Start the worker, which attaches to the nvim socket then loops.

        The attach + retry happens ON the worker thread (not here) so the
        GUI thread never blocks waiting for the editor nvim to bind its
        `--listen` socket. Idempotent: a second call while live is a no-op.
        """
        if self._worker is not None:
            return
        self._worker = threading.Thread(
            target=self._run_loop,
            name="nvim-rpc-loop",
            daemon=True,
        )
        self._worker.start()

    def stop(self) -> None:
        """Ask nvim to quit (best-effort) and join the worker.

        `_stop_event` is set FIRST so a concurrent socket-wait aborts and
        observers see shutdown-in-progress before any RPC round-trip. The
        editor QMLTermSession (KSession) reaps the nvim child when the QML
        engine tears down on app quit, so it is the backstop if `qa!` doesn't
        land (e.g. socket already gone); this stays best-effort.
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

    def _attach_with_retry(self) -> pynvim.Nvim | None:
        """Poll for the socket then attach, on the worker thread.

        Returns the attached Nvim, or None if the budget elapses / stop()
        is signalled first. The editor nvim binds `--listen` a beat after
        its process spawns, so the file may not exist on our first look.
        """
        deadline = time.monotonic() + _SOCKET_WAIT_TIMEOUT_S
        while not self._stop_event.is_set() and time.monotonic() < deadline:
            if os.path.exists(self._socket_path):
                try:
                    return pynvim.attach("socket", path=self._socket_path)
                except Exception:  # noqa: BLE001
                    log.debug("nvim socket attach retry", exc_info=True)
            self._stop_event.wait(timeout=_SOCKET_POLL_INTERVAL_S)
        if not self._stop_event.is_set():
            log.error(
                "could not attach to nvim socket %s within %.1fs",
                self._socket_path,
                _SOCKET_WAIT_TIMEOUT_S,
            )
        return None

    def _run_loop(self) -> None:
        nvim = self._attach_with_retry()
        if nvim is None:
            self._stop_event.set()
            self.closed.emit()
            return
        # Publish for the GUI-facing methods. Reference assignment is atomic
        # under the GIL; the methods no-op while this is still None.
        self._nvim = nvim
        try:
            nvim.run_loop(
                request_cb=self._on_request,
                notification_cb=self._on_notification,
                setup_cb=self._on_loop_setup,
                err_cb=self._on_err,
            )
        except EOFError:
            # nvim exited (e.g. user `:qa` in the editor) — channel closes,
            # pynvim raises EOFError. Not a crash.
            log.debug("nvim closed its RPC channel (normal exit)")
        except Exception:  # noqa: BLE001
            if not self._stop_event.is_set():
                log.exception("nvim rpc loop crashed")
        finally:
            # Set unconditionally — covers cooperative stop() and the
            # crash/closed paths so stop_event.wait() always unblocks.
            self._stop_event.set()
            self.closed.emit()

    def _on_loop_setup(self) -> None:
        """Subscribe to the chrome channels + re-request the initial pushes.

        Runs on the loop thread (pynvim requires subscribe there). The
        re-pushes plug the subscribe-race (gotcha #2): init.lua/orchestrator
        fired their first payloads during nvim startup, before we attached.
        """
        nvim = self._nvim
        if nvim is None:
            return
        for channel in self._CHANNEL_TO_SIGNAL:
            try:
                nvim.subscribe(channel)
            except Exception:  # noqa: BLE001
                log.exception("subscribe(%s) failed", channel)
        for fn in self._INITIAL_PUSH_GLOBALS:
            try:
                nvim.exec_lua(f"if _G.{fn} then _G.{fn}() end")
            except Exception:  # noqa: BLE001
                log.debug("initial re-push %s failed", fn, exc_info=True)
        # Channel is live + subscribed — let the GUI thread replay any pending
        # session-restore editor buffers (queued connection handles the hop).
        self.attached.emit()

    def _on_request(self, name: str, args: list[Any]) -> Any:  # noqa: ARG002
        """nvim sends no requests to a plain RPC client — no-op (nil reply)."""
        log.debug("rpc request: %s", name)
        return None

    def _on_notification(self, name: str, args: list[Any]) -> None:
        # GC suspended for the whole handler (gotcha #10): Python 3.14's
        # incremental cyclic GC can fire from any allocation on this worker
        # thread (incl. pynvim's msgpack/logging internals) and race the
        # QSGRenderThread. Same recipe as SessionHost.
        gc_was_enabled = gc.isenabled()
        if gc_was_enabled:
            gc.disable()
        try:
            self._dispatch_notification(name, args)
        finally:
            if gc_was_enabled:
                gc.enable()

    def _dispatch_notification(self, name: str, args: list[Any]) -> None:
        """Route an rpcnotify to its Qt signal. Unknown channels ignored."""
        attr = self._CHANNEL_TO_SIGNAL.get(name)
        if attr is None:
            return
        payload = args[0] if args else {}
        if not isinstance(payload, dict):
            log.debug("notification %s payload not a dict: %r", name, payload)
            return
        getattr(self, attr).emit(payload)

    def _on_err(self, msg: str) -> None:
        log.warning("nvim stderr: %s", msg.rstrip())

    # --- GUI-thread-facing control API ---------------------------------
    #
    # pynvim requires RPC calls on the loop thread; every method marshals
    # via `nvim.async_call` (thread-safe). All no-op until the worker has
    # attached (self._nvim is None) — matches the pre-swap defensive shape.

    @Slot(str)
    def input(self, keys: str) -> None:
        """Forward a NeoVim keycode string (e.g. `i`, `<Esc>`) to nvim.

        Secondary/scripted path: ordinary typing flows through the editor
        terminal's PTY straight to nvim. This is for IDE-initiated input
        (replaying a sequence over the control channel)."""
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
        """Change nvim's working directory to `path` via nvim_set_current_dir.

        Raw-path API call (no shell escaping). Marshalled via async_call
        (gotcha #1). No-op before attach. Updates `:pwd` only — does not
        touch open buffers (project-switch buffer handling is a non-goal
        here). Keeps file pickers / `:find` / git-from-nvim following the
        IDE's displayed project root."""
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
        """Open `path` in the editor's current window via nvim_cmd.

        Structured `{cmd, args}` runs above mode dispatch, so it works
        regardless of nvim's current mode and needs no `fnameescape` for
        paths with spaces / `%` / `#`. This is the control path the IDE
        file manager uses to open files in the editor surface. Marshalled
        via async_call (gotcha #1); no-op before attach. No `bang`: a dirty
        buffer errors like `:edit foo` rather than discarding work."""
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

    @Slot()
    def checktime(self) -> None:
        """Re-stat loaded buffers; reload any changed on disk (autoread).

        The IDE's real-time external-reload path. An agent (or the shell
        pane) rewriting a file on disk does not touch nvim, so an open
        buffer stays stale until something runs `:checktime`. The IDE
        drives this off the recursive working-tree watcher
        (`GitController.workingTreeChanged`), so agent edits reload the
        visible buffer live — even while the editor surface is HIDDEN
        (nvim redraws its grid; the user sees the fresh content the
        instant they swap back).

        `silent!` keeps the grid quiet for the conflict case: when a
        buffer has UNSAVED local edits AND its file also changed on disk,
        `autoread` deliberately declines to reload (preserving the user's
        in-flight work) and nvim prints a non-blocking W12 warning —
        which we suppress. `autoread` itself is asserted in
        `runtime/init.lua`; without it `:checktime` would pop an
        interactive (O)K/(L)oad prompt instead of reloading silently.

        Marshalled via async_call (gotcha #1); no-op before attach."""
        nvim = self._nvim
        if nvim is None:
            return

        def _do() -> None:
            try:
                nvim.command("silent! checktime")
            except Exception:  # noqa: BLE001
                log.exception("nvim checktime failed")

        try:
            nvim.async_call(_do)
        except Exception:  # noqa: BLE001
            log.exception("async_call(checktime) failed")

    # --- Session save/restore support ----------------------------------
    #
    # These three are the editor side of the IDE's sessionizer. Unlike the
    # fire-and-forget control methods above, `query_buffers` and `save_all`
    # are SYNCHRONOUS (sync-over-async — the inverse of gotcha #1): the GUI
    # thread schedules the work on the loop thread via async_call, then blocks
    # on a threading.Event until it completes. They run only at teardown /
    # restore, where a brief GUI-thread block is fine — and `save_all` MUST
    # block, or teardown's `qa!` would force-quit before `:wall` lands and
    # discard the very edits "Save & close" promised to keep.

    @staticmethod
    def _collect_buffers(nvim: pynvim.Nvim) -> list[dict]:
        """Enumerate listed, loaded, named buffers (runs on the loop thread).

        Returns one dict per buffer: `{path, modified, active, line, col}`.
        Cursor `line`/`col` are the live values for the ACTIVE buffer only
        (others default to 1/0) — inactive cursors come back free at restore
        via nvim's own shada `'"` mark, written by the graceful `qa!`. One
        enumeration serves two callers: the manifest (all paths + active
        cursor) and the dirty-buffer modal (`[b for b in … if b['modified']]`).
        """
        try:
            cur_handle = nvim.current.buffer.handle
        except Exception:  # noqa: BLE001
            cur_handle = -1
        try:
            cur_line, cur_col = nvim.current.window.cursor
        except Exception:  # noqa: BLE001
            cur_line, cur_col = 1, 0
        out: list[dict] = []
        for buf in nvim.buffers:
            try:
                if not buf.valid or not nvim.api.buf_is_loaded(buf):
                    continue
                name = buf.name
                if not name or not buf.options["buflisted"]:
                    continue
                active = buf.handle == cur_handle
                out.append(
                    {
                        "path": name,
                        "modified": bool(buf.options["modified"]),
                        "active": active,
                        "line": int(cur_line) if active else 1,
                        "col": int(cur_col) if active else 0,
                    }
                )
            except Exception:  # noqa: BLE001
                log.debug("query_buffers: skipping a buffer", exc_info=True)
        return out

    def query_buffers(self, timeout: float = 2.0) -> list[dict]:
        """Snapshot open buffers `[{path, modified, active, line, col}]`.

        Synchronous: blocks the calling (GUI) thread until the loop thread
        replies or `timeout` elapses. Returns `[]` before attach, on timeout,
        or on error — callers treat that as "no editor state / nothing dirty".
        """
        nvim = self._nvim
        if nvim is None:
            return []
        result: list[dict] = []
        done = threading.Event()

        def _do() -> None:
            try:
                result.extend(self._collect_buffers(nvim))
            except Exception:  # noqa: BLE001
                log.exception("query_buffers failed")
            finally:
                done.set()

        try:
            nvim.async_call(_do)
        except Exception:  # noqa: BLE001
            log.exception("async_call(query_buffers) failed")
            return []
        if not done.wait(timeout):
            log.warning("query_buffers timed out after %.1fs", timeout)
            return []
        return result

    def save_all(self, timeout: float = 5.0) -> bool:
        """Write every modified buffer (`:wall`); block until it lands.

        SYNCHRONOUS by necessity: this backs the "Save & close" teardown
        action, and the subsequent `qa!` would discard unsaved work if `:wall`
        had not completed first. Returns True on success, False before attach /
        on timeout / on error.
        """
        nvim = self._nvim
        if nvim is None:
            return False
        done = threading.Event()
        ok = [False]

        def _do() -> None:
            try:
                nvim.command("silent! wall")
                ok[0] = True
            except Exception:  # noqa: BLE001
                log.exception("nvim wall failed")
            finally:
                done.set()

        try:
            nvim.async_call(_do)
        except Exception:  # noqa: BLE001
            log.exception("async_call(wall) failed")
            return False
        if not done.wait(timeout):
            log.warning("save_all timed out after %.1fs", timeout)
            return False
        return ok[0]

    def restore_buffers(
        self, files: list[str], active: str = "", line: int = 1, col: int = 0
    ) -> None:
        """Reopen `files`, display `active`, restore its cursor (fire-and-forget).

        `badd` adds each file to the buffer list without displaying it; `edit`
        then shows the active one (so the window lands where the user left it).
        The cursor is clamped to the buffer's line count. No-op before attach
        or with an empty list. Marshalled via async_call (gotcha #1); does not
        block — restore can settle asynchronously after the surface is shown.
        """
        nvim = self._nvim
        if nvim is None or not files:
            return
        target = active or files[0]

        def _do() -> None:
            try:
                for path in files:
                    if path and path != target:
                        nvim.api.cmd({"cmd": "badd", "args": [path]}, {})
                nvim.api.cmd({"cmd": "edit", "args": [target]}, {})
                try:
                    total = nvim.api.buf_line_count(0)
                    clamped = max(1, min(int(line), total))
                    nvim.api.win_set_cursor(0, [clamped, max(0, int(col))])
                except Exception:  # noqa: BLE001
                    log.debug("restore_buffers: cursor restore skipped", exc_info=True)
            except Exception:  # noqa: BLE001
                log.exception("restore_buffers failed")

        try:
            nvim.async_call(_do)
        except Exception:  # noqa: BLE001
            log.exception("async_call(restore_buffers) failed")
