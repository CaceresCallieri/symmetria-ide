"""QApplication wiring: spawns the NvimBackend RPC client, loads the QML scene.

This is the boundary between Python backend code and the QML UI. The editor
nvim + shell run inside QMLTermWidget panes (the forked qmltermwidget) spawned
by QMLTermSessions in Main.qml; `NvimBackend` is the RPC-only client that
attaches to the editor nvim's --listen socket for the chrome relays. The QML
import module `Symmetria.Ide` is registered so that QML files can
`import Symmetria.Ide 1.0` and instantiate `MinimapView`, etc.

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
import shutil
import tempfile

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
from .trace import trace
from .cmdline_models import (  # noqa: F401 — side-effect: @QmlElement registration
    CmdlineState,
    CompletionModel,
)
from .git_controller import GitController, GitStatusListModel
from .minimap_model import MinimapModel
from .minimap_view import MinimapView  # noqa: F401 — side-effect: @QmlElement registration
from .editor_font import DEFAULT_FONT_POINT_SIZE, default_font
from .nvim_backend import _RUNTIME_DIR, NvimBackend
from .session_host import SessionHost
from .session_models import (  # noqa: F401 — side-effect: @QmlElement registration
    SessionModel,
)
from .tree_state_cache import load_expanded, save_expanded
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
    the editor is hidden and the `AgentPane` takes over. Driven
    entirely from the IDE side — `show_agent` / `hide_agent` /
    `toggle_agent` are the public entry points, callable from QML
    chrome (the composer's Escape press in `AgentPane.qml` calls
    `hide_agent`) and from tests. The Lua `<leader>aN` / `<C-1..5>` /
    `<C-S-q>` hijack path that previously routed through `agent_event`
    has been stripped so orchestrator.nvim's own keymaps own those
    slots inside the embedded NeoVim.
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
    # Per-project expanded-state cache. `expandedPathsCacheChanged` fires
    # right after a project switch (in `_sync_expanded_paths_cache`) so
    # QML can re-feed the FM's `restoreExpandedPaths` prop. The cache
    # itself is a list[str] of absolute paths; the FM treats null/empty
    # as "no restore, use lazyExpand cascade".
    expandedPathsCacheChanged = Signal()
    # QML-bound focus pull. Main.qml's Connections block listens for this
    # signal and calls `fileTreeView.forceActiveFocus()`. Emitted by the
    # `focus_tree()` Slot, which the Ctrl+J ApplicationShortcut in
    # qml/Main.qml dispatches. Carries no payload — it's a one-way ask.
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

    # Pool cap is policy, not a structural limit — `_session_hosts` is
    # a sparse dict, so widening to N is just bumping this constant.
    # 5 matches the originally planned `<C-1>..<C-5>` keybind surface;
    # Track-2 may extend it.
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
        # Tracer one-shot flags — set true after the first capsule /
        # first cwd capsule / first ignored set has been observed, so
        # the trace emits exactly once each per launch.
        self._first_capsule_seen = False
        self._first_displayed_root_traced = False
        # NeoVim runs as a TUI inside a QMLTermWidget editor surface (spawned
        # by the QMLTermSession in Main.qml with `nvim --listen <sock>`).
        # `_backend` is an RPC-only connection to that socket: control
        # (input/edit_file/set_current_dir) + the chrome rpcnotify relays
        # (capsule/cmdline/whichkey/minimap/...). It does NOT render — the
        # terminal widget draws nvim's grid; the IDE renders the chrome
        # overlays from the relayed channels. The shell pane is a second
        # QMLTermWidget, also QML-spawned. No Python TerminalBackend exists for
        # either pane after the qmltermwidget migration.
        self._nvim_socket = os.path.join(
            tempfile.mkdtemp(prefix="symmetria-nvim-"), "nvim.sock"
        )
        self._backend = NvimBackend(self._nvim_socket)
        # Per Q2-d topology decision: terminal is the persistent home
        # surface, the editor is summoned over it. First-launch = terminal.
        self._central_surface: str = "terminal"
        self._status = StatusBarState(self)
        self._capsules = CapsuleModel(self)
        self._cmdline = CmdlineState(self)
        self._completion = CompletionModel(self)
        self._whichkey_state = WhichKeyState(self)
        self._whichkey_model = WhichKeyModel(self)
        # Editor minimap content model — Phase 1 of docs/minimap-prd.md.
        # Receives full-buffer snapshots from the Lua emitter
        # (runtime/lua/orchestrator/minimap.lua) via `NvimBackend.minimap_event`.
        # Exposed to QML as `minimapModel` context property in _build_engine
        # so MinimapView's bufferRowCount can bind to its lineCount.
        self._minimap_model = MinimapModel(self)
        # ----- Per-instance pools (multi-instance foundation) -----------
        #
        # Slot-keyed dicts let Track-2 chord wirings drop in multi-spawn
        # and focus switching without a second plumbing refactor.
        # Today the pool contains at most one slot (spawned lazily on
        # first user action or via env-var); the dict shape already
        # supports slots 1..5 when Track-2 activates them.
        #
        # Slot numbering is 1-based: 1..5 are valid. Per-instance scalar state
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
        # Currently locked at 1 (only one instance spawned at a time).
        # Track-2 chord wirings will call `focus_instance(N)` to reassign.
        self._focused_instance: int = 1
        # request_id -> issuing slot. Populated when a `permission_request`
        # event lands so `respond_to_permission` can route the user's
        # decision back to the right sidecar's `canUseTool` resolver
        # without trusting `_focused_instance` (the user could focus-switch
        # between request and response in a multi-instance scenario). Cleared
        # on response or session close. Keyed here so `respond_to_permission`
        # never needs updating when multi-instance activates.
        self._pending_permissions: dict[str, int] = {}
        # NB: pool starts EMPTY. Previously `__init__` auto-allocated
        # slot 1 (so the bubble strip read as "1 active" before the
        # user had asked for any agent), and `start()` pre-warmed its
        # subprocess. Both violated the user's mental model — "no
        # agents until I spawn one". The first Track-2 chord or
        # env-var startup path spawns slot 1 lazily via
        # `_spawn_instance(1)`; subsequent spawns fill slots 2..5.
        # The env-var startup paths (`SYMMETRIA_IDE_AGENT_PROMPT` /
        # `_VIEW`) handle the spawn themselves in `start()`.
        # ----- Backend signal wiring (chrome rpcnotify relays) -----------
        # These signals originate on the NvimBackend worker thread; Qt's
        # auto-connection promotes them to QueuedConnection across the
        # thread boundary (same as the embed model used). The minimap
        # connects below are explicit-queued per §4 P2.
        self._backend.capsule_updated.connect(self._route_capsule)
        self._backend.cmdline_updated.connect(self._cmdline.apply)
        self._backend.completions_updated.connect(self._completion.apply)
        # Both whichkey consumers listen to the same payload — state
        # handles visibility/trail, model handles the items list.
        self._backend.whichkey_event.connect(self._whichkey_state.apply)
        self._backend.whichkey_event.connect(self._whichkey_model.apply)
        # queued: minimap_event originates on the pynvim worker thread;
        # MinimapModel mutates list state read by the GUI-thread painter,
        # so the connection must marshal to the GUI thread explicitly
        # (§4 P2 — same rule as session_host's event hop).
        self._backend.minimap_event.connect(
            self._minimap_model.apply, Qt.ConnectionType.QueuedConnection
        )
        # queued: minimap_viewport_event also originates on the pynvim
        # worker thread (§4 P2). Drives the Phase 3 viewport indicator.
        self._backend.minimap_viewport_event.connect(
            self._minimap_model.apply_viewport, Qt.ConnectionType.QueuedConnection
        )
        # queued: minimap_diagnostics_event + minimap_git_event likewise
        # cross from the pynvim worker thread (§4 P2). Both drive the
        # Phase 4 left-edge gutter; separated so listeners that only
        # care about one of them don't re-evaluate on the other.
        self._backend.minimap_diagnostics_event.connect(
            self._minimap_model.apply_diagnostics,
            Qt.ConnectionType.QueuedConnection,
        )
        self._backend.minimap_git_event.connect(
            self._minimap_model.apply_git, Qt.ConnectionType.QueuedConnection
        )
        # Agent-pane visibility lives on the IDE side only — the Lua-driven
        # `agent_event` rpcnotify channel was stripped when orchestrator.nvim
        # took back ownership of `<leader>aN` / `<C-1..5>` / `<C-S-q>`. The
        # `_on_agent_event` dispatcher below remains callable from tests and
        # from future Track-2 QML chord wirings.
        self._agent_visible = False
        # File manager toggle-overlay lifecycle. Lua may emit via rpcnotify
        # in the future (no live emitter today); NvimBackend re-emits as
        # fm_event, this controller owns the state. The panel itself is a
        # QML overlay over the editor surface (not a separate window).
        self._backend.fm_event.connect(self._on_fm_event)
        self._fm_visible = False
        self._fm_initial_path = ""
        # File-tree sidebar focus is driven entirely from QML
        # (Ctrl+J ApplicationShortcut → focus_tree()); the Lua `<leader>tf`
        # → `tree` rpcnotify path was stripped alongside the agent hijacks.
        self._backend.nav_event.connect(self._on_nav_event)
        # queued: NvimBackend worker → AppController GUI. Secondary surface
        # for the project-anchor concept (the primary is a Qt application-
        # scope shortcut in Main.qml). Lua's `:SymmetriaAnchor` /
        # `:SymmetriaUnanchor` user commands emit through this channel.
        self._backend.anchor_event.connect(self._on_anchor_event)
        # Shell-driven cwd updates now arrive via the QMLTermSession's native
        # `currentDir` (polled by a Timer in Main.qml → `on_shell_cwd`), not a
        # terminal reader thread / OSC 7 signal. The shell's exit is handled in
        # QML (log-only `onFinished`); there is no Python terminal backend to
        # connect lifecycle signals from anymore.
        # Seed `cwd` with $HOME so QML's `rootPath: controller.cwd` has
        # a valid path during the brief window between QML construction
        # and the first capsule push from runtime/init.lua's VimEnter +
        # `symmetria_push_state` re-request (per CLAUDE.md gotcha #2).
        # Empty string here would trip FileTreeView's `if (rootPath !==
        # "")` guard and leave the sidebar showing "Empty" until the
        # capsule lands.
        #
        # `os.getcwd()` rather than `~`: the launch cwd is what the user
        # implicitly chose by running `cd <project> && python -m
        # symmetria_ide`, and matches what nvim's VimEnter capsule will
        # report (nvim inherits Python's cwd). It also seeds
        # `controller.displayedRoot`, which the QMLTermSessions bind as their
        # `initialWorkingDirectory` — so both the editor nvim and the shell
        # spawn in the launch project before any capsule arrives. Falls back
        # to `~` if `getcwd()` raises (deleted cwd — extreme edge case).
        try:
            self._cwd: str = os.getcwd()
        except OSError:
            self._cwd = os.path.expanduser("~")
        # Cache HOME once so displayedRootCompact doesn't re-expand on every
        # cwd change. os.path.expanduser("~") is a pure function of the
        # process environment and never changes at runtime.
        self._home: str = os.path.expanduser("~")
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
        # Per-project expanded-state cache (option 6). Populated synchronously
        # from disk in `_sync_expanded_paths_cache` whenever displayedRoot
        # changes — happens BEFORE `_sync_git_repo_root` because the QML
        # binding `restoreExpandedPaths: controller.expandedPathsCache`
        # must already hold the new list by the time the FM's
        # `onRootPathChanged` fires its mount cascade. Empty list = "no
        # cache for this project yet" (FM falls back to lazyExpand
        # cascade). The disk file lives at
        # `$XDG_STATE_HOME/symmetria-ide/projects/<hash>.json`; see
        # `tree_state_cache.py` for the format + atomic-write semantics.
        self._expanded_paths_cache: list[str] = []
        # Tracks the repo root that `_expanded_paths_cache` was loaded
        # for, so `saveExpandedPaths` writes back to the right file even
        # if `displayedRoot` is about to change. Loading is keyed off
        # `displayedRoot`; saving is keyed off this captured root.
        self._expanded_paths_cache_root: str = ""
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
        # Load the per-project expanded-paths cache FIRST so the QML
        # binding `restoreExpandedPaths: controller.expandedPathsCache`
        # holds the new list before the FM's `onRootPathChanged`
        # cascade runs. Connect order is load-bearing: Qt fires slots
        # in registration order, and the FM's binding update is driven
        # synchronously by `expandedPathsCacheChanged`, which fires
        # inside `_sync_expanded_paths_cache`. If we registered this
        # AFTER `_sync_git_repo_root`, the FM would see a stale cache
        # on project switch and run the empty-cache lazyExpand cascade.
        # Same-thread: `displayedRootChanged` fires on the GUI thread;
        # `load_expanded` is a synchronous file read with no Qt deps.
        self.displayedRootChanged.connect(self._sync_expanded_paths_cache)

        # CRITICAL: also populate the cache RIGHT NOW so the QML
        # binding `restoreExpandedPaths: controller.expandedPathsCache`
        # holds the saved list on its FIRST evaluation. The FM mounts
        # during `engine.load(Main.qml)`, which runs in `_build_engine`
        # — BEFORE `start()`'s synthetic `displayedRootChanged` emit.
        # Without this pre-population, the FM's `onRootPathChanged`
        # fires once at QML instantiation with `restoreExpandedPaths`
        # still at its initial empty value, the lazyExpand cascade
        # runs, and the later `_sync_expanded_paths_cache` slot fires
        # too late (rootPath has not changed, so the FM doesn't
        # remount). Reading `self.displayedRoot` here works because
        # `_anchored=False` at this point, so it equals `self._cwd`,
        # which was set from `os.getcwd()` just above — the same
        # value the FM will read when it mounts.
        self._sync_expanded_paths_cache()

        # same-thread: displayedRootChanged fires on the GUI thread; GitController.set_repo_root is GUI-only
        self.displayedRootChanged.connect(self._sync_git_repo_root)

        # Mirror the displayed root into nvim's `:pwd`. Without this, nvim's
        # working directory stays frozen at Python's launch-time cwd, so
        # file pickers / `:find` / `:term` spawned from nvim all target the
        # wrong project after the user has wandered. The slot itself marshals
        # the call through `nvim.async_call` (gotcha #1), so the connect is
        # a plain same-thread one — `displayedRootChanged` fires on the GUI
        # thread; `set_current_dir` is safe to invoke from there.
        # Deliberately non-destructive: no buffer wipe, no `:bd`. That layers
        # on later inside the session-open flow once that exists; for now we
        # just keep `:pwd` honest. See docs/vision.md "Modes of inhabiting
        # the IDE" for the dual-mode framing this lands inside.
        self.displayedRootChanged.connect(self._sync_nvim_cwd)

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
        the first paint frame). Track-2 chord wirings will call this
        with a fresh slot number and then immediately follow with
        `host.start("")` to pre-warm the new sidecar.

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
        # diagnostic-only and we don't index it per-slot — log
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

        Strategy (originally from now-shelved PRD §5.3): "the one BELOW
        the closed one if it exists, otherwise the next ABOVE". Walks down from `closed_slot - 1`
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
        if not self._first_capsule_seen:
            self._first_capsule_seen = True
            trace("first_capsule")
        cid = str(payload.get("id") or "")
        if cid == "cwd":
            new_cwd = str(payload.get("value") or "")
            if new_cwd != self._cwd:
                self._cwd = new_cwd
                self.cwdChanged.emit()
                if not self._first_displayed_root_traced:
                    self._first_displayed_root_traced = True
                    trace("first_cwd_capsule")
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

        Reads from the per-instance dict. Focus-switch emits
        `awaitingResponseChanged` so QML re-binds against the new
        slot's value without per-instance machinery on the QML side.
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

        Same focused-slot read as `awaitingResponse`; same
        re-emit-on-focus-switch contract (emits `permissionModeChanged`).
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

    # --- Pool-shape properties (QML indicator surface) ---

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

        Currently reads 0 or 1 (lazy spawn, pool is empty until the user
        triggers a spawn). Track-2 multi-instance will increment this
        as new sidecars spawn and decrement it on close.
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
        every focus switch — `Connections { onFocusedInstanceChanged }`
        is more reliable than `layoutChanged`-style auto-rebinding
        for this property.
        """
        return self._session_models.get(self._focused_instance)

    @Slot(int)
    def focus_instance(self, index: int) -> None:
        """Reassign focus to the given pool slot.

        Only slot 1 is populated today (lazy spawn); any other index is
        a no-op-with-log until Track-2 chord wirings extend the pool.
        Emits all three QML-facing signals so the pane re-binds the spinner, the pill,
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

        No-op + log on unknown slot — Track-2 chord wirings may dispatch
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

    @Slot(int)
    def seek_to_row(self, row: int) -> None:
        """Scroll the editor to put 1-indexed buffer line `row+1` at the
        cursor. Phase 3 of docs/minimap-prd.md — drives click-to-scroll
        and drag-scrubbing on the minimap.

        `row` is 0-indexed (Python convention; matches MinimapModel and
        the wire format from runtime/lua/orchestrator/minimap.lua). We
        translate to 1-indexed at the nvim boundary because `:goto`
        and `normal! NG` count from 1.

        gotcha #1 — pynvim isn't thread-safe; this slot runs on the
        GUI thread (QML signal) so we MUST marshal to the loop thread
        via `nvim.async_call`. Direct `nvim.command(...)` from QML's
        click handler would raise "request from non-main thread".

        No-op when the backend's nvim handle isn't ready yet (early
        boot, post-shutdown). The user's click is silently dropped
        rather than crashing — matches the pattern other QML-driven
        nvim commands use (e.g. agent-pane chord wirings).
        """
        if row < 0:
            row = 0
        target_line = row + 1  # 1-indexed for nvim's :goto

        def _do_goto() -> None:
            try:
                # `normal! NG` jumps the cursor to line N. The bang
                # suppresses user-defined remappings of G — important
                # because some users remap G to a different motion.
                self._backend._nvim.command(f"normal! {target_line}G")
            except Exception:  # noqa: BLE001
                log.exception("seek_to_row: nvim.command failed for row=%d", row)

        if self._backend._nvim is not None:
            self._backend._nvim.async_call(_do_goto)

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
        """Open the file-manager overlay at `initial_path` (or anchored root)."""
        if self._fm_visible:
            return
        self._fm_initial_path = initial_path or self._fm_default_path()
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

        Single funnel for every IDE surface that opens files — the
        sidebar file tree (`FileTreeView.onFileActivated`), the file
        manager overlay (Ctrl+E picker via `pick_in_nvim`), and the
        git status panel. Delegates to `NvimBackend.edit_file` so the
        request rides the `nvim_cmd` RPC path, not a keystroke — see
        that method's docstring for why mode-independence matters
        (terminal mode would otherwise type `:execute ...` into the
        running shell instead of running an ex-command).

        Also swaps the central surface to the editor first, so the user
        actually SEES the file. Activating a file from the file tree
        while the terminal pane is the active central surface (the user
        has Ctrl+Shift+E'd over to the shell) used to silently load the
        buffer in an unseen nvim — the swap eliminates that surprise.
        `swap_to_editor` is idempotent (no-op when editor is already
        the central surface), so the dominant case — file tree clicked
        while the editor is visible — emits no spurious
        `centralSurfaceChanged`. Swap-first / edit-after ordering means
        the editor is mounted + repainting before the nvim `:edit`
        async-marshal completes, so the file appears in an
        already-visible pane rather than popping in pre-loaded.

        `pick_in_nvim` wraps this with the overlay-dismiss step the
        picker needs; the sidebar caller wants the sidebar to stay
        visible, so it calls this directly.
        """
        if not path:
            return
        self.swap_to_editor()
        self._backend.edit_file(path)

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

    @Property(str, notify=displayedRootChanged)
    def displayedRootCompact(self) -> str:
        """`displayedRoot` rendered with `$HOME` collapsed to `~`.

        Pure view-layer transformation for the side-panel header (and
        any other "where am I right now" affordance). Kept Python-side
        — and not pushed into QML as inline JS — so the HOME-prefix
        logic has a single home, can be unit-tested, and stays in sync
        with however the future session model might want to format
        paths (e.g., showing the session's display name instead of the
        raw path once that exists).
        """
        root = self.displayedRoot
        if not root:
            return ""
        home = self._home  # cached in __init__; HOME never changes at runtime
        if home and (root == home or root.startswith(home + os.sep)):
            return "~" + root[len(home) :]
        return root

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

    @Property(list, notify=expandedPathsCacheChanged)
    def expandedPathsCache(self) -> list[str]:
        """Saved expanded-paths list for the current displayed root.

        QML binds the FM's `restoreExpandedPaths` to this property.
        Empty list means "no cache yet" — the FM treats that as a
        signal to use its `lazyExpand` cascade, identical to current
        behaviour for first-time mounts. The list is refreshed by
        `_sync_expanded_paths_cache` on every `displayedRootChanged`.
        """
        return self._expanded_paths_cache

    @Slot()
    def _sync_expanded_paths_cache(self) -> None:
        """Reload the cache for the new displayed root.

        Connected to `displayedRootChanged` (BEFORE `_sync_git_repo_root`
        in connect order, intentionally — see the connect site). The
        load is synchronous file I/O, typically <2ms even on cold disk:
        one open + one JSON parse + N stat calls (N = saved paths,
        capped by realistic UI use to a few hundred). If we ever see
        load latency become a launch concern, this slot is the right
        place to add a background-thread variant — emit
        `expandedPathsCacheChanged` from the GUI thread once the worker
        finishes. The disk file's atomic-write contract guarantees we
        never observe a partial-write here.
        """
        root = self.displayedRoot
        # Stash the root the cache was loaded for. `saveExpandedPaths`
        # writes back to this — keeps save paths correct even if the
        # user is rapidly switching projects.
        self._expanded_paths_cache_root = root
        self._expanded_paths_cache = load_expanded(root) if root else []
        self.expandedPathsCacheChanged.emit()

    @Slot(list)
    def saveExpandedPaths(self, paths: list[str]) -> None:
        """Persist the FM's current expanded-paths set.

        Wired from QML as
        `onExpandedStateChanged: controller.saveExpandedPaths(paths)`.
        Writes synchronously to `_expanded_paths_cache_root` (NOT the
        live displayedRoot — those can differ briefly during project
        switches; we want to attribute the save to the project the
        paths actually came from). Atomic via `os.replace`, so a
        crash mid-write never corrupts the existing cache.

        The QML `paths` arrives as a JavaScript Array; PySide6's
        list-typed Slot converts it to a Python list before dispatch.
        Non-string entries are filtered out inside `save_expanded`.
        """
        if not self._expanded_paths_cache_root:
            return
        save_expanded(self._expanded_paths_cache_root, paths)
        # Mirror the saved set into the in-memory cache so a
        # subsequent property read reflects what's on disk. Don't
        # emit `expandedPathsCacheChanged` — we'd ping-pong with the
        # FM's binding update.
        self._expanded_paths_cache = sorted(set(p for p in paths if isinstance(p, str)))

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
    def _sync_nvim_cwd(self) -> None:
        """Push the displayed root into nvim as its `:pwd`.

        Same `displayedRootChanged` source as `_sync_git_repo_root`,
        same anchored-pins-the-target semantic: anchored → pwd stays on
        the anchored root; unanchored → pwd follows the terminal's cwd
        via the `on_shell_cwd` routing (Timer-polled `KSession.currentDir`).
        Empty / missing path is a no-op
        (covers the initialization edge case where `displayedRoot` is
        briefly the empty string before the first cwd capsule lands).

        The underlying `NvimBackend.set_current_dir` is itself a no-op
        when nvim hasn't spawned yet, so this is also safe if any
        startup ordering shuffles around — `_sync_nvim_cwd` will simply
        miss the very first emission and the next one (or the explicit
        cwd capsule push that always follows VimEnter) will land.
        """
        root = self.displayedRoot
        if not root:
            return
        self._backend.set_current_dir(root)

    @Slot()
    def focus_tree(self) -> None:
        """Ask QML to move active focus into the FileTreeView.

        Emits `focusTreeRequested` rather than reaching into QML
        directly — keeps the Python side stateless about QML focus
        ownership. Main.qml's Connections block calls
        `fileTreeView.forceActiveFocus()` on receipt.
        """
        self.focusTreeRequested.emit()

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
        Internal primitive called by `toggle_editor_terminal`; the
        user-facing chord is `Ctrl+Shift+E` (the earlier `Ctrl+Shift+T`
        was retired when the toggle landed).
        """
        if self._central_surface == "terminal":
            return
        self._central_surface = "terminal"
        self.centralSurfaceChanged.emit()

    @Slot()
    def swap_to_editor(self) -> None:
        """Make the editor pane the visible central surface.

        Idempotent: if editor is already visible, no signal fires.
        Retained as a primitive used by `toggle_editor_terminal` and
        callable from internal slots/tests; the user-facing chord
        (`Ctrl+Shift+E`) now goes through the toggle.
        """
        if self._central_surface == "editor":
            return
        self._central_surface = "editor"
        self.centralSurfaceChanged.emit()

    @Slot()
    def toggle_editor_terminal(self) -> None:
        """Flip the central surface between editor and terminal.

        Bound to `Ctrl+Shift+E` in Main.qml. Pressing from editor →
        terminal; pressing from any other surface (terminal today, a
        hypothetical tertiary central surface tomorrow) → editor. The
        asymmetry is intentional: 'E' names the editor, so the chord
        always lands you on the editor unless you were already there.

        Always emits exactly one `centralSurfaceChanged`; never a noop
        from the user's perspective. Composes the existing
        `swap_to_editor` / `swap_to_terminal` primitives rather than
        mutating state directly so any future invariants (logging,
        focus side-effects, session bookkeeping) added to those slots
        are honored by the toggle as well.
        """
        if self._central_surface == "editor":
            self.swap_to_terminal()
        else:
            self.swap_to_editor()

    @Slot()
    def focus_terminal(self) -> None:
        """Ask QML to move active focus into the TerminalView.

        Symmetric counterpart of `focus_editor` / `focus_tree`. Emits
        the signal rather than touching QML focus directly — Main.qml's
        Connections block calls `terminalView.forceActiveFocus()` on
        receipt.
        """
        self.focusTerminalRequested.emit()

    @Slot(str)
    def on_shell_cwd(self, path: str) -> None:
        """Route the shell terminal's current directory into the same `cwd`
        capsule machinery nvim's `:cd` and the old OSC 7 path used.

        Post-qmltermwidget-migration replacement for the old OSC 7 path
        (terminal reader thread → `osc7_received` → `_route_capsule`): the
        shell pane is now a QMLTermSession whose `currentDir` property the
        Konsole engine tracks natively (it reads the foreground process cwd
        via /proc — no shell-side OSC 7 hook needed). A Timer in Main.qml
        polls `session.currentDir` and calls this slot only when the value
        changes, so the per-keystroke OSC 7 stream is replaced by a coalesced
        change signal. Routing through `_route_capsule({id:"cwd", value})`
        keeps every downstream consumer (anchor state machine, file tree,
        git controller) on the single existing path — no parallel routing.

        KSession's `currentDir` can carry a trailing slash and is empty until
        the shell has started; normalise to match `_parse_osc7`'s output
        (trailing slash stripped except root) and drop empties — the
        `new_cwd != self._cwd` guard in `_route_capsule` would otherwise treat
        an empty string as a real change.
        """
        if not path:
            return
        if len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/") or "/"
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

    def _fm_default_path(self) -> str:
        """Default initial path for the FM overlay when none is provided.

        Delegates to `displayedRoot` — the same anchored-then-cwd fallback
        chain the file-tree and git panes use — so the FM opens uniformly
        from any pane (Ctrl+E is an IDE-wide ApplicationShortcut, with
        no "current buffer" notion in the agent / terminal panes). The
        anchored-root path is kept fresh by the unified capsule routing
        (nvim's :cd and the shell's `currentDir` via `on_shell_cwd` both
        flow through `_route_capsule` with the synthetic `cwd` id).

        Falls back to $HOME when neither the anchor nor the cached cwd
        resolves to a real path — defense-in-depth against an empty
        capsule payload at startup before the first DirChanged.
        """
        return self.displayedRoot or os.path.expanduser("~")

    @Slot(dict)
    def _on_fm_event(self, payload: dict) -> None:
        """Route Lua-emitted fm rpcnotify events.

        Payload shapes:
          { op: "show"|"hide"|"toggle", initialPath?: string }

        The "debug" op (previously emitted by the now-removed
        install_fm_keymap keymap self-heal) is no longer emitted by
        any Lua code. If a future :SymFm user-command needs diagnostics,
        add the branch back at that point.
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
          changes between request and response in a multi-instance
          scenario.

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

        Crashes, SIGTERM, or auth failures all reach the GUI through
        `closed` rather than a `result` envelope. Without this slot
        the spinner would stay lit indefinitely after a crash.

        Resets the slot's permission mode to `default` so the next
        session starts with the canonical pill rather than briefly
        inheriting the stale mode from the dead subprocess. Drops any `_pending_permissions` entries the
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
        """Route an agent lifecycle event.

        Historically driven by Lua rpcnotify ("agent" channel from
        `runtime/init.lua`). That coupling was stripped when
        orchestrator.nvim took back ownership of `<leader>aN` /
        `<C-1..5>` / `<C-S-q>`; this dispatcher is now callable directly
        from tests and from future Track-2 QML chord wirings that will
        re-introduce the spawn / focus / close surface on non-colliding
        chords. The op semantics below are unchanged.

        Payload shape: `{op: "show"|"hide"|"toggle"|"focus"|"close"|"debug",
        ...}`. Unknown ops log at DEBUG and no-op — additive protocol
        evolution doesn't crash the controller.

        Dispatch table (originally Phase B / PRD §5.1):

        - `op="show"`, `action="new"` → spawn into next free slot (1..5)
          and focus it. Pool full = warn + focus the highest-numbered
          existing slot (slot 5 in the saturated case).
        - `op="show"` without `action` → just open the pane on the
          currently-focused instance. No spawn.
        - `op="focus"`, `index=N` → switch focus to slot N. No-op if
          slot N is empty (focus does not spawn; avoids accidental spawn
          from a held keybind).
        - `op="close"` → close the focused instance. With `index=N`,
          close that specific slot. After close: refocus to the
          next-lowest occupied slot (walk-down-then-up rule), or hide
          the pane if the pool emptied.
        - `op="hide"` / `op="toggle"` → pane visibility only, no
          per-instance impact.
        - `op="debug"` → diagnostic trail (historically from Lua
          keymap-install attempts; retained for forward compatibility).
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
        """`op=show` dispatch — spawn-into-next-free path.

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
        """`op=focus` dispatch.

        Focusing a non-existent slot is a no-op-with-log (decided NOT
        to auto-spawn — matches orchestrator.nvim and avoids accidental
        spawn from a held keybind).
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

        The editor-side `<C-S-q>` hijack that previously dispatched via
        `agent_event` → `_handle_agent_close` has been stripped (orchestrator.nvim
        owns the chord again inside embedded nvim). The QML composer /
        pane chrome can't reach nvim's keymap system anyway — focus sits
        on a TextField — so this in-pane bridge is the canonical path.

        Routes through `_handle_agent_close({})` rather than calling
        `_close_instance` directly — `_handle_agent_close` carries the
        refocus + empty-pool + signal-emit semantics that QML's chrome
        relies on (chip strip empties, spinner clears, pill resets).
        """
        self._handle_agent_close({})

    def _handle_agent_close(self, payload: dict) -> None:
        """`op=close` dispatch — called from `close_focused_instance` or future explicit-index variants.

        Missing index = focused instance. Refocus selection uses
        `_next_focus_after_close` (walk down then up). Empty pool
        after close hides the pane and snaps `_focused_instance` back
        to 1 so the next spawn lands cleanly at slot 1.
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
        the focused slot (in a multi-instance scenario, the user could
        focus-switch between request and response). The lookup is authoritative;
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
        trace("start_begin")
        # The editor NeoVim is spawned by the QMLTermSession inside Main.qml
        # (Konsole KSession), using the `editorProgram`/`editorArgs` context
        # properties built in `_build_engine` — which runs BEFORE start(), so
        # nvim is already launching by the time we get here. It opens a
        # `--listen` control socket; `_backend` (the RPC-only client) attaches
        # to that socket on its OWN worker thread with a retry budget, so the
        # GUI thread never blocks even though the socket may not exist for the
        # first few millis. nvim does NOT render through Python anymore — the
        # terminal widget draws its grid; `_backend` only carries the chrome
        # rpcnotify relays + control RPCs (input / edit_file / set_current_dir).
        self._backend.start()
        trace("backend_started")
        # Seed the GitController with the launch cwd by firing
        # `displayedRootChanged` once at startup. Without this, the very
        # first nvim cwd capsule arrives with a value equal to `_cwd`
        # (because `_cwd` is initialised from `os.getcwd()` at
        # AppController.__init__, and nvim's `BufEnter` cwd capsule
        # reports the same path), so `_route_capsule` hits the
        # `new_cwd != self._cwd` no-op gate and never emits — leaving
        # `_sync_git_repo_root` unwired. The GitController worker would
        # then sleep forever, the Active Changes panel stays hidden
        # (`gitStatusList.count == 0`), and the FileTreeView's
        # `ignoredPathSet` binding never sees a non-null value (the
        # option 1 gitignore short-circuit silently becomes a no-op).
        # Bug observed on 2026-05-22 — both panels stayed empty when
        # launching the IDE in its own project dir, despite the same
        # logic working when nvim later changed buffer to a file
        # outside the initial cwd. One synthetic emit at the end of
        # `start()` is enough; downstream slots (`_sync_git_repo_root`,
        # `_sync_nvim_cwd`, QML bindings) all run with the correct
        # initial value.
        self.displayedRootChanged.emit()
        # Phase 2.5 terminal pane — the shell is now spawned by the
        # QMLTermSession in Main.qml (Konsole KSession), the same way the
        # editor nvim is. No Python TerminalBackend pre-warm here anymore;
        # the widget starts its shell in Component.onCompleted, and its
        # native `currentDir` drives the file-tree cwd sync via
        # `controller.on_shell_cwd` (polled by a Timer in QML).
        trace("terminal_started")
        # Pool stays empty unless an env-var path explicitly opts in.
        # Track-2 chord wirings will lazily spawn slot 1 via
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
        #     smoke tests; equivalent to the user spawning a slot
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
        # Ask nvim to quit GRACEFULLY over the RPC socket (`_backend.stop()`
        # sends `qa!` + closes the client) so it writes shada/swap cleanly.
        # The terminal widgets (editor nvim + shell) are owned by their
        # QMLTermSessions (KSession); their child processes are reaped when the
        # QML engine tears down on app quit, so there is no Python-side killpg
        # backstop to run here anymore.
        self._backend.stop()
        # Clean up the temporary directory that held the nvim socket.
        # The socket file itself is gone when nvim exits; the directory
        # it lived in is ours to clean up (mkdtemp creates it in /tmp).
        shutil.rmtree(os.path.dirname(self._nvim_socket), ignore_errors=True)

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
    def completion(self) -> CompletionModel:
        return self._completion

    @property
    def whichkey_state(self) -> WhichKeyState:
        return self._whichkey_state

    @property
    def whichkey_model(self) -> WhichKeyModel:
        return self._whichkey_model

    @property
    def minimap_model(self) -> MinimapModel:
        return self._minimap_model

    @property
    def nvimSocketPath(self) -> str:
        """Path to the editor nvim's `--listen` control socket.

        Read by `_build_engine` to build the `editorArgs` context property the
        QMLTermSession launches nvim with, and by `NvimBackend` to attach the
        RPC-only chrome relay. Single source of truth for the socket path.
        """
        return self._nvim_socket


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
    registered modules (the terminal panes are no longer @QmlElement
    types — they're QMLTermWidget items from the imported fork).
    """
    # Keep these references — they are the second layer of protection for
    # the noqa: F401 side-effect imports above. Removing any name here
    # means a linter can silently drop the import and break @QmlElement
    # registration. See CLAUDE.md gotcha #7 and project-standards §2 P1.
    _ = MinimapView
    _ = CmdlineState
    _ = CompletionModel
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

    # Dev-mode override: prepend a source-tree path to the QML import resolver
    # so edits to the Symmetria FM's QML files (e.g. FileTreeView.qml) take
    # effect without reinstalling /usr/lib/qt6/qml/Symmetria/. QML2_IMPORT_PATH
    # and QML_IMPORT_PATH env vars are not reliably honored once the installed
    # plugin's qmldir has been cached, but `engine.addImportPath` is documented
    # to take precedence. Production deployments leave the env var unset and
    # behavior is unchanged.
    #
    # The Models C++ plugin is NOT shipped from the source tree (it builds to
    # `plugin/build/`), so the resolver falls through to the installed path
    # for Symmetria.FileManager.Models — only the UI QML overlays.
    _fm_dev_path = os.environ.get("SYMMETRIA_IDE_FM_QML_PATH", "").strip()
    if _fm_dev_path:
        # Prepend rather than append: addImportPath() in Qt 6.x adds to the
        # END of the path list, but the resolver searches in list order, so
        # an appended dev path loses to the installed /usr/lib/qt6/qml entry
        # for any module name that already resolves there. Replace the full
        # path list with our prefix to guarantee precedence.
        new_paths = [_fm_dev_path, *engine.importPathList()]
        engine.setImportPathList(new_paths)
        log.info("FM QML dev override active: %s", _fm_dev_path)
        log.info("QML import path list: %s", engine.importPathList())

    # The terminal/editor surface is our fork of qmltermwidget (Konsole's VT
    # engine + renderer as a QML item). We add its build-output dir to the
    # import path so `import QMLTermWidget 2.0` resolves to OUR build (with the
    # background-transparency fix + Symmetria colorscheme) rather than the stock
    # Arch package. `addImportPath` PREPENDS (verified: fork lands at index 0,
    # /usr/lib/qt6/qml at index 4), so the fork reliably wins even though the
    # stock `qmltermwidget` package is installed at the standard path.
    # See /home/jc/projects/symmetria-qmltermwidget/MODIFICATIONS.md.
    #
    # WORKAROUND: the default points at a machine-specific absolute build dir
    # because the fork is not yet packaged. Env-overridable via
    # SYMMETRIA_IDE_QMLTERMWIDGET_PATH so it works on other checkouts. Remove
    # the hardcoded default once a PKGBUILD installs the fork to
    # /usr/lib/qt6/qml/QMLTermWidget/ (provides/conflicts the stock package) —
    # then the import resolves with no override and the var ships unset.
    _qtw_path = os.environ.get(
        "SYMMETRIA_IDE_QMLTERMWIDGET_PATH",
        "/home/jc/projects/symmetria-qmltermwidget",
    ).strip()
    if _qtw_path:
        engine.addImportPath(_qtw_path)
        log.info("qmltermwidget fork import path: %s", _qtw_path)

    ctx = engine.rootContext()

    # Make backend + capsules available to QML as a single `controller`
    # context property — keeps the QML surface small.
    ctx.setContextProperty("controller", controller)
    ctx.setContextProperty("capsuleModel", controller.capsules)
    ctx.setContextProperty("statusState", controller.status)
    ctx.setContextProperty("cmdlineState", controller.cmdline)
    ctx.setContextProperty("completionModel", controller.completion)
    ctx.setContextProperty("whichKeyState", controller.whichkey_state)
    ctx.setContextProperty("whichKeyModel", controller.whichkey_model)
    # Editor minimap model — Phase 1 of docs/minimap-prd.md. Main.qml
    # binds `MinimapView.bufferRowCount: minimapModel.lineCount`.
    ctx.setContextProperty("minimapModel", controller.minimap_model)
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
    #   2. The pool is empty at IDE launch (lazy spawn via Track-2
    #      chord or env-var). Dereferencing `_session_hosts[_focused_instance]`
    #      here would KeyError before the user has spawned anything.
    # Nothing in QML still binds against the old names — they were
    # carry-over from the original single-instance topology.

    # Resolve the editor font ONCE in Python so every QML overlay binds
    # to the same family the editor (`editor_font.default_font`) chose.
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
    _resolved_font = default_font()
    _primary_family = (_resolved_font.families() or [_resolved_font.family()])[0]
    ctx.setContextProperty("editorFontFamily", _primary_family)
    # Editor font point size — single source of truth in editor_font.py.
    # The QMLTermWidget editor + shell panes bind `font.pointSize` to this.
    ctx.setContextProperty("editorFontPointSize", float(DEFAULT_FONT_POINT_SIZE))

    # Editor nvim launch spec, consumed by the QMLTermSession inside Main.qml.
    # nvim is now spawned by the terminal widget (Konsole KSession), NOT by a
    # Python TerminalBackend — but it still opens a `--listen` control socket
    # that the RPC-only `NvimBackend` attaches to for the chrome relays
    # (cmdline / which-key / capsules / completions / minimap). The socket path
    # + runtime injection mirror exactly what the old `editor_argv` in
    # `start()` used; only the spawner moved from Python to QML.
    #   - editorProgram: argv[0]
    #   - editorArgs:    argv[1:] (QMLTermSession.shellProgramArgs excludes the
    #                    program name)
    # nvim's initial cwd is set in QML via `initialWorkingDirectory:
    # controller.displayedRoot` (a live binding), not a context property.
    ctx.setContextProperty("editorProgram", "nvim")
    ctx.setContextProperty(
        "editorArgs",
        [
            "-n",
            "--listen",
            controller.nvimSocketPath,
            "--cmd",
            f"set rtp^={_RUNTIME_DIR}",
            "--cmd",
            f"luafile {_RUNTIME_DIR / 'init.lua'}",
        ],
    )

    # Shell pane launch spec. The shell runs as a plain interactive shell so it
    # reads the user's real ~/.zshrc / ~/.bashrc — the old OSC 7 rcfile/ZDOTDIR
    # injection (runtime/symmetria-shell/) is gone; the QMLTermSession's native
    # `currentDir` (Konsole reads the foreground process cwd via /proc) drives
    # the file-tree cwd sync now, polled by a Timer in Main.qml that calls
    # `controller.on_shell_cwd`. No args → interactive non-login, matching the
    # prior TerminalBackend behavior.
    # Named shellExec/shellExecArgs (not shellProgram/shellProgramArgs) to avoid
    # colliding with QMLTermSession's OWN property names — an unqualified RHS
    # `shellProgram: shellProgram` would self-bind to the session's property
    # instead of resolving to the context property.
    ctx.setContextProperty("shellExec", os.environ.get("SHELL") or "/bin/bash")
    ctx.setContextProperty("shellExecArgs", [])
    trace("engine_ctx_ready")

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
    trace("run_entered")
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
    trace("qgui_created")

    _register_qml_types()
    trace("qml_registered")
    controller = AppController()
    trace("controller_created")
    engine = _build_engine(controller)
    if engine is None:
        return 1
    trace("engine_loaded")

    controller.start()
    trace("start_done")
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

    trace("exec_entered")
    return app.exec()
