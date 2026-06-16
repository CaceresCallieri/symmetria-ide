"""Browser automation layer (Phase 4 Stage 2a) — drives the embedded
WebEngineViews via QML-delivered `runJavaScript`.

WHY QML-DELIVERED (not Python-driven): a QML-owned `QQuickWebEngineView`
is not drivable from Python. `findChild` hands back a bare `QObject`, and
`view.runJavaScript(code, callback)` then fails to convert the Python
callable — `_pythonToCppCopy: Cannot copy-convert (function) to C++` — the
same "not Python-wrappable" wall `KSession` hits (see CLAUDE.md "The
terminal panes"). So Python emits an `automationRequested(request_id,
slot, op, payload)` signal; the QML `BrowserSurface` runs the op on the
target view and calls `on_result(request_id, result_json)` back. This is
the SAME delivery split as STT injection: Python validates + bookkeeps,
QML is the only side that touches the widget.

This module is the TRANSPORT-AGNOSTIC core. The IDE-hosted MCP server
(Stage 2b) wraps `request()` with an async resolver that completes a
future; tests drive it with a plain callback. The op vocabulary is the
same either way.

Threading: `request()` and `on_result()` are GUI-THREAD ONLY — the signal
emit must run where the WebEngineViews live, and `_pending` is touched
from both, so keeping both on the GUI thread avoids a lock. Stage 2b runs
its MCP handlers on a separate asyncio thread and MUST marshal `request()`
onto the GUI thread (a queued signal / `QMetaObject.invokeMethod`) before
calling it — the inverse of gotcha #1 (which marshals pynvim RPC OFF the
GUI thread; here we marshal ONTO it).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Callable

from PySide6.QtCore import QObject, Signal, Slot

log = logging.getLogger(__name__)

# Op vocabulary. Every op is ultimately a `runJavaScript` variant (or a
# native nav assignment) on the target WebEngineView — see the delivery
# Connections in qml/BrowserSurface.qml.
OPS = ("navigate", "eval_js", "snapshot", "click", "fill")

# Result callback shape: receives the parsed result dict (always carries
# an "ok" bool; an error path adds "error").
ResultCallback = Callable[[dict], None]


class BrowserAutomation(QObject):
    """Bridges automation requests to the QML WebEngineViews.

    Owned by `AppController` and exposed to QML as
    `controller.browserAutomation`. The delivery Connections in
    BrowserSurface.qml binds `onAutomationRequested` and calls `on_result`.
    """

    # (request_id, slot, op, payload-json) → QML delivery.
    automationRequested = Signal(str, int, str, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        # request_id → result callback. GUI-thread-only (see module docstring).
        self._pending: dict[str, ResultCallback] = {}
        self._seq = 0

    def _next_id(self) -> str:
        # pid-prefixed counter (no Math.random / time — deterministic and
        # collision-free within a process; the pid distinguishes instances).
        self._seq += 1
        return f"{os.getpid()}_{self._seq}"

    def request(
        self, slot: int, op: str, payload: dict, on_result: ResultCallback
    ) -> str:
        """Emit an automation request to QML; `on_result(dict)` fires when
        QML delivers the result. Returns the request id ("" on a rejected op).

        GUI-thread only (see module docstring). Validation that the slot
        actually has a live window happens QML-side (the delivery handler
        answers `{ok: false, error: "no-window"}` for an empty slot).
        """
        if op not in OPS:
            on_result({"ok": False, "error": f"unknown op {op!r}"})
            return ""
        request_id = self._next_id()
        self._pending[request_id] = on_result
        self.automationRequested.emit(request_id, slot, op, json.dumps(payload))
        return request_id

    # -- Convenience wrappers (the Stage 2b MCP tools call these) --------
    def navigate(self, slot: int, url: str, on_result: ResultCallback) -> str:
        return self.request(slot, "navigate", {"url": url}, on_result)

    def eval_js(self, slot: int, code: str, on_result: ResultCallback) -> str:
        return self.request(slot, "eval_js", {"code": code}, on_result)

    def snapshot(self, slot: int, on_result: ResultCallback) -> str:
        return self.request(slot, "snapshot", {}, on_result)

    def click(self, slot: int, ref: str, on_result: ResultCallback) -> str:
        return self.request(slot, "click", {"ref": ref}, on_result)

    def fill(self, slot: int, ref: str, text: str, on_result: ResultCallback) -> str:
        return self.request(slot, "fill", {"ref": ref, "text": text}, on_result)

    @Slot(str, str)
    def on_result(self, request_id: str, result_json: str) -> None:
        """QML delivers an op result here (a JSON string).

        Pops the pending callback and invokes it with the parsed dict. A
        result for an unknown id is a safe no-op (the request may have been
        abandoned) — it must never raise, since it runs on the GUI thread.
        """
        callback = self._pending.pop(request_id, None)
        if callback is None:
            log.warning("browser automation: result for unknown request %s", request_id)
            return
        try:
            result = json.loads(result_json)
            if not isinstance(result, dict):
                result = {"ok": False, "error": "result-not-object", "raw": result_json}
        except (json.JSONDecodeError, TypeError):
            result = {"ok": False, "error": "bad-result-json", "raw": result_json}
        callback(result)

    def pending_count(self) -> int:
        """Number of in-flight requests (test/debug introspection)."""
        return len(self._pending)
