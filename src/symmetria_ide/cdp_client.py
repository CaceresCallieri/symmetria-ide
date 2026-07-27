"""Minimal Chrome DevTools Protocol client for the IDE-owned Chrome.

Scope is deliberately tiny. Agents drive pages through off-the-shelf
`chrome-devtools-mcp`; the IDE only needs the few operations that MCP cannot
do for us — allocate a real browser WINDOW and know when its title/url change
or when the user closes it.

**Why a websocket instead of Chrome's HTTP endpoints:** `PUT /json/new` opens a
TAB, and there is no HTTP form that requests a new window. `Target.createTarget`
takes `newWindow: true`, and it is websocket-only. The HTTP endpoint is still
used once, to discover the browser-level websocket URL.

**Why QtWebSockets rather than `websockets`/`aiohttp`:** it ships with PySide6
(no new dependency) and, more importantly, it is driven by the Qt event loop —
so this whole client lives on the GUI thread with no worker thread, no
cross-thread marshaling, and no exposure to the Python-3.14 cyclic-GC race that
governs every worker we do run (gotcha #10).

Connection lifetime is tied to Chrome, not to the IDE: Chrome is spawned lazily
and can be killed by the user, so `connect()` is idempotent and re-callable and
every send while disconnected fails fast rather than queueing.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

from PySide6.QtCore import QObject, QTimer, QUrl, Signal, Slot
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWebSockets import QWebSocket

log = logging.getLogger(__name__)

# Chrome's debug port is not listening the instant the process exists, so the
# first attach after a cold start always loses the race. We retry on a timer
# instead of leaving the client detached until the next browser operation:
# without this, a session that opens exactly one window would never attach at
# all, and its title/url would stay frozen at whatever we guessed.
_RETRY_INTERVAL_MS = 750
_MAX_ATTACH_ATTEMPTS = 10  # ~7.5s, comfortably past a cold Chrome start

type CommandCallback = Callable[[dict], None]


class CdpClient(QObject):
    """Browser-level CDP session over a single websocket.

    Signals are the IDE-facing surface; `send` is fire-and-callback. All of it
    runs on the GUI thread.
    """

    #: Emitted when an ESTABLISHED session drops — Chrome exited or was killed.
    #: The owner treats it as "every window we knew about is gone".
    disconnected = Signal()
    #: (target_id, url, title) whenever a page target appears or changes.
    targetUpdated = Signal(str, str, str)
    #: (target_id) when a page target goes away (window/tab closed).
    targetGone = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._socket = QWebSocket()
        self._socket.connected.connect(self._on_connected)
        self._socket.disconnected.connect(self._on_disconnected)
        self._socket.textMessageReceived.connect(self._on_message)
        # Without this, a failed websocket handshake is SILENT: `disconnected`
        # does not fire for a socket that never connected, so nothing would
        # consume the retry budget and CDP would stay detached for the rest of
        # the session with no log line explaining why.
        self._socket.errorOccurred.connect(self._on_socket_error)
        self._network = QNetworkAccessManager(self)
        self._next_id = 1
        self._pending: dict[int, CommandCallback] = {}
        self._port = 0
        self._connecting = False
        self._connected = False
        self._attempts = 0
        self._closed = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def connect_to(self, port: int) -> None:
        """Discover the browser websocket on `port` and open it.

        Idempotent: repeated calls while connected or mid-connect are ignored,
        so callers can invoke this on every browser operation as a cheap
        "make sure we're attached" without tracking state themselves.
        """
        if self._connected or self._connecting or port <= 0:
            return
        self._port = port
        self._attempts = 0
        self._closed = False
        self._attempt_attach()

    def _attempt_attach(self) -> None:
        if self._closed:
            return
        self._connecting = True
        self._attempts += 1
        request = QNetworkRequest(QUrl(f"http://127.0.0.1:{self._port}/json/version"))
        reply = self._network.get(request)
        # No context-object argument here: PySide6's `connect` does not accept
        # the C++ `connect(sender, signal, context, functor)` form for a
        # lambda ("Expected signal or callable"), so §4 P1's context-QObject
        # advice is unavailable on this call. Lifetime is covered structurally
        # instead — the QNetworkAccessManager is parented to `self`, so its
        # replies are aborted when we die — and `_on_version_reply` bails on
        # `_closed` for the ordinary teardown case.
        reply.finished.connect(lambda: self._on_version_reply(reply))

    def _retry_or_give_up(self, reason: str) -> None:
        self._connecting = False
        if self._closed:
            return
        if self._attempts >= _MAX_ATTACH_ATTEMPTS:
            log.warning("cdp: giving up attaching to Chrome (%s)", reason)
            return
        QTimer.singleShot(_RETRY_INTERVAL_MS, self, self._attempt_attach)

    def close(self) -> None:
        """Drop the session (IDE shutdown / Chrome kill).

        `_closed` is what actually makes this stick: a `/json/version` request
        already in flight would otherwise still open a websocket from its
        completion handler, reviving the session after teardown.
        """
        self._closed = True
        self._pending.clear()
        self._connecting = False
        self._socket.close()

    # -- commands -------------------------------------------------------

    def send(
        self,
        method: str,
        params: dict | None = None,
        callback: CommandCallback | None = None,
    ) -> bool:
        """Send one CDP command. Returns False when not connected.

        The caller's `callback` receives the `result` object (or a dict with an
        `error` key). A False return means the command never left — callers
        must treat that as the failure it is rather than waiting for a callback
        that will never fire.
        """
        if not self._connected:
            log.info("cdp: dropping %s — not connected", method)
            return False
        message_id = self._next_id
        self._next_id += 1
        if callback is not None:
            self._pending[message_id] = callback
        payload = {"id": message_id, "method": method, "params": params or {}}
        self._socket.sendTextMessage(json.dumps(payload))
        return True

    def create_window(self, url: str, callback: CommandCallback | None = None) -> bool:
        """Open a NEW browser window (not a tab) at `url`.

        `newWindow` is the reason this client exists — see the module
        docstring. `newTab` is passed alongside because Chrome treats a
        window request as a tab request when the browser has no window yet.
        """
        return self.send(
            "Target.createTarget",
            {"url": url or "about:blank", "newWindow": True, "newTab": True},
            callback,
        )

    def close_target(
        self, target_id: str, callback: CommandCallback | None = None
    ) -> bool:
        """Close a target (our window) by id."""
        if not target_id:
            return False
        return self.send("Target.closeTarget", {"targetId": target_id}, callback)

    # -- plumbing -------------------------------------------------------

    def _on_version_reply(self, reply: QNetworkReply) -> None:
        """Read `webSocketDebuggerUrl` out of /json/version and open it."""
        try:
            if reply.error() != QNetworkReply.NetworkError.NoError:
                # Expected while Chrome is still starting — retry, don't warn.
                self._retry_or_give_up(reply.errorString())
                return
            payload = json.loads(bytes(reply.readAll().data()).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            self._retry_or_give_up(f"unparseable /json/version: {exc}")
            return
        finally:
            reply.deleteLater()
        ws_url = str(payload.get("webSocketDebuggerUrl", ""))
        if not ws_url:
            self._retry_or_give_up("no webSocketDebuggerUrl")
            return
        self._socket.open(QUrl(ws_url))

    @Slot()
    def _on_socket_error(self) -> None:
        """The websocket itself failed (handshake refused, Chrome mid-start).

        Separate from `_on_disconnected`, which Qt does NOT emit for a socket
        that never reached the connected state.
        """
        if self._connected:
            return  # a live session dropping is _on_disconnected's business
        self._retry_or_give_up(self._socket.errorString())

    @Slot()
    def _on_connected(self) -> None:
        self._connecting = False
        self._connected = True
        log.info("cdp: attached to Chrome on port %d", self._port)
        # Discovery is what turns this from a command channel into a live
        # inventory: without it we would learn nothing about windows the user
        # opens or closes by hand, and the registry would drift immediately.
        self.send("Target.setDiscoverTargets", {"discover": True})

    @Slot()
    def _on_disconnected(self) -> None:
        was_connected = self._connected
        self._connected = False
        self._connecting = False
        self._pending.clear()
        if was_connected:
            log.info("cdp: detached from Chrome")
            self.disconnected.emit()

    @Slot(str)
    def _on_message(self, message: str) -> None:
        try:
            payload = json.loads(message)
        except ValueError:
            log.warning("cdp: unparseable frame")
            return
        message_id = payload.get("id")
        if message_id is not None:
            error = payload.get("error")
            if error is not None:
                # Without this the failure is invisible: the callback sees a
                # payload with no targetId, shrugs, and the agent is told its
                # window opened fine.
                log.warning("cdp: command %s failed: %s", message_id, error)
            callback = self._pending.pop(message_id, None)
            if callback is not None:
                result = payload.get("result")
                callback(result if isinstance(result, dict) else payload)
            return
        self._dispatch_event(
            str(payload.get("method", "")), payload.get("params") or {}
        )

    def _dispatch_event(self, method: str, params: dict) -> None:
        if method in ("Target.targetCreated", "Target.targetInfoChanged"):
            info = params.get("targetInfo") or {}
            # Chrome reports its own internals (service workers, the omnibox
            # popup, extension backgrounds) as targets too. Only pages are
            # windows a user can look at.
            if info.get("type") != "page":
                return
            self.targetUpdated.emit(
                str(info.get("targetId", "")),
                str(info.get("url", "")),
                str(info.get("title", "")),
            )
        elif method == "Target.targetDestroyed":
            self.targetGone.emit(str(params.get("targetId", "")))
