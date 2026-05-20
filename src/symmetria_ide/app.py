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

from typing import ClassVar

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
    QtMsgType,
    QUrl,
    Signal,
    Slot,
    qInstallMessageHandler,
)
from PySide6.QtGui import QGuiApplication, QSurfaceFormat
from PySide6.QtQml import QQmlApplicationEngine, QmlElement

from .bootstrap import QML_DIR, configure_headless_mode, configure_logging
from .cmdline_models import (  # noqa: F401 — side-effect: @QmlElement registration
    CmdlineState,
    CompletionModel,
    PopupmenuModel,
)
from .git_controller import GitController, GitStatusListModel
from .nvim_backend import NvimBackend
from .nvim_view import NvimView  # noqa: F401 — side-effect: @QmlElement registration
from .session_host import SessionHost
from .session_models import (  # noqa: F401 — side-effect: @QmlElement registration
    SessionModel,
)
from .terminal_backend import TerminalBackend
from .terminal_view import (  # noqa: F401 — side-effect: @QmlElement registration
    TerminalView,
)
from .whichkey_models import (  # noqa: F401 — side-effect: @QmlElement registration
    WhichKeyModel,
    WhichKeyState,
)

QML_IMPORT_NAME = "Symmetria.Ide"
QML_IMPORT_MAJOR_VERSION = 1


log = logging.getLogger(__name__)


def _qt_message_handler(mode, ctx, msg: str) -> None:
    """Route Qt/QML log output into Python's logging so console.log + qWarning
    actually surface during development.

    PySide6's default handler suppresses QML `console.log` and many qWarnings
    unless we install our own. The result was that the FileTreeView sidebar
    rendered as an empty column with no diagnostic clue — Qt's "QML component
    failed to construct" warning was being swallowed by the default handler.
    Routing through `log.warning` keeps the messages alive in the same stream
    as Python's own log output.
    """
    qt_log = logging.getLogger("qt.qml")
    file_loc = ""
    if ctx is not None and getattr(ctx, "file", None):
        file_loc = f" ({ctx.file}:{getattr(ctx, 'line', 0)})"
    line = f"{msg}{file_loc}"
    if mode in (QtMsgType.QtFatalMsg, QtMsgType.QtCriticalMsg):
        qt_log.error(line)
    elif mode == QtMsgType.QtWarningMsg:
        qt_log.warning(line)
    elif mode == QtMsgType.QtInfoMsg:
        qt_log.info(line)
    else:
        qt_log.debug(line)


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
    fmVisibleChanged = Signal()
    awaitingResponseChanged = Signal()
    permissionModeChanged = Signal()
    focusedInstanceChanged = Signal()
    instanceCountChanged = Signal()
    instanceTitlesChanged = Signal()
    cwdChanged = Signal()
    # Project-anchor signals. `anchoredChanged` fires on every anchor /
    # release transition; `displayedRootChanged` fires whenever the
    # *effective* root that consumers should display changes — which
    # is either an anchor/release, OR a cwd change while NOT anchored.
    # The conditional emit in `_route_capsule` is what makes anchoring a
    # state machine instead of a stored value (anchor pins the root and
    # cwd updates flow into `_cwd` silently; release re-syncs on the
    # next cwd update).
    anchoredChanged = Signal()
    displayedRootChanged = Signal()
    treeVisibleChanged = Signal()
    # QML-bound focus pull. The Lua `<leader>tf` keybind routes here via
    # `_on_tree_event`; Main.qml's Connections block calls
    # `fileTreeView.forceActiveFocus()`. Decoupled from the data-bearing
    # signals above because it carries no payload — it's a one-way ask.
    focusTreeRequested = Signal()
    # Reverse direction of focusTreeRequested — fired from
    # `_on_nav_event` when nvim spillover targets the editor (no
    # `_NAV_FROM_EDITOR` entry maps to "editor" today, so only the
    # `focus_editor()` public slot emits this signal currently).
    # NOTE: the QML Ctrl+H ApplicationShortcut calls
    # `editor.forceActiveFocus()` directly and does NOT go through this
    # slot — keep the two paths in sync when adding new nav targets.
    focusEditorRequested = Signal()
    # Phase 2.5 central-surface state. `_central_surface` holds either
    # "terminal" or "editor"; both `editorVisible` and `terminalVisible`
    # are derived `@Property(bool)` over the same notify signal so QML
    # bindings stay declarative without a second source of truth.
    # `focusTerminalRequested` is the symmetric counterpart of
    # `focusTreeRequested` / `focusEditorRequested` — Main.qml's
    # Connections block translates it into `terminalView.forceActiveFocus()`.
    centralSurfaceChanged = Signal()
    focusTerminalRequested = Signal()

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

    # Phase B caps the pool at 5 instances to match the `<C-1>..<C-5>`
    # focus keybind surface. The cap is policy, not a structural limit
    # — `_session_hosts` is a sparse dict, so widening to N is just
    # bumping this constant + adding `<C-N>` keybinds in init.lua.
    _MAX_INSTANCES: int = 5

    # Focus-chain spatial graph for the <C-h/j/k/l> spillover bridge.
    # When nvim spills over from the editor at an edge, this table
    # picks the destination outer pane. Today there's only one outer
    # pane (tree) on the right; future panes (agent dock, terminal,
    # etc.) extend the table without restructuring the dispatch path.
    # Reverse direction (tree → editor) is handled directly from QML
    # via the Ctrl+H ApplicationShortcut's `editor.forceActiveFocus()` call
    # — it does NOT go through this table. This dict therefore only encodes
    # editor-as-source edges.
    _NAV_FROM_EDITOR: ClassVar[dict[str, str]] = {
        "right": "tree",
        # left/up/down: no outer pane yet — adding agent dock at "down"
        # is a one-line change.
    }

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        # These initial dimensions are a seed that gets immediately overridden
        # when NvimView first receives a geometryChange event and calls
        # backend.resize() with the real pixel-derived cell count.
        self._backend = NvimBackend(cols=120, rows=30)
        # Phase 2.5 terminal pane. Coexists with nvim under Main.qml's
        # `mainContent` Item — `_central_surface` toggles which is visible.
        # Spawned eagerly from `start()` AFTER the nvim backend so the
        # editor's grid lands first (gates QSGRenderThread's first frame —
        # spawning the terminal first can briefly flash an empty editor
        # on slow hardware). The closed signal is connected here so a
        # shell that exits on its own surfaces in the log.
        self._terminal_backend = TerminalBackend(self)
        # Per Q2-d topology decision: terminal is the persistent home
        # surface, nvim is summoned over it. First-launch visible = terminal.
        # Editor is pre-spawned hidden via `_backend.start()` so swap-to-
        # editor is instant (Q1 answer 1b).
        self._central_surface: str = "terminal"
        self._status = StatusBarState(self)
        self._capsules = CapsuleModel(self)
        self._cmdline = CmdlineState(self)
        self._popupmenu = PopupmenuModel(self)
        self._completion = CompletionModel(self)
        self._whichkey_state = WhichKeyState(self)
        self._whichkey_model = WhichKeyModel(self)
        # ----- Per-instance pools (Phase A foundation) -------------------
        #
        # Phase A replaces the singular `_session_host` / `_session_model`
        # fields with slot-keyed dicts so Phase B can drop in `<C-1>..<C-5>`
        # focus + `<leader>aN` multi-spawn without a second refactor.
        # Phase A holds N=1 — the IDE still spawns exactly one sidecar at
        # app start and the user-visible behavior is identical to today.
        # The plumbing just speaks dict-of-slot natively.
        #
        # Slot numbering matches `<C-N>` keybind semantics: 1..5 are valid
        # in Phase B; Phase A uses only slot 1. Per-instance scalar state
        # (awaiting-response, permission-mode) lives in parallel dicts,
        # NOT inside `SessionHost` / `SessionModel`, because those objects
        # are 1:1 with one sidecar/model and shouldn't carry controller
        # bookkeeping. The QML-facing `awaitingResponse` /
        # `permissionMode` properties read from `_focused_instance`'s
        # slot in those dicts.
        self._session_hosts: dict[int, SessionHost] = {}
        self._session_models: dict[int, SessionModel] = {}
        self._awaiting_response: dict[int, bool] = {}
        self._permission_mode: dict[int, str] = {}
        # Per-slot session title — captured from the user's first prompt
        # via `submit_prompt_for` and never overwritten thereafter (matches
        # orchestrator.nvim's "title source is set-once" semantic, where
        # OSC 2 typically fires once at session-start). An empty/missing
        # entry means "no title yet" and the AgentTopBar chip renders
        # only the slot number. `_close_instance` removes the entry.
        self._instance_titles: dict[int, str] = {}
        # Slot whose transcript / state the QML pane currently mirrors.
        # Phase A locks this at 1 (only one instance exists). Phase B
        # introduces `<C-1>..<C-5>` to reassign it.
        self._focused_instance: int = 1
        # request_id -> issuing slot. Populated when a `permission_request`
        # event lands so `respond_to_permission` can route the user's
        # decision back to the right sidecar's `canUseTool` resolver
        # without trusting `_focused_instance` (the user could have focus-
        # switched between request and response in Phase B). Cleared on
        # response or session close. Phase A risk #1 mitigation per the
        # PRD §4.3 — adding it now keeps Phase B from re-touching
        # `respond_to_permission`.
        self._pending_permissions: dict[str, int] = {}
        # NB: pool starts EMPTY. Previously `__init__` auto-allocated
        # slot 1 (so the bubble strip read as "1 active" before the
        # user had asked for any agent), and `start()` pre-warmed its
        # subprocess. Both violated the user's mental model — "no
        # agents until I press <leader>aN". The first `<leader>aN`
        # now calls `_spawn_instance(1)` lazily, matching what the
        # subsequent `<leader>aN` invocations do for slots 2..5. The
        # env-var startup paths (`SYMMETRIA_IDE_AGENT_PROMPT` /
        # `_VIEW`) handle the spawn themselves in `start()`.
        # ----- Backend signal wiring (unchanged) -------------------------
        self._backend.capsule_updated.connect(self._route_capsule)
        self._backend.cmdline_updated.connect(self._cmdline.apply)
        self._backend.popupmenu_updated.connect(self._popupmenu.apply)
        self._backend.completions_updated.connect(self._completion.apply)
        # Both whichkey consumers listen to the same payload — state
        # handles visibility/trail, model handles the items list.
        self._backend.whichkey_event.connect(self._whichkey_state.apply)
        self._backend.whichkey_event.connect(self._whichkey_model.apply)
        # Lua-driven agent-pane lifecycle. The rpcnotify emitter lives
        # in `runtime/init.lua`; BackendEvents routes it here. No Qt
        # thread hop — pynvim's worker already ran the notification
        # through the auto-queued `nvim_backend._on_notification`
        # path before emitting `agent_event`.
        self._backend.agent_event.connect(self._on_agent_event)
        self._agent_visible = False
        # File manager toggle-overlay lifecycle. Same routing pattern as
        # agent_event — Lua emits via rpcnotify, NvimBackend re-emits as
        # fm_event, this controller owns the state. The panel itself is a
        # QML overlay over NvimView (not a separate window).
        self._backend.fm_event.connect(self._on_fm_event)
        self._fm_visible = False
        self._fm_initial_path = ""
        # Always-on file-tree sidebar. Same Lua → rpcnotify → backend signal
        # → controller routing as fm_event; the only op today is "focus",
        # which the Main.qml Connections block translates into a
        # `forceActiveFocus()` on the FileTreeView instance.
        # queued: NvimBackend worker → AppController GUI (same path as agent_event/fm_event)
        self._backend.tree_event.connect(self._on_tree_event)
        # queued: NvimBackend worker → AppController GUI (same path as tree_event)
        self._backend.nav_event.connect(self._on_nav_event)
        # queued: NvimBackend worker → AppController GUI. Secondary surface
        # for the project-anchor concept (the primary is a Qt application-
        # scope shortcut in Main.qml). Lua's `:SymmetriaAnchor` /
        # `:SymmetriaUnanchor` user commands emit through this channel.
        self._backend.anchor_event.connect(self._on_anchor_event)
        # Terminal lifecycle event. `closed` fires when the shell process
        # exits (EOF on master fd, user typed `exit`, or `stop()` killed
        # it). Used only for logging in v1 — the user's next swap-to-
        # editor presents a working editor; the dead terminal pane stays
        # visible until then. A v2 enhancement could auto-swap to editor
        # on close. queued: terminal reader thread → AppController GUI.
        self._terminal_backend.closed.connect(
            self._on_terminal_closed, Qt.ConnectionType.QueuedConnection
        )
        # Phase 2.5 deliverable 3: shell-driven cwd updates. The terminal
        # reader thread extracts OSC 7 sequences (emitted by the
        # chpwd hook in runtime/symmetria-shell/) and emits osc7_received
        # with the parsed path. We route through `_route_capsule` with
        # the synthetic {id:"cwd", value:path} dict shape, so the
        # downstream consumers (anchor state machine, file tree,
        # git controller) see the update through their existing
        # connections — identical code path to nvim's `:cd`. queued:
        # terminal reader thread → AppController GUI.
        self._terminal_backend.osc7_received.connect(
            self._on_terminal_osc7, Qt.ConnectionType.QueuedConnection
        )
        # Seed `cwd` with $HOME so QML's `rootPath: controller.cwd` has
        # a valid path during the brief window between QML construction
        # and the first capsule push from runtime/init.lua's VimEnter +
        # `symmetria_push_state` re-request (per CLAUDE.md gotcha #2).
        # Empty string here would trip FileTreeView's `if (rootPath !==
        # "")` guard and leave the sidebar showing "Empty" until the
        # capsule lands.
        self._cwd: str = os.path.expanduser("~")
        # Project-anchor state. When `_anchored` is True, `displayedRoot`
        # returns `_anchored_root` and incoming cwd updates DO NOT fire
        # `displayedRootChanged` — they still update `_cwd` silently so a
        # later `release_anchor` re-syncs to the latest cwd on the next
        # update. The split between "raw cwd" (`_cwd`) and "displayed
        # root" (`displayedRoot`) is what lets the file tree pin to a
        # project while a future terminal pane's shell continues to cd
        # freely. Anchor is an IDE-level concern; triggers are a Qt
        # application-scope shortcut + `:SymmetriaAnchor` user command,
        # NOT a nvim `<leader>` binding (it has to fire from any pane).
        self._anchored: bool = False
        self._anchored_root: str = ""
        # Always-on by default per the "visualization-first" decision —
        # toggle keybind deferred (no `<leader>tt` in v1). Property
        # exists so QML's `visible: controller.treeVisible` binding has
        # something to read, and so a v2 toggle is a one-line addition.
        self._tree_visible: bool = True
        # ----- Git status provider (status badges + Active Changes panel) -
        # The GitController watches `.git/index` + co. via QFileSystemWatcher
        # and exposes `statusForPath(absolute_path)` to QML. It's the single
        # source of truth shared between the file tree's per-row badges and
        # the (forthcoming) Active Changes panel above the tree — one parse,
        # two consumers. Bound to the nvim `project` capsule below.
        self._git_controller = GitController(self)
        # Flat-list projection for the Active Changes panel — filters out
        # directory aggregates, sorts by path, exposes Qt model roles for
        # the panel's ListView. Auto-refreshes on the controller's
        # statusChanged via a queued connection (handled internally).
        self._git_status_list = GitStatusListModel(self._git_controller, self)
        # Drive `repoRoot` from `displayedRoot` (NOT raw `cwd`). This is
        # the ONE place where the anchor concept leaks below the pure
        # view-transformation line into actual behavior: when anchored,
        # git operations target the anchored root even as cwd wanders.
        # That IS the user-facing payoff of anchoring — `<leader>g*`
        # operations stay scoped to the project the user committed to.
        # Every other consumer (file-tree rootPath, future terminal
        # new-tab cwd) is a view-layer rebind via `displayedRoot`; this
        # is the one operational rebind. `displayedRootChanged` fires
        # on cwd updates while NOT anchored AND on anchor/release
        # transitions, so the provider rebuilds at exactly the right
        # moments. Same-thread connection (anchor and capsule routing
        # both run on the GUI thread), no QueuedConnection needed.
        # same-thread: displayedRootChanged fires on the GUI thread; GitController.set_repo_root is GUI-only
        self.displayedRootChanged.connect(self._sync_git_repo_root)

    def _create_instance(self, slot: int) -> None:
        """Allocate one pool entry: host + model + per-instance scalar state.

        Wires the host's signals to the model and to indexed event
        handlers via `lambda ..., idx=slot: ...` — capturing `slot` as a
        default arg avoids the closure-over-loop-var pitfall (every
        lambda would otherwise reference the same final binding) and
        keeps the routing intent grep-able at the connect site.

        Does NOT call `host.start(...)` — caller is responsible for
        cold-start ordering (e.g. `AppController.start()` sequences the
        slot-1 pre-warm AFTER `NvimBackend.start()` so the editor wins
        the first paint frame). Phase B's `<leader>aN` handler will
        call this with a fresh slot number and then immediately follow
        with `host.start("")` to pre-warm the new sidecar.

        Idempotent on slot reuse (same slot already in pool — log and
        return) so a paranoid caller can't double-allocate.
        """
        if slot in self._session_hosts:
            log.warning("_create_instance: slot %d already exists — ignored", slot)
            return
        host = SessionHost(self, instance_index=slot)
        model = SessionModel(self, instance_index=slot)
        self._session_hosts[slot] = host
        self._session_models[slot] = model
        self._awaiting_response[slot] = False
        self._permission_mode[slot] = "default"
        # queued: SessionHost worker thread -> SessionModel (GUI thread).
        # Explicit QueuedConnection documents the thread hop per
        # project-standards §4 P2.
        host.event_received.connect(model.apply, Qt.ConnectionType.QueuedConnection)
        # queued: SessionHost worker thread -> AppController (GUI thread).
        # Lambda default-arg captures `slot` so the indexed handler
        # receives this entry's slot regardless of which sidecar emits.
        host.event_received.connect(
            lambda event, idx=slot: self._on_session_event_for(idx, event),
            Qt.ConnectionType.QueuedConnection,
        )
        host.closed.connect(model.on_host_closed, Qt.ConnectionType.QueuedConnection)
        # queued: subprocess EOF (worker thread) -> AppController.
        # Indexed via the same lambda+default-arg pattern.
        host.closed.connect(
            lambda idx=slot: self._on_session_closed_for(idx),
            Qt.ConnectionType.QueuedConnection,
        )
        # queued: stderr worker thread -> AppController. stderr is
        # diagnostic-only and we don't index it for Phase A — log
        # messages can attribute via SessionHost's instance_index in a
        # follow-up if multi-instance traces become noisy.
        host.stderr_line.connect(
            self._log_session_stderr, Qt.ConnectionType.QueuedConnection
        )
        self.instanceCountChanged.emit()

    def _next_free_slot(self) -> int | None:
        """Lowest unoccupied slot in `1.._MAX_INSTANCES`, or None if full.

        Returns 1 for an empty pool (start-of-day, or after closing the
        last instance). The PRD's "lowest free" semantics means the
        natural way to think about the pool is "fill from the bottom" —
        which also matches the `<C-1>..<C-5>` keybind ordering so the
        first spawn is `<C-1>`-reachable, the second is `<C-2>`, etc.
        """
        for slot in range(1, self._MAX_INSTANCES + 1):
            if slot not in self._session_hosts:
                return slot
        return None

    @staticmethod
    def _derive_title(text: str) -> str:
        """Turn a raw user prompt into a chip-sized session title.

        Strips whitespace, takes only the first line (multi-line prompts
        usually have a clear "topic sentence" first), and truncates to
        an orchestrator-matching 32 chars with a U+2026 ellipsis. The
        AgentTopBar chip applies a tighter visual cap on top of this
        via `elide: Text.ElideRight` — this method only ensures the
        Python-side cap so the wire/state stays compact.
        """
        first_line = text.strip().splitlines()[0] if text.strip() else ""
        if len(first_line) <= 32:
            return first_line
        return first_line[:32] + "…"

    def _spawn_instance(self, slot: int, prompt: str = "") -> None:
        """Allocate the slot and pre-warm its sidecar.

        Wraps `_create_instance` (allocation only) + `host.start(prompt)`
        (subprocess spawn). Empty prompt = pre-warm only — the SDK's
        prompt async iterable blocks waiting for the first user message
        but the subprocess is alive and `set_permission_mode` writes
        will be honored. This is the same pre-warm contract slot 1
        uses at app start (see `start()`).
        """
        self._create_instance(slot)
        self._session_hosts[slot].start(prompt)

    def _close_instance(self, slot: int) -> None:
        """Tear down the slot's host + model + per-instance state.

        Stops the sidecar (joins workers via the existing `stop()`),
        drops the slot from every pool dict, and removes any
        `_pending_permissions` entries the dead sidecar issued — those
        request_ids will never be answered now that the SDK has
        auto-rejected canUseTool's promise on abort.

        Caller is responsible for refocus / hide-pane decisions —
        `_close_instance` is allocation-symmetric to `_spawn_instance`
        and stays focus-agnostic so the dispatch table can compose
        close + refocus differently per `op`.

        No-op + log on unknown slot.
        """
        host = self._session_hosts.get(slot)
        if host is None:
            log.warning("_close_instance: slot %d not in pool — no-op", slot)
            return
        host.stop()
        del self._session_hosts[slot]
        self._session_models.pop(slot, None)
        self._awaiting_response.pop(slot, None)
        self._permission_mode.pop(slot, None)
        had_title = self._instance_titles.pop(slot, None) is not None
        if had_title:
            self.instanceTitlesChanged.emit()
        self._pending_permissions = {
            req_id: issuing_slot
            for req_id, issuing_slot in self._pending_permissions.items()
            if issuing_slot != slot
        }
        self.instanceCountChanged.emit()

    def _next_focus_after_close(self, closed_slot: int) -> int | None:
        """Pick which slot to focus after closing `closed_slot`.

        Per PRD §5.3: "the one BELOW the closed one if it exists,
        otherwise the next ABOVE". Walks down from `closed_slot - 1`
        toward 1 first, then up from `closed_slot + 1` toward
        `_MAX_INSTANCES`. Returns None on empty pool.

        This is NOT `min(self._session_hosts.keys())` — closing slot 3
        of {1, 2, 3} should focus 2 (below), but `min` would pick 1.
        The "below first" rule matches a stack-of-recent-work mental
        model where the user opened higher slots more recently and
        wants focus to fall back toward older work, not all the way
        to the start.
        """
        if not self._session_hosts:
            return None
        for candidate in range(closed_slot - 1, 0, -1):
            if candidate in self._session_hosts:
                return candidate
        for candidate in range(closed_slot + 1, self._MAX_INSTANCES + 1):
            if candidate in self._session_hosts:
                return candidate
        return None

    @Slot(dict)
    def _route_capsule(self, payload: dict) -> None:
        # `cwd` is intercepted BEFORE _status.apply so it never reaches
        # the StatusBarState (not a statusbar field) and never falls
        # through to CapsuleModel (would render as a stray status-bar
        # pill). The sidebar's FileTreeView reads `controller.cwd`
        # directly via the @Property binding.
        cid = str(payload.get("id") or "")
        if cid == "cwd":
            new_cwd = str(payload.get("value") or "")
            if new_cwd != self._cwd:
                self._cwd = new_cwd
                self.cwdChanged.emit()
                # The conditional below is the load-bearing line of the
                # anchor state machine: while anchored, cwd updates still
                # flow into `_cwd` (so a later release re-syncs cleanly),
                # but the DISPLAYED root stays pinned to `_anchored_root`.
                # Without this gate, anchoring would degrade to a no-op
                # because every BufEnter would re-fire downstream binds.
                if not self._anchored:
                    self.displayedRootChanged.emit()
                else:
                    log.debug(
                        "cwd update suppressed (anchored to %s): %s",
                        self._anchored_root,
                        new_cwd,
                    )
            return
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

    @Property(bool, notify=fmVisibleChanged)
    def fmVisible(self) -> bool:
        return self._fm_visible

    @Property(str, notify=fmVisibleChanged)
    def fmInitialPath(self) -> str:
        """Initial directory for the FM overlay — set just before show_fm.

        QML reads this when the overlay's Loader becomes active so the
        FileManager panel boots into the right directory. Reset to ""
        when the overlay closes so the next open re-pulls the current
        nvim cwd rather than reusing a stale value.
        """
        return self._fm_initial_path

    @Property(bool, notify=awaitingResponseChanged)
    def awaitingResponse(self) -> bool:
        """Spinner state for the FOCUSED instance.

        Reads from the per-instance dict. Phase B's focus-switch
        emits `awaitingResponseChanged` so the QML re-binds against
        the new slot's value without the property gaining any
        per-instance machinery on the QML side.
        """
        return self._awaiting_response.get(self._focused_instance, False)

    def _set_awaiting_response_for(self, slot: int, value: bool) -> None:
        """Per-instance spinner mutation.

        Updates the dict unconditionally on a real change but only
        emits the QML-facing signal when the mutated slot is the
        currently-focused one — the property reads from
        `_focused_instance`, so an unfocused instance changing state
        produces no QML re-bind. Idempotent: repeated edges (two
        `result` envelopes, spurious `closed` after `stop()`) don't
        re-emit even for the focused slot.
        """
        if self._awaiting_response.get(slot) == value:
            return
        self._awaiting_response[slot] = value
        if slot == self._focused_instance:
            self.awaitingResponseChanged.emit()

    @Property(str, notify=permissionModeChanged)
    def permissionMode(self) -> str:
        """Permission mode for the FOCUSED instance.

        Same focused-slot read as `awaitingResponse`; same Phase B
        re-emit-on-focus-switch contract.
        """
        return self._permission_mode.get(self._focused_instance, "default")

    def _set_permission_mode_for(self, slot: int, value: str) -> None:
        """Per-instance permission-mode mutation.

        Validates against `_PERMISSION_MODES` and silently drops invalid
        values (forward-compat against future SDK modes, and protection
        against malformed `permission_mode_changed` envelopes). Same
        focused-slot signal-emit gate as `_set_awaiting_response_for`.
        """
        if value not in self._PERMISSION_MODES:
            log.warning("_set_permission_mode_for: invalid mode %r — ignored", value)
            return
        if self._permission_mode.get(slot) == value:
            return
        self._permission_mode[slot] = value
        if slot == self._focused_instance:
            self.permissionModeChanged.emit()

    # --- Pool-shape properties (Phase A surface for the QML indicator) ---

    @Property(int, notify=focusedInstanceChanged)
    def focusedInstance(self) -> int:
        """Slot whose transcript the agent pane currently mirrors.

        Read by AgentTopBar.qml to highlight the focused chip in the
        always-on chip strip. Also used by AgentPane.qml when binding
        `sessionModelForFocused` to the correct slot's transcript.
        """
        return self._focused_instance

    @Property(int, notify=instanceCountChanged)
    def instanceCount(self) -> int:
        """Number of live instances in the pool.

        Phase A always reads 1. Phase B's `<leader>aN` increments this
        as new sidecars spawn; `<C-S-q>` decrements it on close.
        """
        return len(self._session_hosts)

    @Property(list, notify=instanceCountChanged)
    def activeInstanceSlots(self) -> list[int]:
        """Sorted list of occupied pool slot indices.

        Reuses `instanceCountChanged` as the notify signal because count
        and key-set always change together (a spawn adds one slot AND
        increments count; a close removes one AND decrements). The QML
        bubble strip in `AgentTopBar.qml` uses this to render filled vs.
        empty bubbles without a second notify hop.
        """
        return sorted(self._session_hosts.keys())

    @Property(list, notify=instanceTitlesChanged)
    def instanceTitles(self) -> list[str]:
        """Per-slot titles, indexed 0..maxInstances-1 (slot N at index N-1).

        QML's AgentTopBar chip Repeater iterates over `activeInstanceSlots`
        but each delegate needs its slot's title text. A list aligned to
        slot indices means the delegate just reads `controller.instanceTitles[bubble.slot - 1]`
        — no per-slot Slot call, no map lookup, no notify-per-slot churn.

        Empty string at any index = "no title yet" and the chip renders
        the slot number alone (orchestrator.nvim's same fallback).
        """
        return [
            self._instance_titles.get(slot, "")
            for slot in range(1, self._MAX_INSTANCES + 1)
        ]

    @Property(int, constant=True)
    def maxInstances(self) -> int:
        """Pool capacity (the `_MAX_INSTANCES` constant).

        Surfaced as a `constant=True` property — it never changes at
        runtime, so QML can render exactly N bubbles without binding
        against a notify signal. The QML side uses this as the Repeater
        model length so the bubble strip stays aligned with the spawn
        cap if `_MAX_INSTANCES` ever shifts.
        """
        return self._MAX_INSTANCES

    # `QObject` here (not `SessionModel`) so the property tolerates a
    # transient None during pool transitions (close-empty → spawn) and
    # so PySide6's signature serializer doesn't choke on the
    # `@QmlElement`-registered concrete type. QML accepts a QObject and
    # the ListView treats None as "no model" (renders empty).
    @Property(QObject, notify=focusedInstanceChanged)
    def sessionModelForFocused(self) -> SessionModel | None:
        """The focused slot's `SessionModel`, re-bindable on focus switch.

        QML context properties are evaluated once at engine load — the
        original `sessionModel` context property keeps pointing at slot
        1's model regardless of focus. To make the agent pane's
        `ListView.model` track the focused instance, QML binds against
        THIS property instead, with `Connections { target: controller;
        onFocusedInstanceChanged: ... }` re-evaluating the binding on
        every focus switch (PRD §5.1's recommended fallback when
        layoutChanged-style auto-rebinding is insufficient).
        """
        return self._session_models.get(self._focused_instance)

    @Slot(int)
    def focus_instance(self, index: int) -> None:
        """Reassign focus to the given pool slot.

        Phase A: only slot 1 exists, so any other index is a no-op-with-
        log. Phase B wires this up to `<C-1>..<C-5>`. Emits all three
        QML-facing signals so the pane re-binds the spinner, the pill,
        and the indicator in a single tick.

        No-op when the index is already focused — avoids a spurious
        re-bind cascade if the keybind is held down or repeated.
        """
        if index not in self._session_hosts:
            log.warning("focus_instance: slot %d not in pool — no-op", index)
            return
        if index == self._focused_instance:
            return
        self._focused_instance = index
        self.focusedInstanceChanged.emit()
        self.awaitingResponseChanged.emit()
        self.permissionModeChanged.emit()

    @Slot(int)
    def cycle_permission_mode_for(self, index: int) -> None:
        """Advance permission mode for the given pool slot.

        Computes the next mode in cycle order from the slot's CURRENT
        per-instance value (NOT optimistically — the sidecar's
        `permission_mode_changed` echo remains the single source of
        truth; the SDK can reject a transition into `bypassPermissions`
        if `allowDangerouslySkipPermissions` is not set, and optimistic
        mutation would flicker the pill into a state the SDK didn't
        accept). Writes the `set_permission_mode` command to that
        slot's host.

        No-op + log on unknown slot — Phase B's keybinds may dispatch
        before the pool entry exists in pathological races; tolerating
        that beats crashing.
        """
        if index not in self._session_hosts:
            log.warning("cycle_permission_mode_for: slot %d not in pool — no-op", index)
            return
        current = self._permission_mode.get(index, "default")
        try:
            idx = self._PERMISSION_MODES.index(current)
        except ValueError:
            # Defensive: shouldn't happen because `_set_permission_mode_for`
            # validates. Fall back to advancing from canonical default.
            idx = -1
        next_mode = self._PERMISSION_MODES[(idx + 1) % len(self._PERMISSION_MODES)]
        log.debug(
            "cycle_permission_mode_for slot=%d: %s -> %s", index, current, next_mode
        )
        self._session_hosts[index].send_set_permission_mode(next_mode)

    @Slot()
    def cycle_permission_mode(self) -> None:
        """Wrapper for QML's Shift+Tab — operates on the focused instance.

        QML's `Keys.onPressed` keeps calling this nullary form; the
        focused-instance routing happens here so the QML side stays
        unaware of the pool.
        """
        self.cycle_permission_mode_for(self._focused_instance)

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

    # --- File manager overlay --------------------------------------------

    @Slot(str)
    def show_fm(self, initial_path: str = "") -> None:
        """Open the file-manager overlay at `initial_path` (or nvim cwd)."""
        if self._fm_visible:
            return
        self._fm_initial_path = initial_path or self._nvim_cwd_or_home()
        self._fm_visible = True
        self.fmVisibleChanged.emit()

    @Slot()
    def hide_fm(self) -> None:
        """Dismiss the file-manager overlay; focus returns to the editor."""
        if not self._fm_visible:
            return
        self._fm_visible = False
        self._fm_initial_path = ""
        self.fmVisibleChanged.emit()

    @Slot()
    def toggle_fm(self) -> None:
        if self._fm_visible:
            self.hide_fm()
        else:
            self.show_fm("")

    @Slot(str)
    def open_in_nvim(self, path: str) -> None:
        """Open a file in NeoVim without touching overlay/sidebar state.

        The "always-on sidebar" caller (FileTreeView's onFileActivated)
        wants the file to land in nvim while the sidebar stays visible
        and the editor regains focus. The overlay-picker caller has
        different semantics — it dismisses the overlay after opening —
        and lives in `pick_in_nvim`.
        """
        if not path:
            return
        # `fnameescape` is the safe quoting routine for nvim ex-command
        # arguments — handles spaces, percent, hash, etc. Wrapping the
        # whole call in a single `:execute` keeps the input() one shot.
        cmd = f":execute 'edit ' . fnameescape({path!r})\n"
        self._backend.input(cmd)

    @Slot(str)
    def pick_in_nvim(self, path: str) -> None:
        """Open a file in NeoVim AND dismiss the FM overlay.

        Used by the file-manager picker (Main.qml's
        `FileManagerService.onPickerCompleted` handler). Splits from
        `open_in_nvim` because the sidebar caller wants to KEEP the
        sidebar visible after activation; muxing both into one method
        would force a flag parameter that's always one specific value
        per caller — a smell that we keep as two thin wrappers instead.
        """
        if not path:
            return
        self.open_in_nvim(path)
        self.hide_fm()

    # --- File-tree sidebar ----------------------------------------------

    @Property(str, notify=cwdChanged)
    def cwd(self) -> str:
        """NeoVim's current working directory.

        Populated by the `cwd` capsule emitted from `runtime/init.lua`
        on VimEnter + DirChanged. Kept as a raw signal (no anchor
        filtering) so any consumer that truly wants the live cwd can
        bind here. UI panes that should respect anchoring bind to
        `displayedRoot` instead.
        """
        return self._cwd

    @Property(str, notify=displayedRootChanged)
    def displayedRoot(self) -> str:
        """The effective project root for UI panes.

        Pure function of `_cwd`, `_anchored`, and `_anchored_root`:
        returns the anchored root when anchored (and non-empty), else
        the raw cwd. Bound by `FileTreeView.rootPath` and the git
        controller's `repoRoot`. The `_anchored_root` non-empty guard
        is defense-in-depth — `anchor_to_current_cwd` won't anchor on
        an empty path, but a malformed `:SymmetriaAnchor` payload from
        Lua could still arrive with `""`, and silently falling back to
        cwd in that case is more forgiving than pinning the tree to
        an empty string.
        """
        if self._anchored and self._anchored_root:
            return self._anchored_root
        return self._cwd

    @Property(bool, notify=anchoredChanged)
    def anchored(self) -> bool:
        """Whether the IDE is currently anchored to a project root.

        QML reads this to flip the application-scope shortcut between
        anchor and release semantics, and to drive any future "anchored
        to X" affordance in the file-tree title / status bar.
        """
        return self._anchored

    @Slot()
    def anchor_to_current_cwd(self) -> None:
        """Anchor to the current `_cwd`.

        Primary entrypoint for the Qt application-scope shortcut. No-op
        on empty cwd (defense against pre-VimEnter activation — the
        $HOME seed in __init__ makes this practically unreachable, but
        the guard documents the precondition).
        """
        self.anchor_to_path(self._cwd)

    @Slot(str)
    def anchor_to_path(self, path: str) -> None:
        """Anchor to an explicit path.

        Used by `:SymmetriaAnchor /some/path` (programmatic surface) and
        as the implementation backing `anchor_to_current_cwd`. Empty
        paths are rejected as a no-op (logged at WARNING) to avoid the
        "anchored to nothing" state that would render as an empty file
        tree with no clear recovery path.
        """
        if not path:
            log.warning("anchor_to_path: empty path rejected")
            return
        already_anchored_here = self._anchored and self._anchored_root == path
        if already_anchored_here:
            return
        was_anchored = self._anchored
        prior_displayed = self.displayedRoot
        self._anchored = True
        self._anchored_root = path
        if not was_anchored:
            self.anchoredChanged.emit()
        if self.displayedRoot != prior_displayed:
            self.displayedRootChanged.emit()
        log.info("anchor: set to %s", path)

    @Slot()
    def release_anchor(self) -> None:
        """Release the anchor; `displayedRoot` reverts to raw cwd.

        Symmetric to `anchor_to_path`: emits `anchoredChanged` only on
        actual transitions, and emits `displayedRootChanged` only when
        the effective root actually changes (the no-change case is
        "anchored_root happened to equal current cwd" — uncommon but
        possible, and a spurious signal there would cost a needless
        git rescan).
        """
        if not self._anchored:
            return
        prior_displayed = self.displayedRoot
        self._anchored = False
        self._anchored_root = ""
        self.anchoredChanged.emit()
        if self.displayedRoot != prior_displayed:
            self.displayedRootChanged.emit()
        log.info("anchor: released")

    @Property(bool, notify=treeVisibleChanged)
    def treeVisible(self) -> bool:
        """Sidebar visibility — true by default.

        Bound to the sidebar's QML `visible` property. No toggle
        keybind exists in v1 (the "visualization-first" decision); the
        property exists so a future `<leader>tt` is a one-line addition
        without restructuring the binding shape.
        """
        return self._tree_visible

    @Property(QObject, constant=True)
    def gitController(self) -> QObject:
        """The `GitController` exposed to QML.

        QML binds the file tree's `statusProvider` to this object via the
        gitProviderAdapter wrapper. Marked `constant=True` because the
        object identity doesn't change over the controller's lifetime —
        only its internal `repoRoot` does.
        """
        return self._git_controller

    @Property(QObject, constant=True)
    def gitStatusList(self) -> QObject:
        """Flat list model of changed files for the Active Changes panel.

        Same identity-stable contract as `gitController` — the model
        updates its contents via `modelReset` on every controller scan,
        but the object itself is created once at startup.
        """
        return self._git_status_list

    @Slot()
    def _sync_git_repo_root(self) -> None:
        """Push the displayed root into the git controller.

        Connected to `displayedRootChanged` (NOT `cwdChanged` — anchoring
        pins git operations to the anchored root even as the raw cwd
        wanders, which IS the user-facing payoff of anchoring). Reads
        `displayedRoot` so the same code path serves both anchored and
        unanchored states without branching. Idempotent on equal values
        — `GitController.set_repo_root` returns early when the new path
        matches the current one, so the no-op-on-cwd-change-while-anchored
        path costs one comparison.
        """
        self._git_controller.set_repo_root(self.displayedRoot)

    @Slot()
    def focus_tree(self) -> None:
        """Ask QML to move active focus into the FileTreeView.

        Emits `focusTreeRequested` rather than reaching into QML
        directly — keeps the Python side stateless about QML focus
        ownership. Main.qml's Connections block calls
        `fileTreeView.forceActiveFocus()` on receipt.
        """
        self.focusTreeRequested.emit()

    @Slot(dict)
    def _on_tree_event(self, payload: dict) -> None:
        """Route Lua-emitted tree rpcnotify events.

        Payload shapes:
          { op: "focus" }
          { op: "debug", event: "keymap_install", keys, reason }
        Only one user-facing op today (focus tree from `<leader>tf`);
        shape mirrors `_on_fm_event` so a future `{op: "toggle"}` or
        `{op: "reveal_current"}` is a single-line dispatch addition.
        """
        op = str(payload.get("op") or "").lower()
        if op == "focus":
            self.focus_tree()
        elif op == "debug":
            event = str(payload.get("event") or "")
            log.debug("tree debug: %s %r", event, payload)
        else:
            log.warning("tree event with unknown op: %r", payload)

    @Slot()
    def focus_editor(self) -> None:
        """Ask QML to move active focus into the NvimView.

        Mirror of `focus_tree`. Called from:
        (a) `_on_nav_event` when nvim spillover targets the editor
            (no `_NAV_FROM_EDITOR` entry maps to "editor" today, but
            the path is wired for future docks left/up/down).
        NOTE: the QML Ctrl+H ApplicationShortcut calls
        `editor.forceActiveFocus()` directly and does NOT route through
        this slot. Keep them in sync when adding new nav targets.
        Emits the signal rather than touching QML focus directly,
        matching the project-standards §4 pattern for cross-layer
        focus asks.
        """
        self.focusEditorRequested.emit()

    # --- Phase 2.5 central-surface swap ----------------------------------
    #
    # The terminal pane and the editor pane both live under Main.qml's
    # `mainContent` Item. `centralSurface` toggles which is visible;
    # `editorVisible` and `terminalVisible` are derived booleans for
    # convenience in QML bindings (single notify signal keeps them in
    # lockstep — no XOR drift). Swap slots are no-op-on-noop so a
    # spurious chord press doesn't churn `centralSurfaceChanged`.

    @Property(str, notify=centralSurfaceChanged)
    def centralSurface(self) -> str:
        return self._central_surface

    @Property(bool, notify=centralSurfaceChanged)
    def editorVisible(self) -> bool:
        return self._central_surface == "editor"

    @Property(bool, notify=centralSurfaceChanged)
    def terminalVisible(self) -> bool:
        return self._central_surface == "terminal"

    @Slot()
    def swap_to_terminal(self) -> None:
        """Make the terminal pane the visible central surface.

        Idempotent: if terminal is already visible, no signal fires.
        Bound to the IDE-wide `Ctrl+Shift+T` Shortcut in Main.qml (PR 5).
        """
        if self._central_surface == "terminal":
            return
        self._central_surface = "terminal"
        self.centralSurfaceChanged.emit()

    @Slot()
    def swap_to_editor(self) -> None:
        """Make the editor pane the visible central surface.

        Idempotent: if editor is already visible, no signal fires.
        Bound to the IDE-wide `Ctrl+Shift+E` Shortcut in Main.qml (PR 5).
        """
        if self._central_surface == "editor":
            return
        self._central_surface = "editor"
        self.centralSurfaceChanged.emit()

    @Slot()
    def focus_terminal(self) -> None:
        """Ask QML to move active focus into the TerminalView.

        Symmetric counterpart of `focus_editor` / `focus_tree`. Emits
        the signal rather than touching QML focus directly — Main.qml's
        Connections block calls `terminalView.forceActiveFocus()` on
        receipt.
        """
        self.focusTerminalRequested.emit()

    @Slot()
    def _on_terminal_closed(self) -> None:
        """Log when the shell process exits.

        v1 behavior is just to log — the user's next swap-to-editor
        gives them a working editor; the dead terminal pane stays
        visible (last frame frozen) until then. A v2 enhancement could
        auto-swap to editor here.
        """
        log.info("terminal shell process exited")

    @Slot(str)
    def _on_terminal_osc7(self, path: str) -> None:
        """Route an OSC 7 cwd announcement from the terminal pane into
        the same `cwd` capsule machinery nvim's `:cd` uses.

        Phase 2.5 deliverable 3 — the final piece of terminal-driven
        cwd sync. The terminal reader thread parses OSC 7 sequences
        emitted by the chpwd hook (zsh) or PROMPT_COMMAND hook (bash)
        installed by `runtime/symmetria-shell/`. Each parsed path
        arrives here as the `osc7_received` signal payload.

        Synthesizing the `{id:"cwd", value:path}` capsule dict and
        dispatching through `_route_capsule` is the load-bearing
        design choice: it means every downstream consumer (the anchor
        state machine in `_route_capsule`'s cwd branch, the file
        tree's `displayedRoot` binding, the git controller's repo-root
        rebind) sees terminal-driven cwd updates through their
        existing connections. Identical code path to nvim's `:cd` —
        no duplication, no parallel routing tree.

        The path is already normalized by `_parse_osc7` (trailing
        slash stripped, root-only filtered) so no further sanitation
        is needed here. An empty path would update `_cwd` to `""`,
        which the `if new_cwd != self._cwd` guard in `_route_capsule`
        treats as a real change — guard against that explicitly.
        """
        if not path:
            log.debug("dropping empty terminal OSC 7 path")
            return
        self._route_capsule({"id": "cwd", "value": path})

    @Slot(dict)
    def _on_nav_event(self, payload: dict) -> None:
        """Route Lua-emitted nav rpcnotify events.

        Payload shapes (mirrors tree/fm event vocabulary):
          { op: "move", dir: "left"|"right"|"up"|"down" }
          { op: "debug", event: "keymap_install", reason: ... }

        `move` events always source from the editor — Lua only emits
        nav at the edge of an nvim split, and our only Lua-side
        keymap surface is the editor. The destination is looked up
        in `_NAV_FROM_EDITOR`; missing directions silently no-op
        (matches vim-tmux-navigator's no-$TMUX behavior — better
        than bell-ringing at a non-existent neighbor).
        """
        op = str(payload.get("op") or "").lower()
        if op == "move":
            direction = str(payload.get("dir") or "").lower()
            target = self._NAV_FROM_EDITOR.get(direction)
            if target == "tree":
                self.focusTreeRequested.emit()
            # else: no outer neighbor in this direction, silently ignore
            # (target == "editor" is reserved for future docks that sit
            # to the editor's left/up/down; no entry in _NAV_FROM_EDITOR
            # maps to that value today, so the branch would be unreachable).
        elif op == "debug":
            event = str(payload.get("event") or "")
            log.debug("nav debug: %s %r", event, payload)
        else:
            log.warning("nav event with unknown op: %r", payload)

    @Slot(dict)
    def _on_anchor_event(self, payload: dict) -> None:
        """Route Lua-emitted anchor rpcnotify events.

        Payload shapes:
          { op: "set", path?: string }
          { op: "clear" }

        `set` without `path` falls back to `anchor_to_current_cwd` —
        the user typed `:SymmetriaAnchor` with no arg, meaning "anchor
        wherever the file tree currently shows". The empty-string check
        AFTER the `.get` is the right shape (the missing-key case and
        the explicit-empty case both deserve the cwd fallback; only a
        non-empty string should go through `anchor_to_path`). Anchor
        is the IDE-level concern, NOT a nvim concept — the PRIMARY
        trigger is a Qt application-scope shortcut in Main.qml. This
        handler exists for the scripted/macro surface.
        """
        op = str(payload.get("op") or "").lower()
        if op == "set":
            path = str(payload.get("path") or "")
            if path:
                self.anchor_to_path(path)
            else:
                self.anchor_to_current_cwd()
        elif op == "clear":
            self.release_anchor()
        else:
            log.warning("anchor event with unknown op: %r", payload)

    def _nvim_cwd_or_home(self) -> str:
        """Default initial path for the FM overlay when none is provided.

        The Lua keybind in runtime/init.lua already passes the buffer's
        parent directory via `rpcnotify(0, "fm", { initialPath = … })`,
        so this fallback is reached only when the keybind hasn't been
        wired up or no buffer is loaded. $HOME is the safe default.

        If we ever need nvim's actual cwd here, subscribe to nvim's
        DirChanged autocmd and cache the path on AppController — don't
        block the GUI thread on a synchronous RPC.
        """
        return os.path.expanduser("~")

    @Slot(dict)
    def _on_fm_event(self, payload: dict) -> None:
        """Route Lua-emitted fm rpcnotify events.

        Payload shapes:
          { op: "show"|"hide"|"toggle", initialPath?: string }
          { op: "debug", event: "keymap_install", keys, reason }
        """
        op = str(payload.get("op") or "").lower()
        initial_path = str(payload.get("initialPath") or "")
        if op == "show":
            self.show_fm(initial_path)
        elif op == "hide":
            self.hide_fm()
        elif op == "toggle":
            if self._fm_visible:
                self.hide_fm()
            else:
                self.show_fm(initial_path)
        elif op == "debug":
            # Diagnostic trail from the Lua-side install_fm_keymap
            # (keymap reinstall observability). Mirrors `_on_agent_event`'s
            # debug branch — log at DEBUG, don't WARNING-spam the app log
            # on every BufEnter.
            event = str(payload.get("event") or "")
            log.debug("fm debug: %s %r", event, payload)
        else:
            log.warning("fm event with unknown op: %r", payload)

    def _on_session_event_for(self, slot: int, event: dict) -> None:
        """Indexed event router — handles OFF edges + permission tracking.

        The `_create_instance` lambda binds `slot` at connect time so
        each sidecar's events route to the right pool entry's state
        regardless of focus. Three cases handled here:

        - `result`: turn-complete; flip the slot's spinner OFF.
        - `permission_mode_changed`: mirror the sidecar's authoritative
          mode into the slot's per-instance state (cycle slot doesn't
          mutate optimistically — see `cycle_permission_mode_for` for
          rationale).
        - `permission_request`: record the issuing slot in
          `_pending_permissions` so `respond_to_permission` can route
          the user's decision back to the right sidecar even if focus
          changes between request and response (Phase B scenario,
          Phase A risk #1 mitigation).

        Pending permission requests do NOT clear the spinner: the
        sidecar's canUseTool callback is awaiting our reply, and the
        turn is still in flight from the user's perspective.
        """
        kind = str(event.get("type") or "")
        if kind == "result":
            self._set_awaiting_response_for(slot, False)
        elif kind == "permission_mode_changed":
            self._set_permission_mode_for(slot, str(event.get("mode") or ""))
        elif kind == "permission_request":
            req_id = str(event.get("request_id") or "")
            if req_id:
                self._pending_permissions[req_id] = slot

    def _on_session_closed_for(self, slot: int) -> None:
        """Indexed close handler — subprocess for `slot` exited.

        Crashes, SIGTERM from `<leader>aN`, or auth failures all reach
        the GUI through `closed` rather than a `result` envelope.
        Without this slot the spinner would stay lit indefinitely after
        a crash.

        Resets the slot's permission mode to `default` so the next
        session (e.g. after `<leader>aN`) starts with the canonical
        pill rather than briefly inheriting the stale mode from the
        dead subprocess. Drops any `_pending_permissions` entries the
        dead sidecar issued — they will never be answered now that
        canUseTool's promise has been auto-rejected by SDK abort.
        """
        self._set_awaiting_response_for(slot, False)
        self._set_permission_mode_for(slot, "default")
        self._pending_permissions = {
            req_id: issuing_slot
            for req_id, issuing_slot in self._pending_permissions.items()
            if issuing_slot != slot
        }

    def _coerce_slot_index(self, raw: object) -> int | None:
        """Parse a payload's `index` field into a valid pool slot.

        Returns None for missing / 0 / non-integer / out-of-range values
        so callers can fall back to a default behavior (e.g. "focused
        instance" semantics for `op=close`). Out-of-range logs at
        WARNING because that means the Lua side dispatched a slot the
        IDE can't honor — visible signal of a future-N keybind drift.
        """
        if raw is None:
            return None
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None
        if value <= 0:
            return None
        if value > self._MAX_INSTANCES:
            log.warning(
                "_coerce_slot_index: %d > _MAX_INSTANCES=%d — dropped",
                value,
                self._MAX_INSTANCES,
            )
            return None
        return value

    @Slot(dict)
    def _on_agent_event(self, payload: dict) -> None:
        """Route a Lua-emitted agent lifecycle event.

        Payload shape: `{op: "show"|"hide"|"toggle"|"focus"|"close"|"debug",
        ...}`. Unknown ops log at DEBUG and no-op — additive protocol
        evolution doesn't crash the controller.

        Phase B dispatch table (PRD §5.1):

        - `op="show"`, `action="new"` → spawn into next free slot (1..5)
          and focus it. Pool full = warn + focus the highest-numbered
          existing slot (slot 5 in the saturated case).
        - `op="show"` without `action` → just open the pane on the
          currently-focused instance. No spawn.
        - `op="focus"`, `index=N` → switch focus to slot N. No-op if
          slot N is empty (per PRD B2 — focus does not spawn).
        - `op="close"` → close the focused instance. With `index=N`,
          close that specific slot. After close: refocus to the
          next-lowest occupied slot (PRD §5.3 walk-down-then-up rule),
          or hide the pane if the pool emptied.
        - `op="hide"` / `op="toggle"` → pane visibility only, no
          per-instance impact.
        - `op="debug"` → diagnostic trail from the Lua side
          (keymap-install attempts, orchestrator race observations).
          Payload carries an `event` string discriminating category.
        """
        op = str(payload.get("op") or "").strip()
        if op == "show":
            self._handle_agent_show(payload)
        elif op == "focus":
            self._handle_agent_focus(payload)
        elif op == "close":
            self._handle_agent_close(payload)
        elif op == "hide":
            self.hide_agent()
        elif op == "toggle":
            self.toggle_agent()
        elif op == "debug":
            event = str(payload.get("event") or "")
            log.debug("agent debug: %s %r", event, payload)
        else:
            log.debug("unhandled agent op: %r", op)

    def _handle_agent_show(self, payload: dict) -> None:
        """`op=show` dispatch — Phase B's spawn-into-next-free path.

        With `action="new"`, allocate a fresh slot from
        `_next_free_slot()` and focus it. When the pool is saturated
        (5/5), fall back to focusing the highest existing slot rather
        than a hard error — the user's intent ("give me a new
        Claude") can't be honored, but routing them to the most
        recently spawned slot is the least surprising failure mode.

        Without `action`, just call `show_agent` so the pane becomes
        visible on whatever instance is already focused — used by the
        env-var startup paths and by future `op=show` invocations
        that want to surface the existing transcript.
        """
        action = str(payload.get("action") or "").strip()
        if action == "new":
            free_slot = self._next_free_slot()
            if free_slot is not None:
                log.info("agent op=show action=new: spawning slot %d", free_slot)
                self._spawn_instance(free_slot)
                self.focus_instance(free_slot)
            else:
                # Pool saturated. Highest-numbered slot is the most-
                # recently-spawned (slots fill from the bottom), so
                # focusing it routes the user to their newest session.
                fallback = max(self._session_hosts.keys())
                log.warning(
                    "agent op=show action=new: pool full (1..%d occupied) "
                    "— focusing slot %d",
                    self._MAX_INSTANCES,
                    fallback,
                )
                self.focus_instance(fallback)
        self.show_agent()

    def _handle_agent_focus(self, payload: dict) -> None:
        """`op=focus` dispatch — `<C-1>..<C-5>`.

        Per PRD B2, focusing a non-existent slot is a no-op-with-log
        (decided NOT to auto-spawn — matches orchestrator.nvim and
        avoids accidental spawn from a held keybind).
        """
        index = self._coerce_slot_index(payload.get("index"))
        if index is None:
            log.warning("agent op=focus: missing/invalid index — no-op")
            return
        # `focus_instance` handles the "already focused" + "slot empty"
        # no-ops with their own logs — keep this dispatcher thin.
        self.focus_instance(index)

    @Slot()
    def close_focused_instance(self) -> None:
        """QML-facing close — used by the agent pane's Ctrl+Shift+Q binding.

        Mirrors the editor-side `<C-S-q>` keymap installed in
        `runtime/init.lua`, which dispatches via `agent_event` →
        `_handle_agent_close`. The QML composer / pane chrome can't reach
        nvim's keymap system (focus sits on a TextField), so a parallel
        QML→Python bridge is required.

        Routes through `_handle_agent_close({})` rather than calling
        `_close_instance` directly — `_handle_agent_close` carries the
        refocus + empty-pool + signal-emit semantics that QML's chrome
        relies on (chip strip empties, spinner clears, pill resets).
        """
        self._handle_agent_close({})

    def _handle_agent_close(self, payload: dict) -> None:
        """`op=close` dispatch — `<C-S-q>` (or future explicit-index variants).

        Missing index = focused instance. Refocus selection uses
        `_next_focus_after_close` (walk down then up). Empty pool
        after close hides the pane and snaps `_focused_instance` back
        to 1 so the next `<leader>aN` press lands cleanly at slot 1.
        """
        raw_index = payload.get("index")
        if raw_index is None:
            target = self._focused_instance
        else:
            target = self._coerce_slot_index(raw_index)
            if target is None:
                log.warning("agent op=close: invalid index %r — no-op", raw_index)
                return
        if target not in self._session_hosts:
            log.warning("agent op=close: slot %d not in pool — no-op", target)
            return
        was_focused = target == self._focused_instance
        self._close_instance(target)
        if not self._session_hosts:
            # Pool empty — pane has nothing to display. Hide it and
            # reset `_focused_instance` to 1 so the next spawn lands
            # at slot 1 (matching cold-start behavior).
            self.hide_agent()
            self._focused_instance = (
                1  # idempotent; assignment is harmless when already 1
            )
            # Always re-emit the focus-tracking signals: the pool is
            # gone so the QML indicator, spinner, and permission pill
            # must all reflect the empty-pool defaults (1 / 0, False,
            # "default"), even when _focused_instance was already 1
            # before the close. Skipping the emit when already at 1
            # would leave QML with stale spinner/pill state from the
            # now-dead slot 1 sidecar.
            self.focusedInstanceChanged.emit()
            self.awaitingResponseChanged.emit()
            self.permissionModeChanged.emit()
            return
        if was_focused:
            new_focus = self._next_focus_after_close(target)
            if new_focus is not None:
                self.focus_instance(new_focus)

    @Slot(str, int)
    def submit_prompt_for(self, prompt: str, index: int) -> None:
        """Indexed composer-submit — route a prompt to a specific pool slot.

        Two branches per the original `submit_prompt`:

        - **Hot** (sidecar alive — the normal path after pre-warm):
          `send_user_message(prompt)` writes a `user_message` command
          on the already-open stdin stream. The SDK's session state
          (model context, tool authorization) is retained inside the
          sidecar so the pane reads as a conversation rather than
          independent one-shots.
        - **Cold** (sidecar not running — defensive fallback):
          `start(prompt)` spawns the Node sidecar and delivers `prompt`
          as the first JSONL `user_message` command. Reached only if
          `AppController.start()` was not called or a previous
          `stop()` wasn't followed by a re-warm.

        Before either branch, we feed a synthetic `user` event into
        the SLOT'S model so the message appears in the pane the instant
        the composer submits. The sidecar deliberately drops
        SDKUserMessage echoes (Python's optimistic-render is the single
        source of truth); without this synthetic injection the user
        would have no visual confirmation of what they typed.

        Caller responsibility: the slot must exist in the pool.
        Dispatch from QML goes through `submit_prompt(prompt)` which
        always targets the focused instance (always present).
        """
        text = prompt.strip()
        if not text:
            return
        if index not in self._session_hosts:
            log.warning("submit_prompt_for: slot %d not in pool — dropped", index)
            return
        # Capture the first prompt as this slot's title, never overwriting.
        # Mirrors orchestrator.nvim's set-once semantic for OSC-2-driven
        # titles. The AgentTopBar chip reads `controller.instanceTitles`
        # to render `<slot> │ <title>` once a title exists; before this
        # the chip shows only the slot number.
        if index not in self._instance_titles:
            self._instance_titles[index] = self._derive_title(text)
            self.instanceTitlesChanged.emit()
        host = self._session_hosts[index]
        model = self._session_models[index]
        # Optimistic local rendering. `apply` is the same slot wired
        # to `event_received` via QueuedConnection, but we're already
        # on the GUI thread here so a direct call is safe and cheaper
        # than detouring through the queue.
        model.apply({"type": "user", "message": {"role": "user", "content": text}})
        # ON edge for the spinner. Set BEFORE dispatching to the host
        # so the UI flips into "thinking" the same frame the user
        # message renders — no perceptible gap between submit and
        # acknowledgement.
        self._set_awaiting_response_for(index, True)
        if host.is_running:
            log.info("submit_prompt slot=%d (continue): %s", index, text[:100])
            host.send_user_message(text)
        else:
            log.info("submit_prompt slot=%d (new session): %s", index, text[:100])
            host.start(text)

    @Slot(str)
    def submit_prompt(self, prompt: str) -> None:
        """Wrapper for QML's composer — routes to the focused instance."""
        self.submit_prompt_for(prompt, self._focused_instance)

    @Slot(str, str, int)
    def respond_to_permission_for(
        self, request_id: str, decision: str, index: int
    ) -> None:
        """Indexed permission-response dispatch.

        Order matters: the sidecar gets the response first so its
        canUseTool promise resolves and the SDK can proceed (or deliver
        the deny tool_result). Then we mark the model row as
        approved/denied so the UI gives immediate feedback even if the
        sidecar's next event takes a moment to arrive.

        Validates `decision` and silently drops invalid values to keep
        the QML invocation surface tolerant of typos. Removes the
        request_id from `_pending_permissions` regardless of whether
        the slot is still present (defensive — a session-close race
        could remove the slot between request and response).
        """
        if decision not in ("allow", "deny"):
            log.warning(
                "respond_to_permission_for: invalid decision %r — dropped",
                decision,
            )
            return
        if index not in self._session_hosts:
            log.warning(
                "respond_to_permission_for: slot %d not in pool — dropped", index
            )
            self._pending_permissions.pop(request_id, None)
            return
        self._pending_permissions.pop(request_id, None)
        self._session_hosts[index].send_permission_response(request_id, decision)
        self._session_models[index].resolve_permission(request_id, decision)

    @Slot(str, str)
    def respond_to_permission(self, request_id: str, decision: str) -> None:
        """Wrapper for QML — looks up the issuing slot via `_pending_permissions`.

        The sidecar that issued the `permission_request` is the one
        whose `canUseTool` promise needs to resolve — not necessarily
        the focused slot (in Phase B, the user could focus-switch
        between request and response). The lookup is authoritative;
        falling back to `_focused_instance` would silently route the
        decision to the wrong sidecar.

        Decision validation is delegated to `respond_to_permission_for` —
        no duplicate check here to keep the wrapper a thin routing layer.
        """
        slot = self._pending_permissions.get(request_id)
        if slot is None:
            log.warning(
                "respond_to_permission: request_id %r not in pending map — "
                "dropped (sidecar may have already aborted the request)",
                request_id,
            )
            return
        self.respond_to_permission_for(request_id, decision, slot)

    def start(self) -> None:
        self._backend.start()
        # Phase 2.5 terminal pane — pre-warm eagerly AFTER nvim has
        # started. nvim ordering gates the QSGRenderThread's first
        # frame; spawning the terminal first can briefly flash an
        # empty editor on slow hardware. Eager pre-warm matches Q1-1b
        # (nvim is also pre-spawned) so the first user chord swap is
        # instant in either direction. Failures from the spawn (shell
        # missing, fd-limit, etc.) propagate as OSError per the
        # TerminalBackend.start() docstring; we log and continue so
        # the IDE still launches without a working terminal pane.
        try:
            self._terminal_backend.start(self._cwd)
        except OSError:
            log.exception("terminal backend pre-warm failed — pane will be inert")
        # Pool stays empty unless an env-var path explicitly opts in.
        # The first interactive `<leader>aN` lazily spawns slot 1 via
        # `_handle_agent_show("new")` → `_next_free_slot()` returns 1
        # → `_spawn_instance(1)`. Permission-mode cycling pre-first-
        # message no longer needs a pre-warm: the sidecar's local
        # `currentMode` is authoritative (CLAUDE.md gotcha #25), so
        # Shift+Tab works the moment the user opens the pane (which
        # cannot happen without spawning a slot first).
        #
        # Two env-var startup paths still need a slot to land on:
        #   SYMMETRIA_IDE_AGENT_PROMPT="..." — spawn slot 1 with the
        #     given prompt AND open the agent view. Used by headless
        #     smoke tests; equivalent to the user pressing <leader>aN
        #     and immediately typing the prompt.
        #   SYMMETRIA_IDE_AGENT_VIEW=1      — spawn slot 1 (empty)
        #     and open the agent view ready for interactive typing.
        # Neither set = classic editor-only workflow with empty pool.
        prompt = os.environ.get("SYMMETRIA_IDE_AGENT_PROMPT") or ""
        want_view = bool(prompt) or os.environ.get("SYMMETRIA_IDE_AGENT_VIEW") == "1"
        if want_view:
            # `_create_instance(1)` allocates host + model + per-instance
            # state without spawning a subprocess. `submit_prompt`'s cold
            # branch (`is_running == False`) then drives `host.start(prompt)`
            # so the first user_message and the subprocess spawn are a
            # single SDK exchange — no separate empty pre-warm write.
            # For the empty-prompt VIEW path, `_spawn_instance` does the
            # subprocess warm-up directly so the pane has something to
            # focus on when shown.
            if prompt:
                log.info("SYMMETRIA_IDE_AGENT_PROMPT set — spawning slot 1 cold")
                self._create_instance(1)
                self.submit_prompt(prompt)
            else:
                log.info("SYMMETRIA_IDE_AGENT_VIEW set — pre-warming slot 1")
                self._spawn_instance(1)
            self.show_agent()
        self.backendReady.emit()

    def shutdown(self) -> None:
        # Stop every pooled session host first — the subprocesses are
        # the noisier of the two and we'd rather have their workers
        # joined before nvim's shutdown handshake owns the event loop.
        # Iterate over a snapshot in case `stop()` mutates the dict
        # (it shouldn't, but defensive iteration costs nothing).
        for host in list(self._session_hosts.values()):
            host.stop()
        # Stop the git worker before nvim — its scan is fast (≤1s join) and
        # we want it joined before the event loop tears down so its
        # cross-thread emit can't fire into a half-destroyed receiver.
        self._git_controller.stop()
        # Stop the terminal BEFORE nvim — reverse of startup order, and
        # prevents the terminal reader thread's queued signals (closed,
        # screen_dirty) from landing against a scene graph that's mid-
        # nvim-teardown. NvimBackend.stop() blocks in a threading.join()
        # for up to 1 s; completing terminal teardown (including the
        # killpg that reaps nested TUIs like vim/htop) before that
        # blocking join keeps shutdown predictable and avoids the terminal
        # reader racing nvim's channel close.
        self._terminal_backend.stop()
        self._backend.stop()

    @property
    def backend(self) -> NvimBackend:
        return self._backend

    @property
    def terminalBackend(self) -> TerminalBackend:
        """The Phase 2.5 terminal backend, exposed as a QML context
        property by `_build_engine`. Main.qml binds it into TerminalView
        the same way nvimBackend is bound into NvimView."""
        return self._terminal_backend

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
    _ = TerminalView
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
    ctx.setContextProperty("terminalBackend", controller.terminalBackend)
    ctx.setContextProperty("capsuleModel", controller.capsules)
    ctx.setContextProperty("statusState", controller.status)
    ctx.setContextProperty("cmdlineState", controller.cmdline)
    ctx.setContextProperty("popupmenuModel", controller.popupmenu)
    ctx.setContextProperty("completionModel", controller.completion)
    ctx.setContextProperty("whichKeyState", controller.whichkey_state)
    ctx.setContextProperty("whichKeyModel", controller.whichkey_model)
    # Git status provider — exposed as its own context property so QML can
    # bind both the file tree's `statusProvider` and the (forthcoming)
    # Active Changes panel to the same object. Equivalent to
    # `controller.gitController` (the @Property) but binding-friendly: a
    # property-of-a-property doesn't re-evaluate when the outer object
    # changes identity, whereas a context property is rebound implicitly.
    ctx.setContextProperty("gitController", controller.gitController)
    ctx.setContextProperty("gitStatusList", controller.gitStatusList)
    # NB: previously this block also exposed `sessionHost` and
    # `sessionModel` as context properties pointing at the focused
    # slot. Those have been removed for two reasons:
    #   1. The agent pane now binds against `controller.sessionModelForFocused`
    #      (a QObject @Property with notify=focusedInstanceChanged) so it
    #      re-binds on focus switch — context properties are evaluated
    #      ONCE at engine load and would keep pointing at stale slot 1.
    #   2. The pool is empty at IDE launch (lazy spawn on first
    #      `<leader>aN`). Dereferencing `_session_hosts[_focused_instance]`
    #      here would KeyError before the user has spawned anything.
    # Nothing in QML still binds against the old names — they were
    # carry-over from Phase A's single-instance topology.

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
    # Install the Qt message handler BEFORE QGuiApplication so any QML
    # warnings emitted during engine construction reach Python's logging.
    # Without this, PySide6's default handler silently drops console.log
    # and most qWarnings — turning silent QML failures into mystery bugs.
    qInstallMessageHandler(_qt_message_handler)
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
