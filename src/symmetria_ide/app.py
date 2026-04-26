"""QApplication wiring: spawns NvimBackend, loads the QML scene.

This is the boundary between Python backend code and the QML UI. The
QML import module `Symmetria.Ide` is registered so that QML files can
`import Symmetria.Ide 1.0` and instantiate `NvimView`.

`CapsuleModel` is a thin ListModel-like wrapper around a Python list
that the StatusBar QML repeats over. Keeping it in Python (not QML)
means capsules are updated by signal-connecting to `NvimBackend`, not
by QML polling.
"""

from __future__ import annotations

import gc
import logging
import os
import signal
import sys

from PySide6.QtCore import (
    Property,
    QAbstractListModel,
    QModelIndex,
    QObject,
    Qt,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import QGuiApplication, QSurfaceFormat
from PySide6.QtQml import QQmlApplicationEngine, QmlElement

from .bootstrap import QML_DIR, configure_headless_mode, configure_logging
from .cmdline_models import (  # noqa: F401 — side-effect: @QmlElement registration
    CmdlineState,
    CompletionModel,
    PopupmenuModel,
)
from .nvim_backend import NvimBackend
from .nvim_view import NvimView  # noqa: F401 — side-effect: @QmlElement registration
from .session_host import SessionHost
from .session_models import (  # noqa: F401 — side-effect: @QmlElement registration
    SessionModel,
)
from .whichkey_models import (  # noqa: F401 — side-effect: @QmlElement registration
    WhichKeyModel,
    WhichKeyState,
)

QML_IMPORT_NAME = "Symmetria.Ide"
QML_IMPORT_MAJOR_VERSION = 1


log = logging.getLogger(__name__)


@QmlElement
class StatusBarState(QObject):
    """Per-field statusline state with individual notify signals.

    QML binds to properties (`mode`, `file`, `branch`, `project`,
    `position`) and each `*Changed` signal makes dependent bindings
    re-evaluate automatically. This is why we moved off a generic
    `ListModel`-of-dicts — `Text.text: model.valueFor("mode")` won't
    re-bind when the dict is replaced, but `Text.text: state.mode` will.

    Unknown capsule ids still flow through `CapsuleModel` so future
    extensions (LSP progress, task state) have somewhere to land
    without touching this class.
    """

    modeChanged = Signal()
    fileChanged = Signal()
    branchChanged = Signal()
    projectChanged = Signal()
    positionChanged = Signal()

    _KNOWN_IDS = frozenset({"mode", "file", "branch", "project", "pos"})

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._mode = ""
        self._file = ""
        self._branch = ""
        self._project = ""
        self._position = ""

    @Property(str, notify=modeChanged)
    def mode(self) -> str:
        return self._mode

    @Property(str, notify=fileChanged)
    def file(self) -> str:
        return self._file

    @Property(str, notify=branchChanged)
    def branch(self) -> str:
        return self._branch

    @Property(str, notify=projectChanged)
    def project(self) -> str:
        return self._project

    @Property(str, notify=positionChanged)
    def position(self) -> str:
        return self._position

    @Slot(dict, result=bool)
    def apply(self, payload: dict) -> bool:
        """Apply one capsule payload; return True if it was handled.

        Returning a bool lets the caller decide whether to also forward
        unhandled payloads into a generic model.
        """
        cid = str(payload.get("id") or "")
        value = str(payload.get("value") or "")
        if cid == "mode" and value != self._mode:
            self._mode = value
            self.modeChanged.emit()
            return True
        if cid == "file" and value != self._file:
            self._file = value
            self.fileChanged.emit()
            return True
        if cid == "branch" and value != self._branch:
            self._branch = value
            self.branchChanged.emit()
            return True
        if cid == "project" and value != self._project:
            self._project = value
            self.projectChanged.emit()
            return True
        if cid == "pos" and value != self._position:
            self._position = value
            self.positionChanged.emit()
            return True
        return cid in self._KNOWN_IDS  # handled but unchanged


@QmlElement
class CapsuleModel(QAbstractListModel):
    """ListModel exposing capsule dicts to QML Repeater/ListView.

    Each capsule carries at least `id`, `label`, `value`. QML accesses
    fields via role names (so the delegate writes `model.label`, etc.).

    Updates are idempotent — `update(payload)` replaces-or-appends by
    `id`, keeping display order stable as capsules refresh.
    """

    IdRole = Qt.ItemDataRole.UserRole + 1
    LabelRole = Qt.ItemDataRole.UserRole + 2
    ValueRole = Qt.ItemDataRole.UserRole + 3

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._items: list[dict[str, str]] = []

    def roleNames(self) -> dict[int, bytes]:
        return {
            self.IdRole: b"id",
            self.LabelRole: b"label",
            self.ValueRole: b"value",
        }

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008, ARG002
        return len(self._items)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self._items):
            return None
        item = self._items[index.row()]
        if role == self.IdRole:
            return item.get("id", "")
        if role == self.LabelRole:
            return item.get("label", "")
        if role == self.ValueRole:
            return item.get("value", "")
        return None

    @Slot(dict)
    def update(self, payload: dict) -> None:
        """Upsert a capsule by `id`. New capsules append, existing replace."""
        cid = str(payload.get("id") or "")
        if not cid:
            return
        label = str(payload.get("label") or "")
        value = str(payload.get("value") or "")
        for i, existing in enumerate(self._items):
            if existing.get("id") == cid:
                self._items[i] = {"id": cid, "label": label, "value": value}
                idx = self.index(i)
                self.dataChanged.emit(idx, idx, [self.LabelRole, self.ValueRole])
                return
        self.beginInsertRows(QModelIndex(), len(self._items), len(self._items))
        self._items.append({"id": cid, "label": label, "value": value})
        self.endInsertRows()


class AppController(QObject):
    """Glue object exposed to QML as `controller`.

    Owns the `NvimBackend`, the `StatusBarState` (for well-known
    capsules bound directly into QML properties), and `CapsuleModel`
    (for unknown/extension capsules). Every incoming capsule is tried
    against `StatusBarState.apply` first; if unhandled, it goes into
    the generic model.

    Also owns the agent-pane visibility state. The agent view is a
    full-window mode (not a side panel): when `agentVisible` is True
    the editor is hidden and the `AgentPane` takes over. Triggered
    by the `<leader>A` Lua keymap (runtime/init.lua emits an `agent`
    rpcnotify) or programmatically via `show_agent` / `hide_agent`.
    The composer's Escape keypress in `AgentPane.qml` calls
    `hide_agent` to return focus to the editor.
    """

    backendReady = Signal()
    agentVisibleChanged = Signal()
    awaitingResponseChanged = Signal()
    permissionModeChanged = Signal()

    # Cycle order for Shift+Tab in the agent pane. Tuple is the source of
    # truth for both validation (gate `_set_permission_mode` against this)
    # and the next-mode computation in `cycle_permission_mode`. Order
    # matches the user's mental model: default (ask) → acceptEdits (auto-go
    # on edits) → bypassPermissions (all gates open) → plan (suppress
    # execution) → wraps. The sidecar's `setPermissionMode` accepts the same
    # four values; the SDK supports two more (`dontAsk`, `auto`) which we
    # deliberately omit per the user's spec — adding them later is just
    # extending this tuple.
    _PERMISSION_MODES: tuple[str, str, str, str] = (
        "default",
        "acceptEdits",
        "bypassPermissions",
        "plan",
    )

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        # These initial dimensions are a seed that gets immediately overridden
        # when NvimView first receives a geometryChange event and calls
        # backend.resize() with the real pixel-derived cell count.
        self._backend = NvimBackend(cols=120, rows=30)
        self._status = StatusBarState(self)
        self._capsules = CapsuleModel(self)
        self._cmdline = CmdlineState(self)
        self._popupmenu = PopupmenuModel(self)
        self._completion = CompletionModel(self)
        self._whichkey_state = WhichKeyState(self)
        self._whichkey_model = WhichKeyModel(self)
        self._session_host = SessionHost(self)
        self._session_model = SessionModel(self)
        self._backend.capsule_updated.connect(self._route_capsule)
        self._backend.cmdline_updated.connect(self._cmdline.apply)
        self._backend.popupmenu_updated.connect(self._popupmenu.apply)
        self._backend.completions_updated.connect(self._completion.apply)
        # Both whichkey consumers listen to the same payload — state
        # handles visibility/trail, model handles the items list.
        self._backend.whichkey_event.connect(self._whichkey_state.apply)
        self._backend.whichkey_event.connect(self._whichkey_model.apply)
        # queued: SessionHost worker threads -> SessionModel (GUI thread).
        # Explicit QueuedConnection documents the thread hop per
        # project-standards §4 P2 — cross-thread connect sites carry a
        # one-line comment so a future agent grepping for
        # "cross-thread" / "queued" finds every such site. AutoConnection
        # would resolve to Queued in practice because the emitter and
        # receiver live on different threads, but explicit is safer when
        # the emitter might later be reused from the GUI thread.
        self._session_host.event_received.connect(
            self._session_model.apply, Qt.ConnectionType.QueuedConnection
        )
        # queued: SessionHost worker thread -> AppController (GUI thread).
        # Second slot on the same signal — drives the "awaiting response"
        # boolean off when a `result` envelope (turn-complete) arrives.
        # Lives next to the model's apply so a future grep for
        # `event_received.connect` finds both consumers in one place.
        self._session_host.event_received.connect(
            self._on_session_event, Qt.ConnectionType.QueuedConnection
        )
        self._session_host.closed.connect(
            self._session_model.on_host_closed, Qt.ConnectionType.QueuedConnection
        )
        # Subprocess died (clean exit, crash, or stop()) — clear the
        # spinner so the UI doesn't lie about activity. Queued because
        # `closed` is emitted from the stdout worker thread on EOF.
        self._session_host.closed.connect(
            self._on_session_closed, Qt.ConnectionType.QueuedConnection
        )
        # queued: SessionHost stderr worker thread -> AppController (GUI thread).
        # stderr_line is emitted from _run_stderr_loop (daemon thread); explicit
        # QueuedConnection per project-standards §4 P2.
        self._session_host.stderr_line.connect(
            self._log_session_stderr, Qt.ConnectionType.QueuedConnection
        )
        # Lua-driven agent-pane lifecycle. The rpcnotify emitter lives
        # in `runtime/init.lua`; BackendEvents routes it here. No Qt
        # thread hop — pynvim's worker already ran the notification
        # through the auto-queued `nvim_backend._on_notification`
        # path before emitting `agent_event`.
        self._backend.agent_event.connect(self._on_agent_event)
        self._agent_visible = False
        # Spinner state — True from `submit_prompt` until either the
        # turn-complete `result` envelope arrives or the subprocess
        # exits. The UI binds visibility against this; the boolean is
        # the entire infrastructure (delegate aesthetic is intentionally
        # cheap so the placeholder can iterate later).
        self._awaiting_response = False
        # Permission mode — mirrors the sidecar's authoritative
        # `currentMode`. Sidecar emits a `permission_mode_changed` echo
        # at session start AND after every successful `setPermissionMode`
        # call, so this field is updated reactively rather than
        # optimistically (the SDK can reject a transition into
        # `bypassPermissions` if `allowDangerouslySkipPermissions` is
        # not set; we set the flag in the sidecar but trust the echo).
        self._permission_mode: str = "default"

    @Slot(dict)
    def _route_capsule(self, payload: dict) -> None:
        if self._status.apply(payload):
            return
        self._capsules.update(payload)

    @Slot(str)
    def _log_session_stderr(self, line: str) -> None:
        """Forward the sidecar's stderr into the app log.

        The agent pane doesn't render stderr for the placeholder —
        it's diagnostic only (sidecar lifecycle, SDK auth failures,
        sidecar-internal logging). Logging at WARNING surfaces it
        without the pane having to grow another row type, and keeps
        the signal trivially greppable during exploration.
        """
        if line:
            log.warning("session stderr: %s", line)

    # --- Agent-pane visibility + submission ----------------------------

    @Property(bool, notify=agentVisibleChanged)
    def agentVisible(self) -> bool:
        return self._agent_visible

    @Property(bool, notify=awaitingResponseChanged)
    def awaitingResponse(self) -> bool:
        return self._awaiting_response

    def _set_awaiting_response(self, value: bool) -> None:
        """Single mutation point for the spinner boolean.

        Centralised so every on/off edge funnels through one
        idempotent guard — repeated edges (two `result` envelopes,
        spurious `closed` after `stop()`) don't re-emit the signal
        and trigger pointless QML re-bindings.
        """
        if self._awaiting_response == value:
            return
        self._awaiting_response = value
        self.awaitingResponseChanged.emit()

    @Property(str, notify=permissionModeChanged)
    def permissionMode(self) -> str:
        return self._permission_mode

    def _set_permission_mode(self, value: str) -> None:
        """Single mutation point for the permission-mode property.

        Same idempotent guard as `_set_awaiting_response` — repeated
        echoes (sidecar emits one on start AND one after every
        `setPermissionMode` resolution, so a no-op transition from
        `default` to `default` lands twice) don't re-emit the signal.

        Validates against `_PERMISSION_MODES` and silently drops invalid
        values; tolerance lets the sidecar add modes later without
        hard-crashing the controller (forward compatibility), and
        protects against malformed `permission_mode_changed` envelopes.
        """
        if value not in self._PERMISSION_MODES:
            log.warning("_set_permission_mode: invalid mode %r — ignored", value)
            return
        if self._permission_mode == value:
            return
        self._permission_mode = value
        self.permissionModeChanged.emit()

    @Slot()
    def cycle_permission_mode(self) -> None:
        """Advance the permission mode one step through `_PERMISSION_MODES`.

        Called from QML's `Keys.onPressed` handler when the agent pane
        captures Shift+Tab / Backtab. Computes the next mode in cycle
        order from the CURRENT `_permission_mode` and writes a
        `set_permission_mode` command to the sidecar. Does NOT mutate
        `_permission_mode` here — the sidecar's `permission_mode_changed`
        echo is the single source of truth, because the SDK can reject
        a transition (e.g. into `bypassPermissions` without
        `allowDangerouslySkipPermissions`). Optimistic mutation would
        flicker the pill into a state the SDK didn't accept.
        """
        try:
            idx = self._PERMISSION_MODES.index(self._permission_mode)
        except ValueError:
            # Defensive: shouldn't happen because `_set_permission_mode`
            # validates. If it does, fall back to advancing from the
            # canonical default rather than wedging the cycle.
            idx = -1
        next_mode = self._PERMISSION_MODES[(idx + 1) % len(self._PERMISSION_MODES)]
        log.debug("cycle_permission_mode: %s -> %s", self._permission_mode, next_mode)
        self._session_host.send_set_permission_mode(next_mode)

    @Slot()
    def show_agent(self) -> None:
        """Open full-window agent view. Focus routing is QML's job."""
        if self._agent_visible:
            return
        self._agent_visible = True
        self.agentVisibleChanged.emit()

    @Slot()
    def hide_agent(self) -> None:
        """Return to editor view. Focus routing is QML's job."""
        if not self._agent_visible:
            return
        self._agent_visible = False
        self.agentVisibleChanged.emit()

    @Slot()
    def toggle_agent(self) -> None:
        # No idempotency guard here — by definition toggle always changes
        # state, so checking for "already in the desired state" makes no
        # sense. show_agent / hide_agent have guards because they have a
        # target direction; toggle does not.
        self._agent_visible = not self._agent_visible
        self.agentVisibleChanged.emit()

    @Slot(dict)
    def _on_session_event(self, event: dict) -> None:
        """OFF edge for the spinner — drop on `result` envelope.

        `result` is the SDK's canonical turn-complete signal (carries
        `duration_ms` + `total_cost_usd`). Watching for it here keeps
        the spinner state coupled to the protocol rather than to UI
        heuristics like "stop spinning when streaming text appears" —
        protocol coupling means tool-using turns (which produce text
        AND continue working) keep the spinner on through the whole
        round-trip.

        Pending permission requests do NOT clear the spinner: the
        sidecar's canUseTool callback is awaiting our reply, and the
        turn is still in flight from the user's perspective. The
        `result` envelope only fires once the user's decision lands
        and any subsequent tool work completes.

        Also routes the sidecar's `permission_mode_changed` echo into
        `_set_permission_mode` so the pill renders the SDK's actual
        accepted state (the cycle slot deliberately doesn't optimistically
        mutate — see `cycle_permission_mode` for rationale).
        """
        kind = str(event.get("type") or "")
        if kind == "result":
            self._set_awaiting_response(False)
        elif kind == "permission_mode_changed":
            self._set_permission_mode(str(event.get("mode") or ""))

    @Slot()
    def _on_session_closed(self) -> None:
        """Defensive OFF edge — subprocess exited without a `result`.

        Crashes, SIGTERM from `<leader>aN`, or auth failures all reach
        the GUI through `closed` rather than a `result` envelope. Without
        this slot the spinner would stay lit indefinitely after a crash.

        Also resets the permission mode to `default` so the next session
        (e.g. after `<leader>aN`) starts with the canonical pill rather
        than briefly inheriting the stale mode from the dead subprocess.
        The new sidecar's start-time echo would override this anyway,
        but resetting synchronously closes the visible window.
        """
        self._set_awaiting_response(False)
        self._set_permission_mode("default")

    @Slot(dict)
    def _on_agent_event(self, payload: dict) -> None:
        """Route a Lua-emitted agent lifecycle event.

        Payload shape: `{op: "show"|"hide"|"toggle"|"debug", ...}`.
        Unknown ops log at DEBUG and no-op — additive protocol
        evolution doesn't crash the controller.

        `action="new"` on a `show` event resets the pane to a fresh
        slate: stops any in-flight subprocess + clears the event log.
        Used by the `<leader>aN` hijack so "New Claude" reads as
        "start from scratch", not "append to whatever was there".

        `op="debug"` surfaces runtime diagnostics from the Lua side
        (keymap-install attempts, orchestrator race observations,
        etc.) at INFO so the operator can observe the hijack state
        without needing `:messages` dives. Payload carries an `event`
        string that discriminates the debug category.
        """
        op = str(payload.get("op") or "").strip()
        action = str(payload.get("action") or "").strip()
        if op == "show":
            if action == "new":
                if self._session_host.is_running:
                    self._session_host.stop()
                self._session_model.clear()
                # Belt-and-suspenders: `_on_session_closed` will fire
                # via the queued `closed` signal once the worker
                # reaches EOF, but the QueuedConnection means it lands
                # on a later event-loop tick. Resetting synchronously
                # here keeps the spinner from briefly lingering when
                # the user mashes <leader>aN.
                self._set_awaiting_response(False)
            self.show_agent()
        elif op == "hide":
            self.hide_agent()
        elif op == "toggle":
            self.toggle_agent()
        elif op == "debug":
            event = str(payload.get("event") or "")
            log.debug("agent debug: %s %r", event, payload)
        else:
            log.debug("unhandled agent op: %r", op)

    @Slot(str)
    def submit_prompt(self, prompt: str) -> None:
        """Route the user's composer submit to the session host.

        Two branches:

        - **Cold** (no running sidecar): `start(prompt)` spawns the
          Node sidecar and delivers `prompt` as the first JSONL
          `user_message` command on stdin, which the sidecar wraps in
          an `SDKUserMessage` for the SDK's prompt iterable.
        - **Hot** (sidecar alive from a previous turn):
          `send_user_message(prompt)` writes another `user_message`
          command on the same stdin stream. The SDK's session state
          (model context, tool authorization, etc.) is retained
          inside the sidecar — this is what makes the pane feel like
          a conversation rather than independent one-shots.

        Before either branch, we feed a synthetic `user` event into
        `SessionModel` so the message appears in the pane the instant
        the composer submits. The sidecar deliberately drops
        SDKUserMessage echoes (Python's optimistic-render is the
        single source of truth) so without this synthetic injection
        the user would have no visual confirmation of what they typed.

        `<leader>aN` (`action="new"` on the agent-event) stops the
        host and clears the model before this slot runs, so "new
        Claude" semantics still re-spawn from scratch.
        """
        text = prompt.strip()
        if not text:
            return
        # Optimistic local rendering. `apply` is the same slot wired
        # to `event_received` via QueuedConnection, but we're already
        # on the GUI thread here so a direct call is safe and cheaper
        # than detouring through the queue.
        self._session_model.apply(
            {"type": "user", "message": {"role": "user", "content": text}}
        )
        # ON edge for the spinner. Set BEFORE dispatching to the host
        # so the UI flips into "thinking" the same frame the user
        # message renders — no perceptible gap between submit and
        # acknowledgement.
        self._set_awaiting_response(True)
        if self._session_host.is_running:
            log.info("submit_prompt (continue): %s", text[:100])
            self._session_host.send_user_message(text)
        else:
            log.info("submit_prompt (new session): %s", text[:100])
            self._session_host.start(text)

    @Slot(str, str)
    def respond_to_permission(self, request_id: str, decision: str) -> None:
        """Send the user's permission decision back through the sidecar.

        Wired from `AgentPane.qml`'s permission card buttons:
        `controller.respond_to_permission(entry.requestId, "allow"|"deny")`.

        Order matters: the sidecar gets the response first so its
        canUseTool promise resolves and the SDK can proceed (or
        deliver the deny tool_result). Then we mark the model row as
        approved/denied so the UI gives immediate feedback even if
        the sidecar's next event takes a moment to arrive. Validates
        `decision` and silently drops invalid values — keeps the QML
        invocation surface tolerant of typos or accidental coercion.
        """
        if decision not in ("allow", "deny"):
            log.warning(
                "respond_to_permission: invalid decision %r — dropped", decision
            )
            return
        self._session_host.send_permission_response(request_id, decision)
        self._session_model.resolve_permission(request_id, decision)

    def start(self) -> None:
        self._backend.start()
        # Pre-warm the SDK sidecar at app launch so the permission-mode
        # pill + Shift+Tab cycling are live the moment the agent pane is
        # reachable. Without this, `cycle_permission_mode` writes a
        # `set_permission_mode` envelope to a non-existent stdin (the
        # `if self._proc is None` guard in `SessionHost._write_command`
        # silently drops the write) until the user sends their first
        # message — user-visible symptom: open IDE → press Shift+Tab on
        # the agent pane → pill never moves → user assumes the binding
        # is broken. Empty prompt to `start("")` spawns the subprocess
        # but skips the initial `send_user_message`; the SDK's prompt
        # async iterable blocks on its first await until `submit_prompt`
        # later pushes onto it. The sidecar's start-time
        # `permission_mode_changed("default")` echo proves the pre-warm
        # succeeded and seeds the QML pill via `_on_session_event`.
        self._session_host.start("")
        # Agent view is editor-first by default. Two opt-in vectors on
        # startup:
        #   SYMMETRIA_IDE_AGENT_PROMPT="..." — spawn one claude run
        #     with the given prompt AND open the agent view so the
        #     events are immediately visible. Used by headless smoke.
        #   SYMMETRIA_IDE_AGENT_VIEW=1      — open the agent view
        #     with an empty composer ready for interactive typing.
        # Neither set = classic editor-only workflow. User can still
        # open the agent view at any time via `<leader>A`.
        prompt = os.environ.get("SYMMETRIA_IDE_AGENT_PROMPT") or ""
        want_view = bool(prompt) or os.environ.get("SYMMETRIA_IDE_AGENT_VIEW") == "1"
        if prompt:
            log.info("SYMMETRIA_IDE_AGENT_PROMPT set — submitting initial prompt")
            # Route through submit_prompt so the env-var path picks up
            # the same synthetic-user-row injection that the composer
            # uses. Now that the sidecar is pre-warmed above, this hits
            # `submit_prompt`'s hot branch (`is_running` is True), which
            # calls `send_user_message` instead of spawning a second
            # subprocess. The synthetic user-row injection inside
            # `submit_prompt` still renders the prompt optimistically.
            self.submit_prompt(prompt)
        if want_view:
            self.show_agent()
        self.backendReady.emit()

    def shutdown(self) -> None:
        # Stop the session host first — the subprocess is the noisier
        # of the two and we'd rather have its workers joined before
        # nvim's shutdown handshake owns the event loop.
        self._session_host.stop()
        self._backend.stop()

    @property
    def backend(self) -> NvimBackend:
        return self._backend

    @property
    def status(self) -> StatusBarState:
        return self._status

    @property
    def capsules(self) -> CapsuleModel:
        return self._capsules

    @property
    def cmdline(self) -> CmdlineState:
        return self._cmdline

    @property
    def popupmenu(self) -> PopupmenuModel:
        return self._popupmenu

    @property
    def completion(self) -> CompletionModel:
        return self._completion

    @property
    def whichkey_state(self) -> WhichKeyState:
        return self._whichkey_state

    @property
    def whichkey_model(self) -> WhichKeyModel:
        return self._whichkey_model

    @property
    def session_host(self) -> SessionHost:
        return self._session_host

    @property
    def session_model(self) -> SessionModel:
        return self._session_model


def _register_qml_types() -> None:
    """Named audit point for QML type registration.

    All `@QmlElement`-decorated classes self-register as a side effect
    of their class definition being evaluated. That happens when their
    module is imported. This function exists so future maintainers have
    a discoverable home for any *explicit* `qmlRegisterType(...)` calls
    that can't be expressed with the decorator.

    Every side-effect import is also referenced here by name so that
    automated import-pruners cannot strip the `noqa: F401` import
    without also touching this function. This applies to all QML-
    registered modules, not just `NvimView`.
    """
    # Keep these references — they are the second layer of protection for
    # the noqa: F401 side-effect imports above. Removing any name here
    # means a linter can silently drop the import and break @QmlElement
    # registration. See CLAUDE.md gotcha #7 and project-standards §2 P1.
    _ = NvimView
    _ = CmdlineState
    _ = CompletionModel
    _ = PopupmenuModel
    _ = WhichKeyModel
    _ = WhichKeyState
    _ = SessionModel


def _build_engine(controller: AppController) -> QQmlApplicationEngine | None:
    """Build the QML engine, wire controller context properties, load Main.qml.

    Returns the loaded engine, or `None` if `Main.qml` failed to load
    (missing root objects). Caller is responsible for mapping `None`
    onto a non-zero exit code.

    The ten context properties here are the stable QML surface between
    Python and QML — keeping them in one place makes it obvious what
    QML sees and makes adding a new one (or removing one) a single-line
    change.
    """
    engine = QQmlApplicationEngine()
    ctx = engine.rootContext()

    # Make backend + capsules available to QML as a single `controller`
    # context property — keeps the QML surface small.
    ctx.setContextProperty("controller", controller)
    ctx.setContextProperty("nvimBackend", controller.backend)
    ctx.setContextProperty("capsuleModel", controller.capsules)
    ctx.setContextProperty("statusState", controller.status)
    ctx.setContextProperty("cmdlineState", controller.cmdline)
    ctx.setContextProperty("popupmenuModel", controller.popupmenu)
    ctx.setContextProperty("completionModel", controller.completion)
    ctx.setContextProperty("whichKeyState", controller.whichkey_state)
    ctx.setContextProperty("whichKeyModel", controller.whichkey_model)
    ctx.setContextProperty("sessionHost", controller.session_host)
    ctx.setContextProperty("sessionModel", controller.session_model)

    # Resolve the editor font ONCE in Python so every QML overlay binds
    # to the same family the grid (`NvimView._default_font`) chose.
    # QML's `font.family` is a single QString — it does NOT parse
    # comma-separated strings as a fallback list, and `font.families`
    # (plural) is not exposed on QML's font value type in Qt 6.11 — so
    # we can't do per-glyph cascade from QML. Passing the resolved
    # family name as a context property prevents drift between grid
    # and overlays, which is the main risk of each QML file having
    # its own hardcoded font string. Full pitfall notes are in
    # `qml/CommandLine.qml` (the canonical font comment).
    #
    # Use .families()[0] rather than .family(): when `setFamilies` is
    # called with a multi-family list, families()[0] is always the
    # primary resolved entry. .family() is equivalent for the preferred
    # path but may differ in edge cases (e.g. systemFont fallback on
    # some Qt builds where family() returns "").
    _resolved_font = NvimView._default_font()
    _primary_family = (_resolved_font.families() or [_resolved_font.family()])[0]
    ctx.setContextProperty("editorFontFamily", _primary_family)

    qml_root = QML_DIR / "Main.qml"
    # IMPORTANT: always use fromLocalFile here, not a QRC resource URL.
    # The "import \"design\"" singleton in Main.qml (and its siblings) resolves
    # via a relative sibling directory lookup — Qt's resource-URL import resolver
    # uses a different search strategy and will NOT find qml/design/qmldir if
    # Main.qml is loaded from qrc:/. Keep this as a file-URL load.
    engine.load(QUrl.fromLocalFile(str(qml_root)))
    if not engine.rootObjects():
        log.error("failed to load Main.qml at %s", qml_root)
        return None
    return engine


def run() -> int:
    configure_logging()
    # Ctrl-C in the terminal should kill the app, not be caught by Qt.
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    # Request an alpha channel on the default surface BEFORE QGuiApplication
    # spins up the QPA plugin. Without this, Wayland (and X) hand us an
    # opaque framebuffer and `color: "transparent"` in QML has no effect —
    # the compositor composites against black. Must precede app creation.
    fmt = QSurfaceFormat.defaultFormat()
    fmt.setAlphaBufferSize(8)
    QSurfaceFormat.setDefaultFormat(fmt)

    app = QGuiApplication(sys.argv)
    app.setApplicationName("Symmetria IDE")
    app.setOrganizationName("Symmetria")
    # Sets the Wayland xdg-shell `app_id` — Hyprland sees this as the
    # window class, so window rules can match on `symmetria-ide`.
    app.setDesktopFileName("symmetria-ide")

    _register_qml_types()
    controller = AppController()
    engine = _build_engine(controller)
    if engine is None:
        return 1

    controller.start()
    app.aboutToQuit.connect(controller.shutdown)
    # If nvim exits on its own (user typed `:q`), close the window too
    # — otherwise the grid freezes on whatever was last rendered and
    # the user has no way to exit except killing the process.
    controller.backend.closed.connect(app.quit, Qt.ConnectionType.QueuedConnection)

    shot_path = os.environ.get("SYMMETRIA_IDE_SCREENSHOT")
    test_keys = os.environ.get("SYMMETRIA_IDE_TEST_KEYS")
    if shot_path or test_keys:
        configure_headless_mode(controller, engine, app, shot_path, test_keys)

    # gotcha #10: gc.freeze() must sit immediately before app.exec() —
    # everything allocated up to here is long-lived (Qt wrappers, QML
    # engine state, controller, backend). Freezing them moves those
    # objects into the permanent generation so the cyclic collector skips
    # them on every subsequent pass. Combined with the gc-disabled window
    # in `NvimBackend._dispatch_redraw`, this shrinks the "GC runs while
    # Qt renders" race surface that caused SIGSEGVs under Python 3.14.
    # Later allocations wouldn't be frozen; earlier placement would miss
    # state that still needs freezing. See CLAUDE.md gotcha #10.
    gc.collect()
    gc.freeze()

    return app.exec()
