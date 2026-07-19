"""Shared pytest fixtures and helpers for the Symmetria IDE test suite.

Centralises infrastructure that would otherwise be duplicated across
structural (source-inspection) test modules.  Any test file that does
structural source checks or needs a bare QCoreApplication should import
from here rather than redefining these helpers locally.
"""

from __future__ import annotations

import inspect
import sys

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
    on each construction. Same override semantics as the fixtures below."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path_factory.mktemp("xdg_config")))


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
    instances: list = []

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
