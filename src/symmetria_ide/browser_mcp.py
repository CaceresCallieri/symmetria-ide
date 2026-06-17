"""IDE-hosted browser MCP server (Phase 4 Stage 2b).

Exposes the embedded browser to spawned agents as MCP tools over local
streamable-HTTP, so an agent drives the IDE's OWN browser window instead
of launching its own Chromium (the containment win — no escaped Hyprland
window). Agents discover it via per-harness `--mcp-config` injection
(Stage 2c, agent_harness.py).

Topology / threading:

    agent (separate process)
      │  MCP over http://127.0.0.1:<port>/mcp
      ▼
    FastMCP + uvicorn          ← daemon thread, own asyncio loop
      │  async tool body: await bridge.op(...)
      ▼
    BrowserMcpBridge           ← queued Qt signal, marshals ONTO the GUI thread
      │  BrowserAutomation.request(...)
      ▼
    qml/BrowserSurface         ← runJavaScript on the WebEngineView (GUI thread)
      │  on_result(...)  → resolves the concurrent.futures.Future
      ▼
    bridge.op() returns        ← asyncio.wrap_future completes the await

The bridge is the marshaling seam and is mcp-independent + unit-tested.
`mcp`/`uvicorn` are imported lazily inside `BrowserMcpServer.start()` so
this module (and the bridge) import even if the package is absent — the
IDE then runs Stage-1-only (manual browser) with a logged warning.

The marshaling direction is the INVERSE of gotcha #1: there we push
pynvim RPC OFF the GUI thread; here we push the tool call ONTO it (the
WebEngineViews live there). `concurrent.futures.Future.set_result` is
thread-safe; `asyncio.wrap_future` bridges it back to the uvicorn loop.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import glob
import json
import logging
import os
import socket
import tempfile
import threading
from typing import Callable

from PySide6.QtCore import QObject, Qt, Signal, Slot

from .browser_automation import BrowserAutomation

log = logging.getLogger(__name__)

# Server identity in the agents' MCP config.
SERVER_NAME = "symmetria-browser"

# Per-launch MCP config files are named <prefix><pid>.json in the temp dir.
_CONFIG_PREFIX = "symmetria-browser-mcp-"


def reap_orphan_configs() -> None:
    """Remove browser-MCP config files left by dead IDE instances.

    The IDE unlinks its own config on graceful shutdown, but a hard-kill
    (SIGKILL / crash) leaves it behind, which confuses external clients that
    discover the server via these files (e.g. bench/browser_mcp_live.py). The
    filename encodes the writing IDE's pid, so a config whose pid is no longer
    alive is safe to remove. Mirrors `_reap_orphan_nvim_sockets` in app.py.
    Best-effort: per-file errors are ignored; the current pid is never touched.
    """
    pattern = os.path.join(tempfile.gettempdir(), f"{_CONFIG_PREFIX}*.json")
    for path in glob.glob(pattern):
        try:
            pid = int(os.path.basename(path)[len(_CONFIG_PREFIX) : -len(".json")])
        except ValueError:
            continue  # not a pid-named config — leave it alone
        if pid == os.getpid() or os.path.exists(f"/proc/{pid}"):
            continue  # ours, or the writing IDE is still alive
        try:
            os.unlink(path)
        except OSError:
            pass


class BrowserMcpBridge(QObject):
    """Marshals an MCP tool call (any thread) ONTO the GUI thread and hands
    back a future the caller awaits.

    `op()` / `read()` are awaited from the uvicorn asyncio thread; the
    queued signals deliver to the slots on the GUI thread (this QObject is
    created on the GUI thread). Op results arrive asynchronously via the
    BrowserAutomation callback; reads resolve synchronously.
    """

    # slot, op, payload_json, future(object) — queued to the GUI thread.
    _opRequested = Signal(int, str, str, object)
    # fn(object), future(object) — queued to the GUI thread.
    _readRequested = Signal(object, object)

    def __init__(
        self,
        automation: BrowserAutomation,
        slot_resolver: Callable[[int], int],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._automation = automation
        # window arg (0 = focused, N = 1-based DISPLAY position) → internal
        # slot (or 0 if no such window). Run on the GUI thread inside _run_op,
        # so it reads controller pool state safely.
        self._slot_resolver = slot_resolver
        # Queued: emitted from the uvicorn thread, delivered on the GUI thread
        # where the WebEngineViews + controller state live (project-standards
        # §4 P2 — cross-thread signal, explicit QueuedConnection).
        self._opRequested.connect(self._run_op, Qt.ConnectionType.QueuedConnection)
        self._readRequested.connect(self._run_read, Qt.ConnectionType.QueuedConnection)

    # -- async entry points (called from MCP tool bodies) ----------------
    async def op(self, window: int, op: str, payload: dict) -> dict:
        """Run an automation op on `window` (0 = focused, N = 1-based display
        position) and await the result."""
        future: concurrent.futures.Future = concurrent.futures.Future()
        self._opRequested.emit(window, op, json.dumps(payload), future)
        return await asyncio.wrap_future(future)

    async def read(self, fn: Callable[[], dict]) -> dict:
        """Run `fn()` on the GUI thread (a controller-state read) and await it."""
        future: concurrent.futures.Future = concurrent.futures.Future()
        self._readRequested.emit(fn, future)
        return await asyncio.wrap_future(future)

    # -- GUI-thread slots ------------------------------------------------
    @Slot(int, str, str, object)
    def _run_op(self, window: int, op: str, payload_json: str, future) -> None:
        slot = self._slot_resolver(window)
        if slot <= 0:
            if not future.done():
                future.set_result({"ok": False, "error": "no-window"})
            return
        try:
            payload = json.loads(payload_json)
        except (json.JSONDecodeError, TypeError):
            payload = {}

        def on_result(result: dict) -> None:
            if not future.done():
                future.set_result(result)

        self._automation.request(slot, op, payload, on_result)

    @Slot(object, object)
    def _run_read(self, fn, future) -> None:
        try:
            result = fn()
            if not future.done():
                future.set_result(result)
        except Exception as exc:  # never leave the await hanging
            log.exception("browser MCP read failed")
            if not future.done():
                future.set_result({"ok": False, "error": str(exc)})


class BrowserMcpServer:
    """Owns the FastMCP server + its uvicorn daemon thread.

    Started from `AppController.start()` (unless `SYMMETRIA_IDE_BROWSER_MCP`
    is "0"). Writes a per-launch MCP config file that agent_harness injects
    via `--mcp-config`. Failure is non-fatal — the IDE keeps running with
    Stage-1 (manual) browsing only.
    """

    def __init__(
        self,
        bridge: BrowserMcpBridge,
        windows_reader: Callable[[], dict],
        window_opener: Callable[[str], dict],
    ) -> None:
        self._bridge = bridge
        self._windows_reader = windows_reader
        self._window_opener = window_opener
        self._server = None  # uvicorn.Server
        self._thread: threading.Thread | None = None
        self._port = 0
        self._config_path = ""

    @property
    def port(self) -> int:
        return self._port

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._port}/mcp" if self._port else ""

    @property
    def config_path(self) -> str:
        """Path to the MCP config file agents load via --mcp-config ("" if
        the server isn't running)."""
        return self._config_path

    def start(self) -> None:
        if os.environ.get("SYMMETRIA_IDE_BROWSER_MCP") == "0":
            log.info("browser MCP server disabled (SYMMETRIA_IDE_BROWSER_MCP=0)")
            return
        try:
            self._start()
        except Exception:
            # Non-fatal: Stage-1 manual browsing still works without the server.
            log.exception("browser MCP server failed to start — agent control disabled")
            self._port = 0
            self._config_path = ""

    def _start(self) -> None:
        from mcp.server.fastmcp import FastMCP  # lazy: optional dependency
        import uvicorn

        # Sweep configs left by hard-killed IDEs before writing our own (keeps
        # client discovery from latching onto a dead instance's port).
        reap_orphan_configs()

        # Ephemeral port via bind-probe (each IDE instance gets its own — the
        # multi-instance topology means a fixed port would collide).
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        self._port = probe.getsockname()[1]
        probe.close()

        bridge = self._bridge
        windows_reader = self._windows_reader
        window_opener = self._window_opener
        server = FastMCP(
            SERVER_NAME, host="127.0.0.1", port=self._port, log_level="WARNING"
        )

        @server.tool()
        async def browser_open(url: str = "about:blank") -> dict:
            """Open a NEW browser window at `url` (the autonomous entry point —
            the other tools only drive existing windows). Returns the new
            window's number for use as the `window` arg of the other tools."""
            return await bridge.read(lambda: window_opener(url))

        @server.tool()
        async def browser_navigate(url: str, window: int = 0) -> dict:
            """Navigate an EXISTING browser window to a URL (bare hosts/
            localhost are resolved; use browser_open for a new window).
            `window`: 1-based window number, 0 = the focused one."""
            return await bridge.op(window, "navigate", {"url": url})

        @server.tool()
        async def browser_eval_js(code: str, window: int = 0) -> dict:
            """Evaluate JavaScript in a browser window and return its value.
            The general-purpose primitive — read or manipulate the page."""
            return await bridge.op(window, "eval_js", {"code": code})

        @server.tool()
        async def browser_snapshot(window: int = 0) -> dict:
            """Snapshot the page's interactive elements (links, buttons, form
            fields), each tagged with a `ref` to pass to click/fill."""
            return await bridge.op(window, "snapshot", {})

        @server.tool()
        async def browser_click(ref: str, window: int = 0) -> dict:
            """Click an element by its snapshot `ref`."""
            return await bridge.op(window, "click", {"ref": ref})

        @server.tool()
        async def browser_fill(ref: str, text: str, window: int = 0) -> dict:
            """Fill a form field by its snapshot `ref` with `text`."""
            return await bridge.op(window, "fill", {"ref": ref, "text": text})

        @server.tool()
        async def browser_list_windows() -> dict:
            """List the open browser windows (number, title, url) and which is
            focused. Use the numbers as the `window` argument of the other
            tools."""
            return await bridge.read(windows_reader)

        app = server.streamable_http_app()
        config = uvicorn.Config(
            app, host="127.0.0.1", port=self._port, log_level="warning"
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=lambda: asyncio.run(self._server.serve()),
            daemon=True,
            name="browser-mcp",
        )
        self._thread.start()
        self._write_config()
        log.info("browser MCP server on %s (config %s)", self.url, self._config_path)

    def _write_config(self) -> None:
        """Write the claude-shaped MCP config agents load via --mcp-config.

        `type: "http"` = streamable-HTTP (what `streamable_http_app()` serves).
        Per-launch file in the temp dir, keyed by pid so concurrent IDE
        instances don't clobber each other's config.
        """
        config = {
            "mcpServers": {
                SERVER_NAME: {"type": "http", "url": self.url},
            }
        }
        path = os.path.join(
            tempfile.gettempdir(), f"{_CONFIG_PREFIX}{os.getpid()}.json"
        )
        with open(path, "w") as handle:
            json.dump(config, handle)
        self._config_path = path

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._config_path:
            try:
                os.unlink(self._config_path)
            except OSError:
                pass
