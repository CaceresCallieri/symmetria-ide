"""Phase-1 contract for the append-only terminal-agent pane registry.

These tests drive the existing AppController entry points.  They deliberately
do not start QML-owned terminal processes: spawn_agent only registers the
record, while a real QML Loader owns process creation.
"""

from __future__ import annotations

import importlib

import pytest

from symmetria_ide.app import AppController


class _Bridge:
    """No-I/O double for the bridge calls made by pool registration."""

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


@pytest.fixture
def controller(monkeypatch) -> AppController:
    monkeypatch.delenv("SYMMETRIA_IDE_AGENT_PROMPT", raising=False)
    monkeypatch.delenv("SYMMETRIA_IDE_AGENT_VIEW", raising=False)
    monkeypatch.setattr(
        "symmetria_ide.app.shutil.which", lambda executable: f"/bin/{executable}"
    )
    value = AppController()
    value._agent_bridge = _Bridge()
    return value


def _pane_model(controller: AppController):
    """Find the public/private controller seam without prescribing its spelling."""
    module = importlib.import_module("symmetria_ide.agent_pane_slots")
    model_type = module.AgentPaneSlotModel
    for name in ("agentPaneSlots", "agentPaneSlotModel", "_agent_pane_slots"):
        candidate = getattr(controller, name, None)
        if isinstance(candidate, model_type):
            return candidate
    pytest.fail("AppController does not own/expose an AgentPaneSlotModel")


def _role(model, name: str) -> int:
    for role, raw_name in model.roleNames().items():
        if bytes(raw_name).decode() == name:
            return role
    pytest.fail(f"AgentPaneSlotModel has no {name!r} role")


def _row_for_slot(model, slot: int) -> int:
    slot_role = _role(model, "slot")
    for row in range(model.rowCount()):
        if model.data(model.index(row, 0), slot_role) == slot:
            return row
    pytest.fail(f"pane registry has no row for slot {slot}")


def _active(model, slot: int) -> bool:
    row = _row_for_slot(model, slot)
    return bool(model.data(model.index(row, 0), _role(model, "active")))


def test_spawn_supports_sixth_and_ninth_concurrent_local_agent(
    controller: AppController, caplog: pytest.LogCaptureFixture
) -> None:
    """Guard: terminal agents are not capped by the five browser/SDK slots."""
    for _ in range(9):
        controller.spawn_agent("fresh", True, "claude")

    assert controller.agentOrder == list(range(1, 10))
    assert controller.focusedAgent == 9
    assert not any("pool full" in record.message for record in caplog.records)


def test_registry_mutations_emit_only_insert_or_scoped_data_change(
    controller: AppController,
) -> None:
    """Guard: pool lifetime never rides reset/remove/move model operations."""
    model = _pane_model(controller)
    allowed: list[str] = []
    forbidden: list[str] = []
    model.rowsInserted.connect(
        lambda _parent, _first, _last: allowed.append("rowsInserted")
    )
    model.dataChanged.connect(
        lambda _top, _bottom, roles: allowed.append(
            "dataChanged" if list(roles) else "bareDataChanged"
        )
    )
    model.modelReset.connect(lambda: forbidden.append("modelReset"))
    model.rowsRemoved.connect(
        lambda _parent, _first, _last: forbidden.append("rowsRemoved")
    )
    model.rowsMoved.connect(lambda *_args: forbidden.append("rowsMoved"))

    controller.spawn_agent("fresh", True, "claude")
    controller.spawn_agent("fresh", True, "claude")
    controller.close_agent(1)
    controller.spawn_agent("fresh", True, "claude")

    assert "rowsInserted" in allowed
    assert "dataChanged" in allowed
    assert "bareDataChanged" not in allowed
    assert forbidden == []


def test_close_retains_inactive_row_and_next_spawn_reuses_lowest_slot(
    controller: AppController,
) -> None:
    """Guard: release flips a row; it never deletes that registry row."""
    for _ in range(3):
        controller.spawn_agent("fresh", True, "claude")
    model = _pane_model(controller)
    assert model.rowCount() == 3

    controller.close_agent(2)
    assert model.rowCount() == 3
    assert _active(model, 2) is False

    controller.spawn_agent("fresh", True, "claude")
    assert model.rowCount() == 3
    assert _active(model, 2) is True
    assert controller.agentOrder == [1, 3, 2]


def test_public_occupancy_projection_matches_model_through_slot_reuse(
    controller: AppController,
) -> None:
    """Guard: QML's Loader flags cannot drift from registry occupancy."""
    model = _pane_model(controller)

    def assert_same_occupancy() -> None:
        model_flags = [_active(model, slot) for slot in range(1, model.rowCount() + 1)]
        assert controller.agentSlotActive == model_flags

    for _ in range(3):
        controller.spawn_agent("fresh", True, "claude")
    assert_same_occupancy()

    controller.close_agent(2)
    assert_same_occupancy()
    assert controller.agentSlotActive == [True, False, True]

    controller.spawn_agent("fresh", True, "claude")
    assert_same_occupancy()
    assert controller.agentSlotActive == [True, True, True]


def test_all_per_slot_lists_track_registry_high_water_past_slot_five(
    controller: AppController,
) -> None:
    """Guard: every slot-indexed QVariantList remains safe for slot > 5."""
    for _ in range(6):
        controller.spawn_agent("fresh", True, "claude")
    model = _pane_model(controller)
    controller.focus_agent(6)

    properties = (
        "agentSlotActive",
        "agentTitles",
        "agentWorktree",
        "agentModels",
        "agentEfforts",
        "agentContextPct",
        "agentContextDisplay",
        "agentDoneAt",
        "agentActivity",
        "agentBrowserCount",
        "agentBrowserAttention",
        "agentCoordAttention",
    )
    high_water = model.rowCount()
    assert high_water == 6
    assert controller.focusedAgent == 6
    for name in properties:
        values = getattr(controller, name)
        assert len(values) == high_water, name
        values[controller.focusedAgent - 1]
