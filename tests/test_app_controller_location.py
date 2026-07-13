"""AppController's Local ↔ VPS location state machine + RemoteContext pairing.

Hermetic shape (template: test_anchor_state.py): bare AppController, no QML
engine, no nvim, no ssh. The controller's RemoteContext is swapped for a
fake after construction — the real one is exercised separately below with
injected loader/prober callables, so no test in this file opens a network
connection.

Contract under test:
- `location` is "local" by default and only reaches "vps" while paired.
- `set_location` mirrors `set_central_surface`: invalid → rejected loudly,
  equal → silent no-op, refused vps → `locationAlert` (the chord's only
  feedback path on unpaired projects).
- A project switch (displayedRootChanged) force-resets to local silently
  and re-probes the new root.
- Losing the pairing while IN the vps context falls back to local with an
  alert — the context must never point at an unpaired server.
"""

from __future__ import annotations

import threading
import time

import pytest
from PySide6.QtCore import Qt

from conftest import FakeRemoteContext, FakeSshfsMount
from symmetria_ide.app import AppController
from symmetria_ide.remote_location import RemoteContext, remote_repo_path
from symmetria_ide.server_registry import RemoteServer

SERVER = RemoteServer(name="vigilia-vps", host="203.0.113.7")


@pytest.fixture
def controller(monkeypatch):
    # No-op the hub link + master teardown BEFORE construction: a
    # set_paired() would otherwise spawn a REAL `ssh -N` forward child
    # (and shutdown a real `ssh -O exit`) from inside the test run.
    monkeypatch.setattr(AppController, "_ensure_hub_link", lambda self: None)
    monkeypatch.setattr(AppController, "_teardown_hub_link", lambda self: None)
    monkeypatch.setattr(
        "symmetria_ide.app.ssh_runner.close_control_master", lambda server: None
    )
    # set_location("vps") runs the chrome swap — never a real sshfs (nor a
    # real ssh from the git worker's remote runner) in-test.
    FakeSshfsMount.instances = []
    monkeypatch.setattr("symmetria_ide.app.SshfsMount", FakeSshfsMount)
    monkeypatch.setattr("symmetria_ide.ssh_runner.run_remote", lambda *a, **k: None)
    ctrl = AppController()
    fake = FakeRemoteContext()
    # Swap AFTER construction: the real context's pairingChanged connect
    # becomes irrelevant (nothing emits it), the fake's is wired here.
    ctrl._remote_context = fake
    fake.pairingChanged.connect(ctrl._on_vps_pairing_changed)
    yield ctrl, fake
    ctrl.shutdown()


def _capture(signal) -> list:
    emissions: list = []
    signal.connect(lambda *args: emissions.append(args))
    return emissions


# ---------------------------------------------------------------------------
# Defaults + guarded transitions
# ---------------------------------------------------------------------------


def test_defaults_local_and_unavailable(controller):
    ctrl, _fake = controller
    assert ctrl.location == "local"
    assert ctrl.vpsAvailable is False
    assert ctrl.vpsServerName == ""


def test_set_location_vps_refused_while_unpaired(controller):
    ctrl, _fake = controller
    alerts = _capture(ctrl.locationAlert)
    changed = _capture(ctrl.locationChanged)
    ctrl.set_location("vps")
    assert ctrl.location == "local"
    assert len(alerts) == 1
    assert alerts[0][0] == "VPS not available"
    assert changed == []


def test_refusal_detail_mentions_probing_when_probe_in_flight(controller):
    ctrl, fake = controller
    fake.set_probing(True)
    alerts = _capture(ctrl.locationAlert)
    ctrl.set_location("vps")
    assert "probing" in alerts[0][1].lower()


def test_invalid_location_rejected_silently(controller):
    ctrl, _fake = controller
    changed = _capture(ctrl.locationChanged)
    alerts = _capture(ctrl.locationAlert)
    ctrl.set_location("cloud")
    assert ctrl.location == "local"
    assert changed == [] and alerts == []


def test_paired_switch_and_idempotence(controller):
    ctrl, fake = controller
    availability = _capture(ctrl.vpsAvailabilityChanged)
    fake.set_paired(SERVER)
    assert len(availability) == 1
    assert ctrl.vpsAvailable is True
    assert ctrl.vpsServerName == "vigilia-vps"

    changed = _capture(ctrl.locationChanged)
    ctrl.set_location("vps")
    assert ctrl.location == "vps"
    assert len(changed) == 1
    ctrl.set_location("vps")  # equal value → silent no-op
    assert len(changed) == 1


def test_toggle_location_round_trip(controller):
    ctrl, fake = controller
    fake.set_paired(SERVER)
    ctrl.toggle_location()
    assert ctrl.location == "vps"
    ctrl.toggle_location()
    assert ctrl.location == "local"


def test_toggle_from_unpaired_alerts_and_stays_local(controller):
    ctrl, _fake = controller
    alerts = _capture(ctrl.locationAlert)
    ctrl.toggle_location()
    assert ctrl.location == "local"
    assert len(alerts) == 1


# ---------------------------------------------------------------------------
# Root changes + pairing loss
# ---------------------------------------------------------------------------


def test_root_change_forces_local_and_reprobes(controller):
    ctrl, fake = controller
    fake.set_paired(SERVER)
    ctrl.set_location("vps")
    alerts = _capture(ctrl.locationAlert)
    ctrl._route_capsule({"id": "cwd", "value": "/tmp/other-project"})
    assert ctrl.location == "local"  # forced back, silently
    assert alerts == []
    assert fake.probe_calls[-1] == "/tmp/other-project"


def test_root_change_while_local_still_reprobes(controller):
    ctrl, fake = controller
    ctrl._route_capsule({"id": "cwd", "value": "/tmp/somewhere"})
    assert fake.probe_calls[-1] == "/tmp/somewhere"


def test_pairing_lost_while_vps_falls_back_with_alert(controller):
    ctrl, fake = controller
    fake.set_paired(SERVER)
    ctrl.set_location("vps")
    alerts = _capture(ctrl.locationAlert)
    changed = _capture(ctrl.locationChanged)
    fake.set_paired(None)
    assert ctrl.location == "local"
    assert len(changed) == 1
    assert alerts and alerts[0][0] == "VPS unavailable"


def test_pairing_lost_while_local_is_quiet(controller):
    ctrl, fake = controller
    fake.set_paired(SERVER)
    alerts = _capture(ctrl.locationAlert)
    changed = _capture(ctrl.locationChanged)
    fake.set_paired(None)
    assert ctrl.location == "local"
    assert changed == [] and alerts == []


# ---------------------------------------------------------------------------
# RemoteContext (real one, injected fakes — still no network)
#
# NO event pumping here: `QCoreApplication.processEvents()` on the shared
# session app drains EARLIER test modules' deleteLater queue and trips the
# 3.14 GC-vs-Qt SEGV (gotcha #10; see memory
# reference/qt-pyside/processevents_shared_app_segv.md). Instead the worker's
# `_probeFinished` emission is captured with a plain direct-connection spy
# (it runs synchronously IN the worker thread), awaited with a sleep-poll,
# and the queued GUI slot `_on_probe_finished` is then delivered BY HAND
# with the captured payload — exercising exactly what the real queued
# connection would carry, without touching the global event queue.
# ---------------------------------------------------------------------------


def _wait_until(predicate, timeout: float = 3.0) -> None:
    """Sleep-poll a worker-thread-driven condition (no Qt event pumping)."""
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert predicate(), "condition not reached while waiting"


def _spy_probe_finished(context: RemoteContext) -> list[tuple]:
    """Direct-connection capture of the worker's `_probeFinished` payloads.

    The connection type must be EXPLICITLY direct: a Python callable's
    wrapper takes the affinity of the connecting (main) thread, so the
    default AutoConnection resolves to queued for a worker-thread emit and
    the spy would never fire without pumping the loop.
    """
    payloads: list[tuple] = []
    context._probeFinished.connect(
        lambda *args: payloads.append(args),
        Qt.ConnectionType.DirectConnection,
    )
    return payloads


def _finish_probe(context: RemoteContext, payloads: list[tuple]) -> None:
    """Await the worker emit, then hand-deliver the queued GUI slot."""
    _wait_until(lambda: payloads)
    context._on_probe_finished(*payloads[-1])


def test_remote_repo_path_uses_basename():
    assert (
        remote_repo_path(SERVER, "/home/jc/projects/symmetria-ide/")
        == "/opt/dev/repos/symmetria-ide"
    )


def test_context_no_servers_means_unpaired_without_probing(qt_app):
    context = RemoteContext(servers_loader=lambda: [], prober=lambda s, r: True)
    changes = _capture(context.pairingChanged)
    context.probe("/tmp/project")
    assert context.paired is False
    assert context.probing is False
    assert changes == []  # nothing was paired before → no edge to report


def test_context_probe_pairs_on_match(qt_app):
    context = RemoteContext(servers_loader=lambda: [SERVER], prober=lambda s, r: True)
    payloads = _spy_probe_finished(context)
    context.probe("/home/jc/projects/demo")
    _finish_probe(context, payloads)
    assert context.paired is True
    assert context.server == SERVER
    assert context.remote_root == "/opt/dev/repos/demo"
    assert context.probing is False


def test_context_probe_unpaired_on_miss(qt_app):
    context = RemoteContext(servers_loader=lambda: [SERVER], prober=lambda s, r: False)
    payloads = _spy_probe_finished(context)
    context.probe("/home/jc/projects/demo")
    _finish_probe(context, payloads)
    assert context.paired is False
    assert context.probing is False


def test_context_first_matching_server_wins(qt_app):
    second = RemoteServer(name="homebox", host="10.0.0.9")
    probed: list[str] = []

    def prober(server, root):
        probed.append(server.name)
        return server.name == "vigilia-vps"

    context = RemoteContext(servers_loader=lambda: [SERVER, second], prober=prober)
    payloads = _spy_probe_finished(context)
    context.probe("/tmp/p")
    _finish_probe(context, payloads)
    assert context.server == SERVER
    assert probed == ["vigilia-vps"]  # match short-circuits


def test_context_stale_probe_result_discarded(qt_app):
    """A slow probe for root A must not pair after root B's probe superseded it."""
    release_first = threading.Event()
    started_first = threading.Event()

    def prober(server, root):
        if root == "/tmp/rootA":
            started_first.set()
            release_first.wait(timeout=5)
            return True  # would pair — but its generation is stale by now
        return False

    context = RemoteContext(servers_loader=lambda: [SERVER], prober=prober)
    payloads = _spy_probe_finished(context)
    context.probe("/tmp/rootA")
    assert started_first.wait(timeout=5)
    context.probe("/tmp/rootB")  # supersedes generation — rootA's worker
    # observes the bumped generation and returns WITHOUT emitting.
    release_first.set()
    _wait_until(lambda: payloads)  # rootB's miss
    for payload in payloads:
        context._on_probe_finished(*payload)
    assert context.paired is False  # rootB's miss is the live verdict
    assert context.probing is False
    # Even if rootA's worker HAD emitted a stale pairing, the generation
    # check in the GUI slot discards it:
    context._on_probe_finished(context._generation - 1, "/tmp/rootA", SERVER)
    assert context.paired is False


def test_context_probe_clears_previous_pairing_eagerly(qt_app):
    context = RemoteContext(servers_loader=lambda: [SERVER], prober=lambda s, r: True)
    payloads = _spy_probe_finished(context)
    context.probe("/tmp/p1")
    _finish_probe(context, payloads)
    assert context.paired is True
    changes = _capture(context.pairingChanged)
    context.probe("/tmp/p2")
    # Synchronously unpaired the moment the new probe starts — a stale
    # "paired" must never survive a project switch.
    assert context.paired is False
    assert len(changes) >= 1


def test_context_prober_exception_lands_unpaired_not_stuck(qt_app):
    def prober(server, root):
        raise RuntimeError("boom")

    context = RemoteContext(servers_loader=lambda: [SERVER], prober=prober)
    payloads = _spy_probe_finished(context)
    context.probe("/tmp/p")
    _finish_probe(context, payloads)
    assert context.paired is False
    assert context.probing is False
