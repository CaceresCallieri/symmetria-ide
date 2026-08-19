"""Shared pytest fixtures and helpers for the Symmetria IDE test suite.

Centralises infrastructure that would otherwise be duplicated across
structural (source-inspection) test modules.  Any test file that does
structural source checks or needs a bare QCoreApplication should import
from here rather than redefining these helpers locally.
"""

from __future__ import annotations

import inspect
import sys
import time
from typing import ClassVar

import pytest
from PySide6.QtCore import QCoreApplication, QObject, Signal


@pytest.fixture(scope="session", autouse=True)
def qt_app():
    """Create a QCoreApplication for the session (required by Qt subsystems).

    QColor, QRectF, and QFontDatabase all abort without a QApplication
    present.  ``scope="session"`` keeps one instance alive for the whole
    run; ``autouse=True`` means tests that don't explicitly request it
    still benefit from it being initialised.
    """
    app = QCoreApplication.instance() or QCoreApplication(sys.argv)
    yield app


@pytest.fixture(autouse=True)
def _isolate_xdg_state(monkeypatch, tmp_path_factory):
    """Redirect ``XDG_STATE_HOME`` to a throwaway dir for every test.

    ``AppController.shutdown()`` now persists a session manifest (``session_store``)
    and ``tree_state_cache`` writes its expansion cache — both under
    ``XDG_STATE_HOME``. Without this, any test that constructs + tears down a
    controller (or touches those caches) would pollute the developer's real
    ``~/.local/state/symmetria-ide``. Function-scoped + autouse so it covers
    every test; tests that need a specific path still override it via their own
    ``monkeypatch.setenv`` (last writer wins)."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path_factory.mktemp("xdg_state")))


@pytest.fixture(autouse=True)
def _isolate_xdg_config(monkeypatch, tmp_path_factory):
    """Redirect ``XDG_CONFIG_HOME`` to a throwaway dir for every test.

    ``server_registry.load_servers`` reads
    ``$XDG_CONFIG_HOME/symmetria-ide/servers.json`` and every
    ``AppController()`` construction probes VPS pairing through it — with the
    developer's real registry in scope the suite would fire live ssh probes
    on each construction. Same override semantics as the fixtures below.

    Also DELETES ``SYMMETRIA_UI_SCHEME``. That var points ``ui_scheme`` at an
    explicit palette file and OUTRANKS ``XDG_CONFIG_HOME``, so redirecting the
    XDG root alone would not isolate it — a developer previewing a palette in
    one worktree would silently feed it to the whole suite."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path_factory.mktemp("xdg_config")))
    monkeypatch.delenv("SYMMETRIA_UI_SCHEME", raising=False)


@pytest.fixture(autouse=True)
def _isolate_xdg_runtime(monkeypatch, tmp_path_factory):
    """Redirect ``XDG_RUNTIME_DIR`` to a throwaway dir for every test.

    ``agent_registry`` publishes per-IDE routing entries under
    ``$XDG_RUNTIME_DIR/symmetria-ide/registry/`` and the reporter hook
    re-resolves its target socket from them (the CC 2.1.x daemon caveat fix).
    Without this isolation, a LIVE IDE instance whose registry entry claims
    this repo's path hijacks the reporter tests' events — the reporter routes
    to the real IDE's socket instead of the test's tmp socket and the test
    times out pumping events. Same override semantics as ``_isolate_xdg_state``:
    tests that need a specific path re-setenv locally (last writer wins)."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path_factory.mktemp("xdg_runtime")))


@pytest.fixture(autouse=True)
def _isolate_usage_poll(monkeypatch):
    """Stop the subscription-usage poller from reaching the real accounts.

    ``AppController.start()`` starts the poller, and several tests call it. Left
    alone, each of those fires a real HTTPS request against the developer's
    Anthropic account AND spawns a real ``codex app-server`` subprocess — from a
    suite that is supposed to touch nothing live. It also makes those tests fail
    offline for reasons unrelated to what they assert.

    Set to ``"0"`` rather than deleted, because ``"0"`` IS the off switch here
    (unlike the tmux flag, where presence is what enables). ``test_usage_poller``
    deletes it in the one test that asserts the default-on branch — the branch
    this fixture otherwise makes unreachable."""
    monkeypatch.setenv("SYMMETRIA_IDE_USAGE_POLL", "0")


@pytest.fixture(scope="session")
def _claude_projects_dir(tmp_path_factory):
    """One empty transcript root for the whole run.

    Session-scoped (unlike the `_isolate_xdg_*` dirs) because nothing ever
    WRITES here — the thread index only reads — so per-test directories would
    be two thousand empty dirs bought for nothing. Same precedent as
    ``_chrome_bin_dir`` / ``_tmux_sock_dir``."""
    return tmp_path_factory.mktemp("claude_projects")


@pytest.fixture(autouse=True)
def _isolate_thread_index(monkeypatch, _claude_projects_dir):
    """Keep the thread indexer away from the developer's real conversations.

    Two separate reaches, so two separate vars. ``AppController.start()``
    requests an index, and the Claude reader's default root is the real
    ``~/.claude/projects`` — 315 transcripts, tens of megabytes, read from a
    suite that is supposed to touch nothing live, and re-read on every
    controller a test constructs.

    ``SYMMETRIA_IDE_THREAD_INDEX`` is set to ``"0"`` (the value IS the off
    switch, like ``SYMMETRIA_IDE_USAGE_POLL``) and gates the REQUEST, so a test
    driving ``AgentThreadIndexer`` directly still gets a live worker.
    ``SYMMETRIA_IDE_CLAUDE_PROJECTS_DIR`` is the containment underneath it: it
    redirects the reader's default root, so even a code path that reaches a
    reader without going through the request gate finds an empty directory.
    ``HOME`` itself is deliberately NOT redirected — Qt resolves fonts and
    settings through it, and moving it would change far more than this."""
    monkeypatch.setenv("SYMMETRIA_IDE_THREAD_INDEX", "0")
    monkeypatch.setenv("SYMMETRIA_IDE_CLAUDE_PROJECTS_DIR", str(_claude_projects_dir))


@pytest.fixture(autouse=True)
def _isolate_xdg_data(monkeypatch, tmp_path_factory):
    """Redirect ``XDG_DATA_HOME`` to a throwaway dir for every test.

    ``chrome_host`` keeps the agentic browser's Chrome profiles under
    ``$XDG_DATA_HOME/symmetria-ide/browser/``, including the ``_template``
    profile the user logs their real dashboards into. A test that seeds or
    opens a profile must never touch those: Chrome is a singleton per
    ``--user-data-dir``, so writing into a profile a LIVE IDE currently has
    open is not merely untidy, it reaches into a running browser's state.
    Same override semantics as the sibling ``_isolate_xdg_*`` fixtures."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path_factory.mktemp("xdg_data")))


@pytest.fixture(autouse=True)
def _isolate_browser_host(monkeypatch, _chrome_bin_dir):
    """Make it impossible for a test to spawn Chrome or touch the compositor.

    The live resource one function call away from any test that reaches
    ``AppController.open_browser`` is **the developer's Chrome**:
    ``chrome_executable()`` finds the real binary on PATH, and Chrome is a
    singleton per ``--user-data-dir`` — a spawn inside the suite would either
    open windows on the developer's screen or hand the request to a browser a
    running IDE already owns.

    Neutralised at the environment level rather than by patching call sites,
    per ``.claude/rules/test_env_isolation.md``: monkeypatched functions only
    cover the test window, while this path can be reached from teardown and
    deferred callbacks too. A test that wants a real binary opts in with its
    own ``setenv`` (last writer wins).

    (This fixture also used to neutralise ``HYPRLAND_INSTANCE_SIGNATURE``,
    because the retired pinned-window backend installed GLOBAL Hyprland window
    rules that would have outlived the run. The nested compositor needs no
    window rules, nothing under ``src/`` reads that variable any more, and the
    module that did is deleted.)"""
    monkeypatch.setenv("SYMMETRIA_IDE_CHROME_BIN", str(_chrome_bin_dir / "no-chrome"))


@pytest.fixture(scope="session")
def _chrome_bin_dir(tmp_path_factory):
    """One throwaway dir for the whole run holding the nonexistent Chrome path.

    Session-scoped for the same reason as ``_tmux_sock_dir``: nothing is ever
    created there, and a per-test ``mktemp`` would leave ~1500 empty dirs
    behind for a value that is only ever read."""
    return tmp_path_factory.mktemp("chrome_bin")


@pytest.fixture(scope="session")
def _opencode_bin_dir(tmp_path_factory):
    """One throwaway directory holding the nonexistent OpenCode binary path."""
    return tmp_path_factory.mktemp("opencode_bin")


@pytest.fixture(autouse=True)
def _isolate_opencode_history(monkeypatch, _opencode_bin_dir):
    """Keep history discovery away from the developer's live OpenCode store.

    ``OpenCodeThreadReader`` spawns the configured executable and asks it for
    sessions.  Contain that reach at the environment boundary so a test that
    starts an index scan without mocking ``subprocess.run`` still fails closed.
    Tests of the production default opt in by deleting this variable locally.
    """
    monkeypatch.setenv(
        "SYMMETRIA_IDE_OPENCODE_BIN", str(_opencode_bin_dir / "no-opencode")
    )


@pytest.fixture(scope="session")
def _tmux_sock_dir(tmp_path_factory):
    """One throwaway directory for the whole run to hold the fake tmux socket
    path. Session-scoped because nothing ever creates the socket — a per-test
    ``mktemp`` would leave ~1500 empty directories behind for a value that is
    only ever read."""
    return tmp_path_factory.mktemp("tmux_sock")


@pytest.fixture(autouse=True)
def _isolate_agent_tmux(monkeypatch, _tmux_sock_dir):
    """Neutralise the tmux substrate's environment for every test.

    Without this the suite KILLS THE DEVELOPER'S OWN AGENT SESSION. Three
    behaviours compound, each harmless alone (verified 2026-07-18 by shimming
    ``tmux`` and capturing eight real ``kill-session`` invocations aimed at the
    session the test run was itself living in):

    - the IDE launchers export ``SYMMETRIA_IDE_AGENT_TMUX=1``, and an agent pane
      INHERITS it — so a suite run from inside the IDE (the normal workflow)
      silently enables the substrate in tests that never opted in;
    - ``_agent_tmux_socket()`` reads the env at CALL time and defaults to the
      real ``~/.vigilia/tmux.sock``, so with the socket unset those tests talk
      to the live tmux server;
    - the session NAME is ``tmux_session_name(displayedRoot, slot)`` and a test
      controller's root is the test process's cwd — this repo — so the suite
      generates precisely the name a real agent working in this repo already
      holds (``symmetria-ide-<hash>-1``), and ``kill-session`` lands on it.

    Clearing the flag also fixes the visible half of the same leak: argv
    assertions expecting the bare ``env`` wrapper got a ``tmux`` wrap and failed
    for anyone running the suite inside the IDE. The throwaway socket stays as
    defence in depth — a kill that escapes anyway can then only reach an empty
    tmux server that nothing is attached to.

    This is containment, not a substitute for mocking: tests asserting the
    ``tmux`` argv should still monkeypatch ``symmetria_ide.app.subprocess.run``
    so they never shell out at all. Same override semantics as
    ``_isolate_xdg_state`` (tests opt in locally; last writer wins)."""
    monkeypatch.delenv("SYMMETRIA_IDE_AGENT_TMUX", raising=False)
    monkeypatch.setenv(
        "SYMMETRIA_IDE_TMUX_SOCKET", str(_tmux_sock_dir / "agent-tmux.sock")
    )


# AppController's sub-controllers that own a worker thread. Each thread's
# target is a BOUND METHOD, so a running thread holds a reference to its
# controller — which is why an un-stopped one is not merely idle but keeps the
# whole AppController graph alive. See `_release_app_controller_workers`.
_WORKER_OWNING_SUBCONTROLLERS = (
    "_git_controller",
    "_git_log_controller",
    "_git_branch_controller",
    "_git_ops_controller",
    "_gh_pr_controller",
    # Owns a ThreadPoolExecutor whose task is a bound method, so a submitted
    # poll pins its AppController exactly like the git controllers' threads do.
    "_usage_poller",
    # The thread-history index worker — same profile as the git workers: a
    # daemon thread whose target is a bound method, so leaving it running pins
    # the whole controller graph and re-opens the inotify leak above.
    "_agent_thread_indexer",
)


@pytest.fixture(autouse=True)
def _release_app_controller_workers(monkeypatch):
    """Stop every AppController a test built, so the suite stops accumulating.

    THE LEAK. Seven test modules construct an `AppController` and never call
    `shutdown()`. Each one starts five worker threads, and because a thread's
    target is a bound method, each running thread pins its controller — so
    nothing is ever garbage-collected, including the `QFileSystemWatcher` each
    `GitController` owns. Measured mid-suite before this fixture existed: **230
    live AppControllers, 1159 threads, and 224 inotify instances held by one
    pytest process**, against a system budget (`fs.inotify.max_user_instances`)
    of 1024 that is SHARED with the developer's running desktop — their IDE,
    file manager and shell hold hundreds more.

    That is the mechanism behind the suite's "intermittent" deaths: it is not
    random, it is a resource ramp that crosses a shared ceiling at a point
    determined by what else is running. It surfaces as a hang, a 139 or a 134
    depending on which allocation loses, and it never reproduces when a single
    file is run in isolation. Observed live as a hang inside
    `QFileSystemWatcher()` construction with the main thread on a futex.

    Stopping the threads is what un-pins the controller, which is what lets the
    watcher — and its inotify fd — be collected. A/B over the same 194 tests
    (`test_app_controller_term_agents` + `_central_surface`): **779 threads
    still alive at session end without this fixture, 4 with it.**

    ⚠ It takes `monkeypatch` on purpose, and not because it patches anything.
    Autouse fixtures set up FIRST and therefore finalise LAST, which would run
    this after a test's monkeypatched `subprocess.run` had been restored and let
    teardown shell out for real. Requesting the same function-scoped
    `monkeypatch` instance the tests use forces it to set up after — and so
    tear down before — the patches come off.

    Deliberately NOT calling `AppController.shutdown()`: that also saves the
    session, quits nvim over RPC and tears down Chrome, none of which the
    modules that skip it have prepared for. This releases the OS resources and
    nothing else.
    """
    from symmetria_ide import app as app_module

    created: list = []
    original_init = app_module.AppController.__init__

    def tracking_init(self, *args, **kwargs):
        # Tracked BEFORE construction, not after: a controller whose __init__
        # raises partway has usually already started some of its worker
        # threads, and those are exactly the ones that pin it. Appending
        # afterwards would leave the failed-construction case — the one the
        # teardown below claims to cover — untracked. `self` is a valid object
        # here, and the teardown's `getattr(sub, "stop", None)` already
        # tolerates the half-built attributes that follow from this.
        created.append(self)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(app_module.AppController, "__init__", tracking_init)
    yield
    for controller in created:
        for name in _WORKER_OWNING_SUBCONTROLLERS:
            sub = getattr(controller, name, None)
            stop = getattr(sub, "stop", None)
            if stop is None:
                continue
            try:
                stop()
            except Exception as exc:  # noqa: BLE001 — see below
                # Blind on purpose: this runs after EVERY test, including ones
                # that failed halfway through construction, so a sub-controller
                # can be in any state. Raising here would replace the test's own
                # failure with a teardown error and hide what actually broke.
                # Reported rather than swallowed, so a controller that cannot be
                # stopped is still visible as the leak it will become.
                print(f"[conftest] {name}.stop() failed during teardown: {exc}")


def wait_until(predicate, timeout: float = 3.0, message: str = "") -> None:
    """Sleep-poll until ``predicate()`` is true. Deliberately does NOT pump Qt.

    For assertions that wait on a WORKER THREAD to run — a socket handler
    accepting a connection, a client thread writing a reply. The thread makes
    progress on its own; this only yields the GIL to let it.

    ⚠ It must never grow a ``QCoreApplication.processEvents()`` call, however
    convenient that looks when a queued signal will not arrive. Pumping drains
    the GLOBAL queue of the session-scoped app, which by mid-suite holds
    ``deleteLater`` deletions posted by earlier QML-heavy modules; running
    those here trips the Python-3.14 cyclic-GC-vs-Qt SEGV (CLAUDE.md gotcha
    #10). It surfaces as a hang, a 139, or a 134 in whichever file did the
    pumping — a file that passes in isolation every time. Measured at 4 failures
    in 9 full-suite runs before the last two pumps were removed, and the rate
    rises with machine load, so a clean run on an idle box proves nothing.

    The way to test a queued delivery instead: connect the spy with an explicit
    ``Qt.ConnectionType.DirectConnection`` so the payload is captured
    synchronously, and hand-deliver the slot. See the memo at
    `.claude/memory/reference/qt-pyside/processevents_shared_app_segv.md`.
    """
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert predicate(), message or "condition not reached within timeout"


def braced_block(text: str, marker: str) -> str:
    """The ``{ ... }`` block introduced by ``marker``, by brace counting.

    For the structural tests that assert against C++/QML source. Fixed-width
    slices were used for this before and are a trap: grow the block past the
    magic length and the assertions silently move out of the inspected window,
    so the failure reads as a regression in the code rather than in the test.

    Lives here because two sibling test modules had grown character-identical
    copies, comment and all — the standard setup for one of them drifting.
    """
    start = text.index(marker)
    open_at = text.index("{", start)
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise AssertionError(f"unbalanced braces after {marker!r}")


def construction_source(cls) -> str:
    """Return ``__init__`` source concatenated with every ``_init_*`` helper.

    NvimView's constructor is split across ``_init_buffers``,
    ``_init_springs``, and ``_init_signals`` helpers so that related
    initialisations are grouped together.  Tests verifying "a call/
    allocation exists during construction" must inspect that whole path,
    not just ``__init__``, otherwise they silently miss anything that
    lives in a helper.  This function stays resilient as new ``_init_*``
    helpers are added.
    """
    parts = [inspect.getsource(cls.__init__)]
    for name in dir(cls):
        if name.startswith("_init_"):
            member = getattr(cls, name)
            if callable(member):
                parts.append(inspect.getsource(member))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Shared test doubles — used by test_app_controller_pool.py,
# test_app_controller_awaiting.py, and test_app_controller_permission_mode.py
# ---------------------------------------------------------------------------


class FakeRemoteContext(QObject):
    """Stand-in for remote_location.RemoteContext — no ssh, no threads.

    Exposes the same surface AppController consumes (paired / probing /
    server / remote_root / probe / pairingChanged); tests flip pairing by
    hand via set_paired. Single canonical definition (same rule as
    FakeSessionHost): import from here, don't copy per file.

    Fixtures swapping this in should ALSO no-op the controller's
    `_ensure_hub_link` / `_teardown_hub_link` (monkeypatch on the class,
    BEFORE construction) — a set_paired would otherwise spawn a real
    `ssh -N` forward child from inside the test run.
    """

    pairingChanged = Signal()

    def __init__(self, remote_root: str = "/opt/dev/repos/fake") -> None:
        super().__init__()
        self._server = None
        self._probing = False
        self._remote_root = remote_root
        self.probe_calls: list[str] = []

    @property
    def paired(self) -> bool:
        return self._server is not None

    @property
    def probing(self) -> bool:
        return self._probing

    @property
    def server(self):
        return self._server

    @property
    def remote_root(self) -> str:
        return self._remote_root if self._server is not None else ""

    def probe(self, project_root: str) -> None:
        self.probe_calls.append(project_root)

    def set_paired(self, server) -> None:
        self._server = server
        self.pairingChanged.emit()

    def set_probing(self, probing: bool) -> None:
        self._probing = probing


class FakeSshfsMount(QObject):
    """Stand-in for mount_manager.SshfsMount — no FUSE, no subprocess.

    mount() lands synchronously in the state set at construction
    (default "mounted") and emits stateChanged, so the controller's
    chrome-activation path runs deterministically in-test. Fixtures that
    exercise set_location("vps") MUST patch `symmetria_ide.app.SshfsMount`
    to this class (the real one spawns an sshfs subprocess).
    """

    stateChanged = Signal()
    # ClassVar, not an instance default: fixtures reset it with
    # `FakeSshfsMount.instances = []` and then assert on it from outside any
    # instance, so it is shared state by design. The annotation says so — it was
    # bare `list` before, which ruff reads as an accidental mutable default.
    instances: ClassVar[list] = []

    def __init__(self, server, remote_path, parent=None) -> None:
        super().__init__(parent)
        self.server = server
        self.remote_path = remote_path
        self.mount_calls = 0
        self.unmount_calls = 0
        self.result_state = "mounted"
        self._state = "unmounted"
        FakeSshfsMount.instances.append(self)

    @property
    def mountpoint(self) -> str:
        return f"/mnt/fake/{self.server.name}/{self.remote_path.rsplit('/', 1)[-1]}"

    @property
    def state(self) -> str:
        return self._state

    @property
    def mounted(self) -> bool:
        return self._state == "mounted"

    def healthy(self) -> bool:
        return self.mounted

    def mount(self) -> None:
        self.mount_calls += 1
        self._state = self.result_state
        self.stateChanged.emit()

    def unmount(self) -> None:
        self.unmount_calls += 1
        self._state = "unmounted"
        self.stateChanged.emit()


class FakeChromeHost(QObject):
    """Stand-in for chrome_host.ChromeHost — no browser, no compositor.

    `open_window` records the request and reports success WITHOUT calling the
    callback: the real host answers asynchronously (a CDP round-trip), and the
    controller's contract is that the slot is reserved synchronously while the
    target id binds later. Tests that care about the late binding drive it
    explicitly via `complete_open`.

    Fixtures swapping this in must patch `symmetria_ide.app.ChromeHost` BEFORE
    the first browser call, since the controller builds its host lazily.
    """

    windowUpdated = Signal(str, str, str)
    windowGone = Signal(str)
    browserGone = Signal()

    def __init__(self, project_root: str = "", parent=None) -> None:
        super().__init__(parent)
        self.project_root = project_root
        self.opened: list[str] = []
        self.closed: list[str] = []
        self.stop_calls = 0
        #: Set to an error code to make the next open fail (pool/degradation
        #: paths: "chrome-not-installed", "chrome-spawn-failed", …).
        self.open_error = ""
        self._callbacks: list = []

    def open_window(self, url: str, callback) -> str:
        if self.open_error:
            return self.open_error
        self.opened.append(url)
        self._callbacks.append(callback)
        return ""

    def complete_open(self, target_id: str, index: int = -1) -> None:
        """Deliver the CDP result for a pending open (binds the target id)."""
        self._callbacks[index]({"targetId": target_id})

    def close_window(self, target_id: str) -> None:
        self.closed.append(target_id)

    def stop(self) -> None:
        self.stop_calls += 1


class FakeSessionHost:
    """Stand-in for SessionHost — no threads, no subprocess, no network.

    Carries `instance_index` so tests can construct one fake per slot
    and assert dispatch routes to the right one. `is_running` is
    flipped True after `start()` to mimic the real host's spawn
    semantics — the cold-vs-hot branch of `submit_prompt_for` reads
    `is_running` to decide between `start(prompt)` and
    `send_user_message(prompt)`.

    Single canonical definition: define here, import in each test file
    that needs it. Adding a new capture field (e.g. Phase C's session_id)
    requires touching only this class, not N per-file copies.
    """

    def __init__(self, instance_index: int = 0) -> None:
        self.instance_index = instance_index
        self.is_running = False
        self.start_calls: list[str] = []
        self.send_calls: list[str] = []
        self.stop_calls = 0
        self.permission_calls: list[tuple[str, str]] = []
        self.set_permission_mode_calls: list[str] = []

    def start(self, prompt: str = "") -> None:
        self.start_calls.append(prompt)
        self.is_running = True

    def send_user_message(self, text: str) -> None:
        self.send_calls.append(text)

    def send_permission_response(self, request_id: str, behavior: str) -> None:
        self.permission_calls.append((request_id, behavior))

    def send_set_permission_mode(self, mode: str) -> None:
        self.set_permission_mode_calls.append(mode)

    def stop(self) -> None:
        self.stop_calls += 1
