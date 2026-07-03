"""IDE-hosted browser MCP server (Phase 4 Stage 2b / Stage 4).

Exposes the embedded browser's WINDOW MANAGEMENT to spawned agents as MCP
tools over local streamable-HTTP — `browser_open` (allocate a visible pooled
WebEngineView), `browser_list_windows` (the url↔window correlation table), and
`browser_request_attention` (light the notification dot on the agent's chip
globe so the user knows to come look).
Page DRIVING (navigate, eval, screenshot, network, console, performance) is
delegated to the off-the-shelf chrome-devtools-mcp, injected per-agent and
pointed at the embedded view's CDP endpoint (see agent_config_path +
QTWEBENGINE_REMOTE_DEBUGGING in app.run()). Either way an agent drives the
IDE's OWN browser window instead of launching its own Chromium — the
containment win (no escaped Hyprland window). Agents discover this server via
per-harness `--mcp-config` injection (Stage 2c, agent_harness.py).

Topology / threading:

    agent (separate process)
      │  MCP over http://127.0.0.1:<port>/mcp
      ▼
    FastMCP + uvicorn          ← daemon thread, own asyncio loop
      │  async tool body: await bridge.read(...)
      ▼
    BrowserMcpBridge           ← queued Qt signal, marshals ONTO the GUI thread
      │  fn()  → reads controller pool state, resolves the Future
      ▼
    bridge.read() returns      ← asyncio.wrap_future completes the await

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
import shutil
import socket
import tempfile
import threading
from contextlib import asynccontextmanager
from typing import Callable

from PySide6.QtCore import QObject, Qt, Signal, Slot

log = logging.getLogger(__name__)

# Server identity in the agents' MCP config.
SERVER_NAME = "symmetria-browser"

# Per-launch MCP config files are named <prefix><pid>.json (the shared
# discovery config) or <prefix><pid>_<slot>.json (a per-agent config) in the
# temp dir. Both encode the writing IDE's pid as the LEADING token so
# reap_orphan_configs can clean either after a hard-kill.
_CONFIG_PREFIX = "symmetria-browser-mcp-"

# Header an agent's per-agent config stamps onto every browser tool request so
# the tool body can attribute the call to the spawning agent. The value is the
# agent's `<ide_pid>_<slot>` id — the SAME key the bridge/hooks use, so the IDE
# maps it straight onto the agent's chip slot. Read case-insensitively
# server-side (Starlette Headers), so the exact casing here is cosmetic.
_AGENT_HEADER = "X-Symmetria-Agent"

# Off-the-shelf Chrome DevTools MCP, injected per-agent alongside our own
# server (see agent_config_path). Pointed at the embedded QtWebEngine's CDP
# endpoint, it gives agents Google's full browser-automation suite
# (screenshots, network, console, performance, Lighthouse) driving the SAME
# contained view — superseding our hand-written op-tools. Pinned for
# reproducibility (mirrors the agent-SDK pin); bump manually after vetting.
_CHROME_DEVTOOLS_MCP_VERSION = "1.3.0"


def _calling_agent_id(server) -> str:
    """The `X-Symmetria-Agent` id of the in-flight MCP request, or "".

    Each spawned agent loads a per-agent config (`agent_config_path`) that
    stamps this header; the tool body reads it back to know which agent is
    driving the browser. Defensive: returns "" when there is no HTTP request
    context (non-HTTP transport, or an untagged caller — the bench harness, or
    an opencode agent that has no per-launch MCP flag yet).
    """
    try:
        request = server.get_context().request_context.request
    except (ValueError, AttributeError):
        # ValueError: no active request context; AttributeError: a transport
        # whose request_context carries no `.request` (non-HTTP).
        return ""
    if request is None:
        return ""
    try:
        # Header is primary (an unknown header can't break the MCP connection,
        # whereas a query-string URL conceivably could, so the config writes
        # the header form). NO config currently writes the `?agent=` form — the
        # query-param branch is dormant, present only so that IF a client turns
        # out not to forward the header, switching `agent_config_path` to the
        # query-param URL is a one-line change the server already accepts.
        agent_id = request.headers.get(_AGENT_HEADER, "")
        if not agent_id:
            agent_id = request.query_params.get("agent", "")
        return agent_id or ""
    except (AttributeError, TypeError):
        return ""


def reap_orphan_configs() -> None:
    """Remove browser-MCP config files left by dead IDE instances.

    The IDE unlinks its own config on graceful shutdown, but a hard-kill
    (SIGKILL / crash) leaves it behind, which confuses external clients that
    discover the server via these files (e.g. bench/browser_mcp_live.py). The
    filename encodes the writing IDE's pid, so a config whose pid is no longer
    alive is safe to remove. Mirrors `_reap_orphan_nvim_sockets` in app.py.
    Best-effort: per-file errors are ignored; the current pid is never touched.
    (Liveness is a `/proc/<pid>` check, so the rare case of pid reuse by an
    unrelated process keeps a stale config — harmless: at worst a client
    connects to a refused port and retries, never to a wrong server.)
    """
    pattern = os.path.join(tempfile.gettempdir(), f"{_CONFIG_PREFIX}*.json")
    for path in glob.glob(pattern):
        stem = os.path.basename(path)[len(_CONFIG_PREFIX) : -len(".json")]
        try:
            # Leading token before any "_" is the pid: "<pid>" (shared config)
            # or "<pid>_<slot>" (per-agent config) both yield the same pid.
            pid = int(stem.split("_")[0])
        except (ValueError, IndexError):
            continue  # not a pid-named config — leave it alone
        if pid == os.getpid() or os.path.exists(f"/proc/{pid}"):
            continue  # ours, or the writing IDE is still alive
        try:
            os.unlink(path)
        except OSError:
            pass


def _browser_gated_body(
    gate: Callable[[], bool] | None, fn: Callable[[], dict]
) -> dict:
    """Run a browser tool body behind the per-project call-time gate.

    Since the server config became always-injected (agent coordination rides
    it), the `.symmetria/ide.json` browser opt-in is enforced HERE for the
    three browser tools instead of at config-injection time (chrome-devtools
    injection keeps the config-level gate — that's the costly Node process).
    A None gate (tests / permissive embedder) means ungated. Runs inside the
    GUI-thread closure the bridge marshals, where controller state lives.
    """
    if gate is not None and not gate():
        return {
            "ok": False,
            "error": "browser tools disabled for this project — enable via "
            "Ctrl+Shift+M (writes .symmetria/ide.json browser_agents)",
        }
    return fn()


def _build_combined_asgi_app(server):
    """Combine the FastMCP server's two transports into ONE Starlette app so a
    single uvicorn port serves both: streamable-HTTP at /mcp for claude, SSE at
    /sse (+ /messages/) for opencode — whose remote MCP speaks SSE, not
    streamable-HTTP (verified spike 2026-07-01; see the reference/agent-sdk
    memory). Both transports come off the SAME FastMCP instance, so they expose
    the same tools; the parent app just spreads both sub-apps' routes.

    The streamable app's lifespan is MANDATORY (it starts the StreamableHTTP
    session manager — without it /mcp raises "Task group is not initialized");
    the SSE app's is composed too for safety. Passing the parent `_app` into
    both `lifespan_context(_app)` calls means any `app.state` a sub-app sets
    lands on the parent that incoming requests actually see via `request.app`.

    Raises RuntimeError if the two sub-apps' route paths collide — a future
    `mcp` version could move a mount path (e.g. streamable → "/"), which would
    otherwise silently shadow one transport; fail loud at startup instead.
    """
    from starlette.applications import Starlette  # dep of mcp/fastmcp

    streamable_app = server.streamable_http_app()
    sse_app = server.sse_app()

    streamable_paths = {getattr(r, "path", None) for r in streamable_app.routes}
    sse_paths = {getattr(r, "path", None) for r in sse_app.routes}
    collision = (streamable_paths & sse_paths) - {None}
    if collision:
        raise RuntimeError(
            f"browser MCP transport route collision on {sorted(collision)} — a "
            "dependency change moved a mount path; one transport would shadow "
            "the other"
        )

    @asynccontextmanager
    async def _lifespan(_app):
        async with streamable_app.router.lifespan_context(_app):
            async with sse_app.router.lifespan_context(_app):
                yield

    return Starlette(
        routes=[*streamable_app.routes, *sse_app.routes],
        lifespan=_lifespan,
    )


class BrowserMcpBridge(QObject):
    """Marshals an MCP tool call (any thread) ONTO the GUI thread and hands
    back a future the caller awaits.

    `read()` is awaited from the uvicorn asyncio thread; the queued signal
    delivers to `_run_read` on the GUI thread (this QObject is created on the
    GUI thread) where the controller pool state lives, and resolves
    synchronously. The name is "read", but the fn is any GUI-thread closure:
    browser_open and browser_list_windows read pool state, while
    browser_request_attention WRITES it (sets the attention dot) — all three
    marshal through here identically. Page DRIVING (navigate, eval, screenshot,
    …) is delegated to chrome-devtools-mcp over the embedded CDP endpoint, so
    the former automation op-path (BrowserAutomation + runJavaScript) is gone.
    """

    # fn(object), future(object) — queued to the GUI thread.
    _readRequested = Signal(object, object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        # Queued: emitted from the uvicorn thread, delivered on the GUI thread
        # where the controller pool state lives (project-standards §4 P2 —
        # cross-thread signal, explicit QueuedConnection).
        self._readRequested.connect(self._run_read, Qt.ConnectionType.QueuedConnection)

    # -- async entry point (called from MCP tool bodies) -----------------
    async def read(self, fn: Callable[[], dict]) -> dict:
        """Run `fn()` on the GUI thread (a controller-state read) and await it."""
        future: concurrent.futures.Future = concurrent.futures.Future()
        self._readRequested.emit(fn, future)
        return await asyncio.wrap_future(future)

    # -- GUI-thread slot -------------------------------------------------
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
        window_opener: Callable[[str, str], dict],
        attention_setter: Callable[[str, str], dict],
        wait_registrar: Callable[[int, str, str], dict] | None = None,
        browser_gate: Callable[[], bool] | None = None,
    ) -> None:
        self._bridge = bridge
        self._windows_reader = windows_reader
        # (url, agent_id) -> dict: the opener records window ownership for the
        # calling agent, so its signature carries the agent id (vs the bare
        # url-only Stage-2 form).
        self._window_opener = window_opener
        # (agent_id, message) -> dict: raises the attention badge on the calling
        # agent's browser globe (browser_request_attention). Same agent-id seam
        # as the opener — the IDE maps it onto the chip slot.
        self._attention_setter = attention_setter
        # (display_num, registrant_agent_id, note) -> dict: the wait_for_agent
        # tool body (agent coordination v1 — see agent_coordination.py). Rides
        # this server because it's a generic FastMCP host, not browser-specific;
        # coordination is UNGATED (unlike the browser tools below).
        self._wait_registrar = wait_registrar
        # () -> bool: the per-project browser opt-in, read AT CALL TIME on the
        # GUI thread. Since coordination made the server config always-injected,
        # the `.symmetria/ide.json` gate moved from config-injection to here for
        # the three browser tools (chrome-devtools injection stays config-level).
        self._browser_gate = browser_gate
        self._server = None  # uvicorn.Server
        self._thread: threading.Thread | None = None  # uvicorn serve thread
        # Starter thread that runs _start() (the ~1s FastMCP+uvicorn import +
        # build) OFF the GUI thread. Distinct from _thread (uvicorn) so the
        # idempotency guard in start() and the uvicorn thread don't alias.
        self._start_thread: threading.Thread | None = None
        self._sock: socket.socket | None = None  # held bound until uvicorn owns it
        self._port = 0
        self._config_path = ""
        # Per-agent config files written by agent_config_path(), unlinked on
        # stop() (orphans from a hard-kill are swept by reap_orphan_configs).
        self._agent_config_paths: set[str] = set()

    @property
    def port(self) -> int:
        return self._port

    @property
    def url(self) -> str:
        """Streamable-HTTP endpoint (claude's `type: http` config points here)."""
        return f"http://127.0.0.1:{self._port}/mcp" if self._port else ""

    @property
    def sse_url(self) -> str:
        """SSE endpoint (opencode's `type: remote` config points here).

        opencode's remote MCP speaks SSE, not streamable-HTTP — so its config
        must target this URL, not `url`. The server mounts BOTH transports on
        the one port (see _start). See the reference/agent-sdk memory.
        """
        return f"http://127.0.0.1:{self._port}/sse" if self._port else ""

    @property
    def config_path(self) -> str:
        """Path to the MCP config file agents load via --mcp-config ("" if
        the server isn't running)."""
        return self._config_path

    def start(self) -> None:
        if os.environ.get("SYMMETRIA_IDE_BROWSER_MCP") == "0":
            log.info("browser MCP server disabled (SYMMETRIA_IDE_BROWSER_MCP=0)")
            return
        # Idempotent: the per-project gate (AppController._refresh_project_
        # browser_enabled) calls this on every displayedRootChanged while the
        # project has opted in — only the FIRST call spawns the starter thread.
        if self._start_thread is not None:
            return
        # Run the build OFF the GUI thread. The lazy `from mcp.server.fastmcp
        # import FastMCP` + `import uvicorn` alone is ~1s (the whole
        # FastMCP→uvicorn→starlette→pydantic stack); running it inline used to
        # block the entire startup path before app.exec() — the single largest
        # phase in the launch waterfall. Agents only read _port/_config_path on
        # a user-initiated spawn (long after launch), and agent_config_path()
        # returns "" while _port is still 0, so the brief not-yet-ready window
        # degrades gracefully (no browser MCP for that one spawn). See CLAUDE.md
        # "Browser MCP server" + "The browser panes" Stage 5 startup-perf note.
        self._start_thread = threading.Thread(
            target=self._start_guarded, daemon=True, name="browser-mcp-start"
        )
        self._start_thread.start()

    def _start_guarded(self) -> None:
        """Starter-thread body: build + launch the server, swallowing failure.

        Non-fatal on error — Stage-1 manual browsing still works without the
        server; _port stays 0 so agent_config_path() returns "" for every
        spawn until (if ever) a later start succeeds."""
        try:
            self._start()
        except Exception:
            log.exception("browser MCP server failed to start — agent control disabled")
            self._port = 0
            self._config_path = ""
            # Release the probe socket if the failure landed AFTER bind but
            # before uvicorn took ownership — otherwise the ephemeral port
            # stays held for the process lifetime.
            if self._sock is not None:
                self._sock.close()
                self._sock = None
            # Deliberately do NOT reset _start_thread: a failed start is
            # try-once for the session (matches the pre-deferral behaviour,
            # which called start() exactly once). Resetting it would let the
            # per-project gate re-attempt on EVERY displayedRootChanged — and
            # the dominant failure (python-mcp not installed) fails identically
            # each time, so that would only spam log.exception + spawn churn.
            # Recovery from a genuine transient failure is an IDE restart.

    def _start(self) -> None:
        from mcp.server.fastmcp import FastMCP  # lazy: optional dependency
        import uvicorn

        # Sweep configs left by hard-killed IDEs before writing our own (keeps
        # client discovery from latching onto a dead instance's port).
        reap_orphan_configs()

        # Ephemeral port via bind-probe, HELD OPEN and handed to uvicorn (each
        # IDE instance gets its own — the multi-instance topology rules out a
        # fixed port). Closing the probe before uvicorn rebinds would open a
        # TOCTOU window: another process/IDE could grab the port, uvicorn would
        # fail to bind on its daemon thread (outside this try/except), and the
        # config we write would point at a dead port. uvicorn.serve(sockets=[…])
        # takes the pre-bound socket, so the port is reserved continuously.
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._port = self._sock.getsockname()[1]

        bridge = self._bridge
        windows_reader = self._windows_reader
        window_opener = self._window_opener
        attention_setter = self._attention_setter
        wait_registrar = self._wait_registrar
        browser_gate = self._browser_gate
        server = FastMCP(
            SERVER_NAME, host="127.0.0.1", port=self._port, log_level="WARNING"
        )

        # Call-time per-project gate for the BROWSER tools only (wait_for_agent
        # is ungated). The gate callable reads controller state, so it must run
        # inside the GUI-thread closure the bridge marshals — wrap the tool's
        # real body rather than checking on the uvicorn thread.
        def _browser_gated(fn):
            return lambda: _browser_gated_body(browser_gate, fn)

        @server.tool()
        async def browser_open(url: str = "about:blank") -> dict:
            """Open a NEW visible browser window at `url`. This is the ONLY way
            to create a window the user can see — the chrome-devtools-mcp tools
            drive EXISTING pages but cannot allocate one here. Returns
            {window, url, title}: `window` is the 1-based number for
            browser_list_windows; `url`/`title` identify the page.

            To then inspect/drive it with the chrome-devtools-mcp tools
            (take_screenshot, list_network_requests, navigate_page, …), call
            chrome-devtools-mcp `list_pages` + `select_page` to select the page
            whose url matches this window's url, THEN drive it. The committed
            url settles after the page loads — read browser_list_windows for the
            current url/title if it differs from what you passed."""
            agent_id = _calling_agent_id(server)
            return await bridge.read(
                _browser_gated(lambda: window_opener(url, agent_id))
            )

        @server.tool()
        async def browser_list_windows() -> dict:
            """List the open browser windows (number, title, url) and which is
            focused. This is the correlation table for the chrome-devtools-mcp
            tools: match a window's url here to a chrome-devtools-mcp page
            (list_pages) and select_page it before driving."""
            return await bridge.read(_browser_gated(windows_reader))

        @server.tool()
        async def browser_request_attention(message: str = "") -> dict:
            """Signal the user to look at THIS agent's browser window.

            Lights a notification dot on your agent's browser globe in the IDE
            top bar — use it when you've loaded or found something in the
            browser the user should see (a result page, a diff, a preview).
            The user does NOT get pulled to the browser automatically; this dot
            is how they know to come look. Requires that you have an open window
            (call browser_open first). `message` is an optional short reason
            (carried for a future tooltip/notification; the dot shows either
            way). Returns {ok, ...}; {ok: false} with an error if you own no
            window or the caller is untagged."""
            agent_id = _calling_agent_id(server)
            return await bridge.read(
                _browser_gated(lambda: attention_setter(agent_id, message))
            )

        if wait_registrar is not None:

            @server.tool()
            async def wait_for_agent(agent: int, note: str = "") -> dict:
                """Register a dependency on another agent in this IDE: when
                that agent finishes its current task, the IDE will verify its
                transcript and then send YOU a go-ahead message to continue.

                `agent` is the other agent's DISPLAY number — the number shown
                on its chip in the IDE top bar (what the user calls "agent 3").
                `note` is an optional one-line description of what you are
                waiting for (e.g. "the API refactor is merged"); it is shown to
                the verification judge and echoed back in the go-ahead.

                After this returns ok, END YOUR TURN and do nothing — do NOT
                poll or busy-wait. The IDE injects a new prompt into you when
                the other agent is done (or if its result needs the user's
                attention, in which case you'll be told to hold). Returns
                {ok, status: "armed"|"evaluating", ...}; {ok: false, error}
                if the target doesn't exist or is yourself."""
                agent_id = _calling_agent_id(server)
                return await bridge.read(lambda: wait_registrar(agent, agent_id, note))

        # Serve BOTH transports on the one port from the same FastMCP instance:
        # /mcp (streamable-HTTP, claude) + /sse (SSE, opencode). See
        # _build_combined_asgi_app for the rationale + the route-collision guard.
        app = _build_combined_asgi_app(server)
        config = uvicorn.Config(
            app, host="127.0.0.1", port=self._port, log_level="warning"
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=lambda: asyncio.run(self._server.serve(sockets=[self._sock])),
            daemon=True,
            name="browser-mcp",
        )
        self._thread.start()
        self._write_config()
        log.info("browser MCP server on %s (config %s)", self.url, self._config_path)

    def _write_config(self) -> None:
        """Write the shared per-launch MCP config (the DISCOVERY anchor).

        `type: "http"` = streamable-HTTP (what `streamable_http_app()` serves).
        Per-launch file in the temp dir, keyed by pid so concurrent IDE
        instances don't clobber each other's config.

        Since Stage 3 this is NOT what spawned agents load — they get a
        per-agent config from `agent_config_path` (which adds the identity
        header). This headerless shared config is retained only so external
        clients can DISCOVER the server's URL by pid (e.g.
        `bench/browser_mcp_live.py`), and as the reap anchor; agents never
        inject it.
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

    def _chrome_devtools_argv(self, agent_id: str) -> list[str] | None:
        """The `npx chrome-devtools-mcp` invocation for the embedded view's CDP
        endpoint, or None when CDP is off / npx is missing.

        The ONE place the version pin, the CDP-port read, and the npx-presence
        gate live — shared by BOTH per-agent config builders (claude's
        agent_config_path splits it into command+args; opencode's
        agent_config_content uses the full argv as `command`). CDP is enabled in
        app.run() via QTWEBENGINE_REMOTE_DEBUGGING (the single source of truth
        for the port, read back here). chrome-devtools-mcp supersedes our old
        navigate/eval/perf/snapshot/click/fill tools with Google's suite while
        driving the SAME contained view; the agent still allocates visible
        windows via our browser_open (chrome-devtools-mcp can't pool a
        WebEngineView) then drives them by select_page. See CLAUDE.md "The
        browser panes".
        """
        cdp_port = os.environ.get("QTWEBENGINE_REMOTE_DEBUGGING", "")
        if not cdp_port:
            return None  # remote debugging off → agent gets only our window tools
        if not shutil.which("npx"):
            # CDP live but node/npx missing: omit the entry so the agent cleanly
            # gets only our window tools (a broken stdio server would otherwise
            # just fail at the agent's MCP client). Warn so the missing dep is
            # visible rather than a silent capability loss.
            log.warning(
                "npx not on PATH — agent %s gets no chrome-devtools-mcp browser "
                "tools (only browser_open/browser_list_windows); install node",
                agent_id,
            )
            return None
        return [
            "npx",
            "-y",
            f"chrome-devtools-mcp@{_CHROME_DEVTOOLS_MCP_VERSION}",
            "--browserUrl",
            f"http://127.0.0.1:{cdp_port}",
        ]

    def agent_config_path(self, agent_id: str, browser_enabled: bool = False) -> str:
        """Write (idempotently) a per-agent MCP config and return its path.

        The config points at this server's URL but adds a `headers` entry
        stamping the agent's `<ide_pid>_<slot>` id, so the browser tool bodies
        can attribute each call back to the spawning agent (the basis for the
        agent-bubble browser indicator). `agent_spawn_argv` passes the returned
        path via `--mcp-config`.

        Since agent coordination (wait_for_agent) rides this server, the
        `symmetria-browser` entry is ALWAYS injected (server up + agent_id
        nonempty) — coordination must Just Work in every project.
        `browser_enabled` (the PER-PROJECT `.symmetria/ide.json` marker — see
        project_browser_marker) now gates only the COSTLY part: the
        `chrome-devtools` entry, i.e. the per-agent `npx chrome-devtools-mcp`
        Node process (the RAM the gate saves). The three browser tools on our
        own server are additionally gated at CALL time via the `browser_gate`
        ctor callable, so a non-opted-in project's agent sees them but gets a
        clear disabled-error. The decision is harness-agnostic — made here,
        before `spawn_argv` applies the harness-specific `--mcp-config` flag.

        Returns "" when the server isn't running or `agent_id` is empty —
        both mean "no per-agent config", which `spawn_argv` treats as no
        `--mcp-config` flag (the agent then gets no IDE MCP tools at all).
        """
        if not self._port or not agent_id:
            return ""
        config = {
            "mcpServers": {
                SERVER_NAME: {
                    "type": "http",
                    "url": self.url,
                    "headers": {_AGENT_HEADER: agent_id},
                },
            }
        }
        # Add the off-the-shelf Chrome DevTools MCP (npx stdio) pointed at the
        # embedded view's CDP endpoint — see _chrome_devtools_argv for the
        # rationale + the CDP/npx gating. claude's schema splits the invocation
        # into `command` (executable) + `args` (the rest). Per-project gated:
        # this is the entry that spawns a Node process per agent.
        argv = self._chrome_devtools_argv(agent_id) if browser_enabled else None
        if argv:
            config["mcpServers"]["chrome-devtools"] = {
                "command": argv[0],
                "args": argv[1:],
            }
        # agent_id is already "<pid>_<slot>", so the file is
        # <prefix><pid>_<slot>.json — leading-pid-parseable by the reaper.
        path = os.path.join(tempfile.gettempdir(), f"{_CONFIG_PREFIX}{agent_id}.json")
        try:
            with open(path, "w") as handle:
                json.dump(config, handle)
        except OSError:
            log.exception(
                "failed to write per-agent browser MCP config for %s", agent_id
            )
            return ""
        self._agent_config_paths.add(path)
        return path

    def agent_config_content(self, agent_id: str, browser_enabled: bool = False) -> str:
        """Inline `OPENCODE_CONFIG_CONTENT` JSON for an opencode agent.

        The opencode counterpart to agent_config_path: opencode has no
        `--mcp-config` flag, so its browser MCP is injected as inline JSON via
        the `OPENCODE_CONFIG_CONTENT` env var (spawn_argv, gated on the harness's
        `mcp_config_env`), which DEEP-MERGES over the project's opencode.json at
        highest precedence — adding our servers without clobbering the project's.

        Mirrors agent_config_path's two servers in opencode's `mcp`-key schema:
        `symmetria-browser` as a REMOTE server on the /sse URL (opencode remote
        MCP speaks SSE, NOT streamable-HTTP — hence sse_url, not url), plus
        `chrome-devtools` as a LOCAL (stdio) server. The X-Symmetria-Agent
        header rides EVERY request over SSE (verified spike 2026-07-01), so the
        chip-globe attribution works for opencode too — see the reference/
        agent-sdk memory.

        Returns "" under the SAME conditions as agent_config_path (server not
        up, or empty id — `browser_enabled` gates only the chrome-devtools
        entry, mirroring the claude builder); spawn_argv then emits no env
        var. No file is written (inline env var), so — unlike
        agent_config_path — there is nothing to track or reap.
        """
        if not self._port or not agent_id:
            return ""
        mcp: dict = {
            SERVER_NAME: {
                "type": "remote",
                "url": self.sse_url,
                "enabled": True,
                "headers": {_AGENT_HEADER: agent_id},
            }
        }
        argv = self._chrome_devtools_argv(agent_id) if browser_enabled else None
        if argv:
            # opencode's local (stdio) shape: `command` is the FULL argv array
            # (vs claude's command+args split in agent_config_path).
            mcp["chrome-devtools"] = {
                "type": "local",
                "command": argv,
                "enabled": True,
            }
        return json.dumps({"mcp": mcp})

    def stop(self) -> None:
        # Called only at app shutdown (aboutToQuit). `_server` may still be None
        # here if the background starter thread hasn't finished building — in
        # that case the daemon starter + uvicorn threads are reaped by process
        # exit, so we rely on that rather than racing the in-flight build.
        if self._server is not None:
            self._server.should_exit = True
        paths = [self._config_path] if self._config_path else []
        paths.extend(self._agent_config_paths)
        for path in paths:
            try:
                os.unlink(path)
            except OSError:
                pass
        self._agent_config_paths.clear()
