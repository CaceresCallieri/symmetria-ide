"""VPS agents in the terminal-agent pool: spawn/attach/detach/kill, the
location-filtered display order, focus handoff, and the hub-snapshot
activity path (disjointness with the shell-bridge handler).

Hermetic: bare AppController with the conftest FakeRemoteContext swapped in,
hub link no-op'd (a real pairing edge would spawn an `ssh -N` child), bridge
publishes spied. NO event pumping (gotcha #10 / processevents_shared_app_segv
memory) — worker signals are spied with explicit DirectConnection and their
queued GUI slots delivered by hand.
"""

from __future__ import annotations

import time

import pytest
from conftest import FakeRemoteContext, FakeSshfsMount
from PySide6.QtCore import Qt

from symmetria_ide import ssh_runner
from symmetria_ide.app import AppController
from symmetria_ide.server_registry import RemoteServer

SERVER = RemoteServer(name="vigilia-vps", host="203.0.113.7")
REMOTE_ROOT = "/opt/dev/repos/fake"


class _BridgeSpy:
    """Records the shell-bridge publishes AppController makes."""

    def __init__(self, ctrl: AppController) -> None:
        self.spawns: list[dict] = []
        self.removes: list[int] = []
        self.focuses: list[int] = []
        ctrl._agent_bridge.notify_spawn = self.spawns.append
        ctrl._agent_bridge.notify_remove = self.removes.append
        ctrl._agent_bridge.notify_focus = self.focuses.append


@pytest.fixture
def controller(monkeypatch):
    monkeypatch.setattr(AppController, "_ensure_hub_link", lambda self: None)
    monkeypatch.setattr(AppController, "_teardown_hub_link", lambda self: None)
    monkeypatch.setattr(
        "symmetria_ide.app.ssh_runner.close_control_master", lambda server: None
    )
    # set_location("vps") runs the chrome swap, which would otherwise mount
    # a REAL sshfs — the fake lands "mounted" synchronously instead.
    FakeSshfsMount.instances = []
    monkeypatch.setattr("symmetria_ide.app.SshfsMount", FakeSshfsMount)
    # ...and the git worker's remote runner must never open a real ssh
    # (tests that care about run_remote's argv re-patch this themselves).
    monkeypatch.setattr("symmetria_ide.ssh_runner.run_remote", lambda *a, **k: None)
    # Local spawns must not depend on a claude binary on the test machine.
    monkeypatch.setattr("symmetria_ide.app.shutil.which", lambda name: f"/bin/{name}")
    ctrl = AppController()
    fake = FakeRemoteContext(remote_root=REMOTE_ROOT)
    ctrl._remote_context = fake
    fake.pairingChanged.connect(ctrl._on_vps_pairing_changed)
    spy = _BridgeSpy(ctrl)
    fake.set_paired(SERVER)
    yield ctrl, fake, spy
    ctrl.shutdown()


def _capture(signal) -> list:
    emissions: list = []
    signal.connect(lambda *args: emissions.append(args))
    return emissions


def _spawn_local(ctrl) -> int:
    assert ctrl.location == "local"
    ctrl.spawn_agent("fresh", True, "claude")
    return ctrl._focused_term_agent


def _spawn_vps(ctrl) -> int:
    assert ctrl.location == "vps"
    ctrl.spawn_agent("fresh", True, "claude")
    return ctrl._focused_term_agent


# ---------------------------------------------------------------------------
# Spawn: record shape + publish rules
# ---------------------------------------------------------------------------


def test_vps_spawn_record_and_no_bridge_publish(controller):
    ctrl, _fake, spy = controller
    ctrl.set_location("vps")
    slot = _spawn_vps(ctrl)
    inst = ctrl._term_agents[slot]
    assert inst["location"] == "vps"
    assert inst["server"] == SERVER
    assert inst["cwd"] == REMOTE_ROOT
    assert inst["remote_root"] == REMOTE_ROOT
    assert inst["tmux_session"].endswith(f"-vps-{slot}")
    assert inst["tmux_socket"] == SERVER.tmux_socket
    # VPS agents are the server hub's, not this machine's: no shell-bridge
    # publish, no focus notify, no registry entry.
    assert spy.spawns == []
    assert spy.focuses == []


def test_local_spawn_still_publishes(controller):
    ctrl, _fake, spy = controller
    slot = _spawn_local(ctrl)
    assert ctrl._term_agents[slot].get("location", "local") == "local"
    assert len(spy.spawns) == 1
    assert spy.focuses  # focus_agent notifies for local agents


def test_vps_spawn_refuses_opencode(controller):
    ctrl, _fake, _spy = controller
    ctrl.set_location("vps")
    alerts = _capture(ctrl.locationAlert)
    ctrl.spawn_agent("fresh", True, "opencode")
    assert ctrl._term_agents == {}
    assert alerts and "claude-only" in alerts[0][0]


def test_vps_spawn_argv_is_remote(controller):
    ctrl, _fake, _spy = controller
    ctrl.set_location("vps")
    slot = _spawn_vps(ctrl)
    argv = ctrl.agent_spawn_argv(slot)
    assert argv[0] == "ssh" and argv[1] == "-t"
    assert "dev@203.0.113.7" in argv


def test_vps_spawn_never_requests_local_mcp_config(controller, monkeypatch):
    """The launch-readiness gate is local-only; VPS agents use the remote hub
    and must not receive a path owned by this IDE's local MCP server."""
    ctrl, _fake, _spy = controller

    def unexpected_config(*_args, **_kwargs):
        pytest.fail("VPS argv requested a local browser-MCP config")

    monkeypatch.setattr(
        ctrl._browser_mcp_server, "agent_config_path", unexpected_config
    )
    monkeypatch.setattr(
        ctrl._browser_mcp_server, "agent_config_content", unexpected_config
    )
    ctrl.set_location("vps")
    argv = ctrl.agent_spawn_argv(_spawn_vps(ctrl))
    assert argv[0] == "ssh"
    assert "--mcp-config" not in argv
    assert not any(arg.startswith("SYMMETRIA_IDE_MCP_CONFIG=") for arg in argv)


# ---------------------------------------------------------------------------
# Location-filtered display order + focus handoff
# ---------------------------------------------------------------------------


def test_agent_order_filters_by_location(controller):
    ctrl, _fake, _spy = controller
    local_slot = _spawn_local(ctrl)
    ctrl.set_location("vps")
    vps_slot = _spawn_vps(ctrl)
    assert ctrl.agentOrder == [vps_slot]
    ctrl.set_location("local")
    assert ctrl.agentOrder == [local_slot]
    # The physical pool holds both; both Loaders stay active (no churn).
    assert ctrl.agentSlotActive[local_slot - 1] is True
    assert ctrl.agentSlotActive[vps_slot - 1] is True


def test_toggle_hands_focus_off_and_restores(controller):
    ctrl, _fake, _spy = controller
    local_slot = _spawn_local(ctrl)
    ctrl.set_location("vps")
    assert ctrl.focusedAgent == 0  # empty vps pool
    vps_slot = _spawn_vps(ctrl)
    assert ctrl.focusedAgent == vps_slot
    ctrl.set_location("local")
    assert ctrl.focusedAgent == local_slot
    ctrl.set_location("vps")
    assert ctrl.focusedAgent == vps_slot


def test_focus_invariant_holds_across_toggles(controller):
    ctrl, _fake, _spy = controller
    _spawn_local(ctrl)
    ctrl.set_location("vps")
    _spawn_vps(ctrl)
    for _ in range(3):
        ctrl.toggle_location()
        focused = ctrl.focusedAgent
        assert focused == 0 or (ctrl._agent_location(focused) == ctrl.location), (
            "focusedAgent must be 0 or a current-location slot"
        )


def test_focus_agent_follows_location(controller):
    ctrl, _fake, _spy = controller
    _spawn_local(ctrl)
    ctrl.set_location("vps")
    vps_slot = _spawn_vps(ctrl)
    ctrl.set_location("local")
    # Programmatic focus of the vps slot (dashboard click) flips location.
    ctrl.focus_agent(vps_slot)
    assert ctrl.location == "vps"
    assert ctrl.focusedAgent == vps_slot


def test_cycle_stays_within_location(controller):
    ctrl, _fake, _spy = controller
    a = _spawn_local(ctrl)
    b = _spawn_local(ctrl)
    ctrl.set_location("vps")
    _spawn_vps(ctrl)
    ctrl.set_location("local")
    assert ctrl.focusedAgent in (a, b)
    seen = set()
    for _ in range(4):
        ctrl.cycle_agent_focus(1)
        seen.add(ctrl.focusedAgent)
    assert seen == {a, b}  # never wanders into the vps slot


# ---------------------------------------------------------------------------
# Detach vs kill
# ---------------------------------------------------------------------------


def test_close_vps_agent_detaches_never_kills(controller, monkeypatch):
    ctrl, _fake, _spy = controller

    def _fail_run(*args, **kwargs):  # pragma: no cover - assertion vehicle
        raise AssertionError("close of a vps agent must not exec tmux kill")

    monkeypatch.setattr("symmetria_ide.app.subprocess.run", _fail_run)
    ctrl.set_location("vps")
    slot = _spawn_vps(ctrl)
    alerts = _capture(ctrl.locationAlert)
    ctrl.close_agent(slot)
    assert slot not in ctrl._term_agents
    assert alerts and alerts[0][0] == "Detached"
    assert SERVER.name in alerts[0][1]


def test_close_local_agent_still_kills_its_tmux(controller, monkeypatch):
    ctrl, _fake, _spy = controller
    killed: list[list[str]] = []
    monkeypatch.setattr(
        "symmetria_ide.app.subprocess.run",
        lambda argv, **kw: killed.append(argv),
    )
    slot = _spawn_local(ctrl)
    # Local agents only recorded a tmux_socket when the wrap ran; simulate.
    ctrl._term_agents[slot]["tmux_socket"] = "/tmp/x.sock"
    ctrl.close_agent(slot)
    assert killed and killed[0][:2] == ["tmux", "-S"]


def test_kill_remote_agent_runs_remote_kill_then_closes(controller, monkeypatch):
    ctrl, _fake, _spy = controller
    ctrl.set_location("vps")
    slot = _spawn_vps(ctrl)
    name = ctrl._term_agents[slot]["tmux_session"]
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "symmetria_ide.app.ssh_runner.run_remote",
        lambda server, argv, **kw: calls.append(argv) or None,
    )
    done: list[tuple] = []
    # Explicit DirectConnection: the worker thread's emit must land in the
    # spy synchronously (AutoConnection would queue it — see the module
    # docstring's no-pumping rule).
    ctrl._remote_kill_done.connect(
        lambda s: done.append((s,)), Qt.ConnectionType.DirectConnection
    )
    alerts = _capture(ctrl.locationAlert)
    ctrl.kill_remote_agent(slot)
    deadline = time.monotonic() + 3.0
    while not done and time.monotonic() < deadline:
        time.sleep(0.01)
    assert done == [(slot,)]
    assert calls == [["tmux", "-S", SERVER.tmux_socket, "kill-session", "-t", name]]
    # Hand-deliver the queued GUI tail.
    ctrl._on_remote_kill_done(slot)
    assert slot not in ctrl._term_agents
    # An explicit kill must NOT show the "still running" detach toast.
    assert all(entry[0] != "Detached" for entry in alerts)


def test_kill_remote_agent_refuses_local_slot(controller):
    ctrl, _fake, _spy = controller
    slot = _spawn_local(ctrl)
    ctrl.kill_remote_agent(slot)
    assert slot in ctrl._term_agents  # untouched


def test_vps_fast_finish_alerts_attach_failure(controller):
    ctrl, _fake, _spy = controller
    ctrl.set_location("vps")
    slot = _spawn_vps(ctrl)
    alerts = _capture(ctrl.locationAlert)
    ctrl.on_agent_finished(slot)  # lifetime ≈ 0 → fast death
    assert slot not in ctrl._term_agents
    assert alerts[0][0] == "VPS agent attach failed"
    # The detach toast is suppressed after the failure alert.
    assert all(entry[0] != "Detached" for entry in alerts)


def test_vps_slow_finish_is_a_plain_detach(controller):
    ctrl, _fake, _spy = controller
    ctrl.set_location("vps")
    slot = _spawn_vps(ctrl)
    ctrl._term_agents[slot]["spawn_mono"] = time.monotonic() - 3600
    alerts = _capture(ctrl.locationAlert)
    ctrl.on_agent_finished(slot)
    assert alerts and alerts[0][0] == "Detached"


# ---------------------------------------------------------------------------
# Attach picker paths
# ---------------------------------------------------------------------------


def test_attach_remote_session_creates_attach_record(controller):
    ctrl, _fake, _spy = controller
    ctrl.set_location("vps")
    ctrl.attach_remote_session("symmetria-ide-abcd", True)
    slot = ctrl.focusedAgent
    inst = ctrl._term_agents[slot]
    assert inst["spawn_type"] == "attach"
    assert inst["tmux_session"] == "symmetria-ide-abcd"
    assert inst["location"] == "vps"
    # Pure attach argv: no inner command after the session name.
    argv = ctrl.agent_spawn_argv(slot)
    assert argv[-1].endswith("symmetria-ide-abcd")


def test_attach_duplicate_focuses_existing(controller):
    ctrl, _fake, _spy = controller
    ctrl.set_location("vps")
    ctrl.attach_remote_session("dup-session", True)
    first = ctrl.focusedAgent
    ctrl.attach_remote_session("dup-session", True)
    assert ctrl.focusedAgent == first
    assert len(ctrl._term_agents) == 1


def test_attach_refused_outside_vps(controller):
    ctrl, _fake, _spy = controller
    ctrl.attach_remote_session("x", True)
    assert ctrl._term_agents == {}


def test_request_remote_sessions_filters_attached(controller, monkeypatch):
    import subprocess as _subprocess

    ctrl, _fake, _spy = controller
    ctrl.set_location("vps")
    ctrl.attach_remote_session("already-attached", True)
    stdout = (
        f"already-attached\t100\t{REMOTE_ROOT}\nphone-session\t200\t{REMOTE_ROOT}\n"
    )
    monkeypatch.setattr(
        "symmetria_ide.app.ssh_runner.run_remote",
        lambda server, argv, **kw: _subprocess.CompletedProcess(argv, 0, stdout, ""),
    )
    payloads: list[dict] = []
    ctrl._remote_tmux_sessions_fetched.connect(
        payloads.append, Qt.ConnectionType.DirectConnection
    )
    ctrl.request_remote_tmux_sessions()
    deadline = time.monotonic() + 3.0
    while not payloads and time.monotonic() < deadline:
        time.sleep(0.01)
    assert payloads and payloads[0]["ok"] is True
    names = [row["name"] for row in payloads[0]["sessions"]]
    assert names == ["phone-session"]
    # Hand-deliver the queued GUI slot → the QML-facing re-emit.
    ready = _capture(ctrl.remoteTmuxSessionsReady)
    ctrl._on_remote_tmux_sessions(payloads[0])
    assert ready and ready[0][0]["ok"] is True


def test_request_remote_sessions_empty_socket_is_empty_not_error(
    controller, monkeypatch
):
    import subprocess as _subprocess

    ctrl, _fake, _spy = controller
    ctrl.set_location("vps")
    monkeypatch.setattr(
        "symmetria_ide.app.ssh_runner.run_remote",
        lambda server, argv, **kw: _subprocess.CompletedProcess(
            argv, 1, "", "no server running on /home/dev/.vigilia/tmux.sock"
        ),
    )
    payloads: list[dict] = []
    ctrl._remote_tmux_sessions_fetched.connect(
        payloads.append, Qt.ConnectionType.DirectConnection
    )
    ctrl.request_remote_tmux_sessions()
    deadline = time.monotonic() + 3.0
    while not payloads and time.monotonic() < deadline:
        time.sleep(0.01)
    assert payloads[0] == {"ok": True, "sessions": []}


# ---------------------------------------------------------------------------
# Hub snapshot: mapping + disjointness with the shell-bridge handler
# ---------------------------------------------------------------------------


def _hub_payload(session_name: str, **agent_fields) -> dict:
    agent = {
        "id": session_name,
        "tmux_session": session_name,
        "activity_state": "working",
        "activity_tool": "Bash",
        "agent_type": "claude",
        "session_id": "",
        "title": "",
    }
    agent.update(agent_fields)
    return {"agents": [agent]}


def test_hub_snapshot_maps_by_tmux_session(controller):
    ctrl, _fake, _spy = controller
    ctrl.set_location("vps")
    slot = _spawn_vps(ctrl)
    name = ctrl._term_agents[slot]["tmux_session"]
    ctrl._on_hub_snapshot(_hub_payload(name, session_id="sess-42"))
    assert ctrl._term_agent_activity[slot]["state"] == "working"
    assert ctrl._term_agents[slot]["session_id"] == "sess-42"


def test_hub_snapshot_backfills_title_until_osc_lands(controller):
    ctrl, _fake, _spy = controller
    ctrl.set_location("vps")
    slot = _spawn_vps(ctrl)
    name = ctrl._term_agents[slot]["tmux_session"]
    ctrl._on_hub_snapshot(_hub_payload(name, title="✳ fix the parser"))
    assert ctrl._term_agents[slot]["title"] == "fix the parser"
    # An existing title (the pane's own OSC) wins — no overwrite.
    ctrl._term_agents[slot]["title"] = "osc title"
    ctrl._on_hub_snapshot(_hub_payload(name, title="hub title"))
    assert ctrl._term_agents[slot]["title"] == "osc title"


def test_hub_snapshot_ignores_unknown_sessions(controller):
    ctrl, _fake, _spy = controller
    ctrl.set_location("vps")
    _spawn_vps(ctrl)
    before = dict(ctrl._term_agent_activity)
    ctrl._on_hub_snapshot(_hub_payload("someone-elses-session"))
    assert ctrl._term_agent_activity == before


def test_hub_and_bridge_snapshots_own_disjoint_slots(controller):
    """The interleaving hazard: neither handler may wipe the other's slots."""
    import os

    ctrl, _fake, _spy = controller
    local_slot = _spawn_local(ctrl)
    ctrl.set_location("vps")
    vps_slot = _spawn_vps(ctrl)
    vps_name = ctrl._term_agents[vps_slot]["tmux_session"]

    # Local slot's activity arrives via the SHELL bridge (pid-prefixed id).
    bridge_payload = {
        "agents": [
            {
                "id": f"{os.getpid()}_{local_slot}",
                "activity_state": "thinking",
                "activity_tool": "",
                "agent_type": "claude",
            }
        ]
    }
    ctrl._on_bridge_snapshot(bridge_payload)
    assert ctrl._term_agent_activity[local_slot]["state"] == "thinking"

    # Hub snapshot lands: sets the vps slot, CARRIES the local one through.
    ctrl._on_hub_snapshot(_hub_payload(vps_name))
    assert ctrl._term_agent_activity[vps_slot]["state"] == "working"
    assert ctrl._term_agent_activity[local_slot]["state"] == "thinking"

    # A later bridge snapshot that OMITS our local agent (idle) clears it,
    # but must CARRY the vps slot through untouched.
    ctrl._on_bridge_snapshot({"agents": []})
    assert local_slot not in ctrl._term_agent_activity
    assert ctrl._term_agent_activity[vps_slot]["state"] == "working"

    # And an idle hub snapshot clears the vps slot without resurrecting
    # or touching anything local.
    ctrl._on_hub_snapshot({"agents": []})
    assert vps_slot not in ctrl._term_agent_activity


# ---------------------------------------------------------------------------
# Hub link lifecycle (real _ensure/_teardown, fakes for the two pieces)
# ---------------------------------------------------------------------------


class _FakeForward:
    instances: list = []

    def __init__(self, server, remote_sock, local_sock, parent=None):

        self._server = server
        self.remote_sock = remote_sock
        self._local = local_sock
        self.started = 0
        self.stopped = 0
        # Signal-shaped attribute: tests don't drive `lost`, so a stub
        # with connect() suffices.
        self.lost = type("_Sig", (), {"connect": staticmethod(lambda *a, **k: None)})()
        _FakeForward.instances.append(self)

    @property
    def local_socket(self):
        return self._local

    def is_running(self):
        return self.started > self.stopped

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1


class _FakeHubClient:
    instances: list = []

    def __init__(self, parent=None, *, socket_path=None):
        self.socket_path = socket_path
        self.started = 0
        self.stopped = 0
        self.snapshot_received = type(
            "_Sig", (), {"connect": staticmethod(lambda *a, **k: None)}
        )()
        _FakeHubClient.instances.append(self)

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1


@pytest.fixture
def hub_controller(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    _FakeForward.instances = []
    _FakeHubClient.instances = []
    monkeypatch.setattr(ssh_runner, "SocketForward", _FakeForward)
    monkeypatch.setattr("symmetria_ide.app.AgentBridgeClient", _FakeHubClient)
    monkeypatch.setattr(
        "symmetria_ide.app.ssh_runner.close_control_master", lambda server: None
    )
    ctrl = AppController()
    fake = FakeRemoteContext(remote_root=REMOTE_ROOT)
    ctrl._remote_context = fake
    fake.pairingChanged.connect(ctrl._on_vps_pairing_changed)
    yield ctrl, fake
    ctrl.shutdown()


def test_pairing_establishes_and_teardown_stops_hub_link(hub_controller):
    ctrl, fake = hub_controller
    fake.set_paired(SERVER)
    assert len(_FakeForward.instances) == 1
    forward = _FakeForward.instances[0]
    assert forward.remote_sock == SERVER.hub_socket
    assert forward.local_socket.endswith(f"hub-{SERVER.name}.sock")
    assert forward.started == 1
    # The main shell-bridge client is also a _FakeHubClient (the class is
    # patched module-wide); the hub client is the one with a socket_path.
    (client,) = [c for c in _FakeHubClient.instances if c.socket_path]
    assert client.socket_path == forward.local_socket
    assert client.started == 1
    # Re-pairing the same healthy link is a no-op (no churn).
    fake.set_paired(SERVER)
    assert len(_FakeForward.instances) == 1
    # Pairing loss tears both pieces down.
    fake.set_paired(None)
    assert forward.stopped >= 1
    assert client.stopped == 1


# ---------------------------------------------------------------------------
# Chrome swap (Phase 3): git runner + tree mount follow the location.
# ---------------------------------------------------------------------------


class _GitSpy:
    """Records the git controllers' location-seam swaps."""

    def __init__(self, ctrl: AppController) -> None:
        self.remote_calls: list[tuple] = []
        self.local_calls = 0
        self.executors: dict[str, list] = {"log": [], "branch": [], "ops": []}
        ctrl._git_controller.set_remote = lambda runner, remote_root, mount: (
            self.remote_calls.append((runner, remote_root, mount))
        )
        ctrl._git_controller.set_local = lambda: setattr(
            self, "local_calls", self.local_calls + 1
        )
        ctrl._git_log_controller.set_executor = self.executors["log"].append
        ctrl._git_branch_controller.set_executor = self.executors["branch"].append
        ctrl._git_ops_controller.set_executor = self.executors["ops"].append


def test_toggle_to_vps_swaps_git_and_tree(controller):
    ctrl, _fake, _spy = controller
    git_spy = _GitSpy(ctrl)
    mounts: list[tuple] = []
    ctrl.treeMountRequested.connect(lambda root, expanded: mounts.append(root))
    ctrl.set_location("vps")
    (mount,) = FakeSshfsMount.instances
    assert mount.mount_calls == 1
    assert git_spy.remote_calls and git_spy.remote_calls[0][1] == REMOTE_ROOT
    assert git_spy.remote_calls[0][2] == mount.mountpoint
    assert mounts and mounts[-1] == mount.mountpoint
    assert ctrl.vpsProjectLabel == "vigilia-vps:fake"
    # Phase 4: the whole git surface follows — one shared remote executor.
    for name in ("log", "branch", "ops"):
        (executor,) = git_spy.executors[name]
        assert executor.is_remote
        assert executor.remote_root == REMOTE_ROOT
        assert executor.local_mount == mount.mountpoint


def test_toggle_back_restores_local_chrome(controller):
    ctrl, _fake, _spy = controller
    git_spy = _GitSpy(ctrl)
    ctrl.set_location("vps")
    mounts: list[str] = []
    ctrl.treeMountRequested.connect(lambda root, expanded: mounts.append(root))
    ctrl.set_location("local")
    assert git_spy.local_calls == 1
    for name in ("log", "branch", "ops"):
        assert not git_spy.executors[name][-1].is_remote
    assert mounts and mounts[-1] == ctrl.displayedRoot
    # The mount is KEPT for the session (cheap re-toggle); only pairing
    # loss / shutdown drop it.
    (mount,) = FakeSshfsMount.instances
    assert mount.unmount_calls == 0


def test_mount_failure_falls_back_to_local_with_alert(controller):
    ctrl, _fake, _spy = controller
    _GitSpy(ctrl)

    alerts = _capture(ctrl.locationAlert)
    # Arrange the fake to land in "failed" instead of "mounted".
    ctrl.set_location("vps")  # creates the mount (mounted) — reset it:
    (mount,) = FakeSshfsMount.instances
    ctrl.set_location("local")
    mount.result_state = "failed"
    mount._state = "unmounted"
    ctrl.set_location("vps")
    assert ctrl.location == "local"  # bounced back
    assert any(entry[0] == "VPS files unavailable" for entry in alerts)


def test_pairing_loss_drops_the_mount(controller):
    ctrl, fake, _spy = controller
    _GitSpy(ctrl)
    ctrl.set_location("vps")
    (mount,) = FakeSshfsMount.instances
    fake.set_paired(None)
    assert mount.unmount_calls == 1
    assert ctrl._repo_mount is None
