"""Phase-2 integration specs for the live thread rail model.

The history readers and sleep/resume semantics belong to later phases.  These
tests therefore exercise only live pool records through AppController's
existing spawn, location, title, and close funnels.
"""

from __future__ import annotations

import importlib

import pytest
from conftest import FakeRemoteContext, FakeSshfsMount

from symmetria_ide.app import AppController
from symmetria_ide.server_registry import RemoteServer

SERVER = RemoteServer(name="thread-rail-vps", host="203.0.113.19")
REMOTE_ROOT = "/opt/dev/repos/thread-rail"


class _Bridge:
    """No-I/O bridge double for local pool registration."""

    def notify_spawn(self, _instance: dict) -> None:
        pass

    def notify_remove(self, _slot: int) -> None:
        pass

    def notify_focus(self, _slot: int) -> None:
        pass

    def notify_title(self, _slot: int, _title: str) -> None:
        pass

    def notify_activity(self, _slot: int, **_activity: object) -> None:
        pass

    def stop(self) -> None:
        pass


@pytest.fixture
def controller(monkeypatch):
    """A paired, hermetic controller so both location views are observable."""
    monkeypatch.delenv("SYMMETRIA_IDE_AGENT_PROMPT", raising=False)
    monkeypatch.delenv("SYMMETRIA_IDE_AGENT_VIEW", raising=False)
    monkeypatch.setattr(
        "symmetria_ide.app.shutil.which", lambda executable: f"/bin/{executable}"
    )
    monkeypatch.setattr(AppController, "_ensure_hub_link", lambda self: None)
    monkeypatch.setattr(AppController, "_teardown_hub_link", lambda self: None)
    monkeypatch.setattr(
        "symmetria_ide.app.ssh_runner.close_control_master", lambda server: None
    )
    FakeSshfsMount.instances = []
    monkeypatch.setattr("symmetria_ide.app.SshfsMount", FakeSshfsMount)
    monkeypatch.setattr("symmetria_ide.ssh_runner.run_remote", lambda *a, **k: None)

    value = AppController()
    value._agent_bridge = _Bridge()
    remote = FakeRemoteContext(remote_root=REMOTE_ROOT)
    value._remote_context = remote
    remote.pairingChanged.connect(value._on_vps_pairing_changed)
    remote.set_paired(SERVER)
    yield value
    value.shutdown()


def _thread_model(controller: AppController):
    """Find the typed model without prescribing its QML property spelling."""
    module = importlib.import_module("symmetria_ide.agent_thread_model")
    model_type = module.AgentThreadModel
    candidates = vars(controller).values()
    for candidate in candidates:
        if isinstance(candidate, model_type):
            return candidate
    pytest.fail("AppController does not own an AgentThreadModel")


def _role(model, name: str) -> int:
    for role, raw_name in model.roleNames().items():
        if bytes(raw_name).decode() == name:
            return role
    pytest.fail(f"AgentThreadModel has no {name!r} role")


def _column(model, name: str) -> list:
    role = _role(model, name)
    return [model.data(model.index(row, 0), role) for row in range(model.rowCount())]


def test_live_thread_rows_match_active_location_and_dense_agent_order(
    controller: AppController,
) -> None:
    """Spec: one row per live agent, ordered exactly like ``agentOrder``."""
    controller.spawn_agent("fresh", True, "claude")
    controller.spawn_agent("fresh", True, "opencode")
    local_slots = list(controller.agentOrder)

    controller.set_location("vps")
    controller.spawn_agent("fresh", True, "claude")
    vps_slots = list(controller.agentOrder)

    model = _thread_model(controller)
    assert _column(model, "slot") == vps_slots
    assert model.rowCount() == len(vps_slots)

    controller.set_location("local")
    assert _column(model, "slot") == local_slots
    assert _column(model, "harness") == ["claude", "opencode"]

    # Dense display order is historical order, not numeric slot order.  Reuse
    # slot 1 after closing it: the survivor stays first and the newcomer appends.
    controller.close_agent(local_slots[0])
    controller.spawn_agent("fresh", True, "claude")
    assert controller.agentOrder == [local_slots[1], local_slots[0]]
    assert _column(model, "slot") == controller.agentOrder


def test_thread_field_update_emits_explicit_roles_without_model_reset(
    controller: AppController,
) -> None:
    """Spec: a title mutation is a scoped row update, never a reset."""
    controller.spawn_agent("fresh", True, "claude")
    slot = controller.focusedAgent
    model = _thread_model(controller)
    changes: list[tuple[int, int, list[int]]] = []
    resets: list[None] = []
    model.dataChanged.connect(
        lambda top, bottom, roles: changes.append(
            (top.row(), bottom.row(), [int(role) for role in roles])
        )
    )
    model.modelReset.connect(lambda: resets.append(None))

    controller.on_agent_title(slot, "Implement the thread rail")

    assert resets == []
    assert changes, "updating a live thread field emitted no dataChanged"
    assert all(roles for _top, _bottom, roles in changes), (
        "every dataChanged emission must carry an explicit role list"
    )
    title_role = _role(model, "title")
    assert any(title_role in roles for _top, _bottom, roles in changes)
    assert _column(model, "title") == ["Implement the thread rail"]


def test_slot_lookup_returns_dead_sentinel_for_absent_selection() -> None:
    """Regression: a stale ListView index never resolves to a recycled slot."""
    module = importlib.import_module("symmetria_ide.agent_thread_model")
    model = module.AgentThreadModel()
    model.set_rows(
        [
            module.ThreadRow(
                thread_id="claude:live-session",
                harness="claude",
                session_id="live-session",
                title="Live thread",
                updated_at=1,
                work_root="/project",
                worktree="",
                slot=7,
            )
        ]
    )

    assert model.slot_at(0) == 7
    assert model.slot_at(-1) == 0
    assert model.slot_at(1) == 0
