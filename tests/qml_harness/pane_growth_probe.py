"""Fresh-process Qt Quick probe for AgentPaneSlotModel delegate lifetime."""

from __future__ import annotations

import importlib
import json
import os
import tempfile
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlComponent, QQmlEngine

REPO_ROOT = Path(__file__).resolve().parents[2]


class _Bridge:
    def notify_spawn(self, _instance: dict) -> None:
        pass

    def notify_remove(self, _slot: int) -> None:
        pass

    def notify_focus(self, _slot: int) -> None:
        pass


def _pane_model(controller, model_type):
    for name in ("agentPaneSlots", "agentPaneSlotModel", "_agent_pane_slots"):
        candidate = getattr(controller, name, None)
        if isinstance(candidate, model_type):
            return candidate
    raise RuntimeError("AppController does not own/expose an AgentPaneSlotModel")


def main() -> int:
    isolated = tempfile.mkdtemp(prefix="symmetria-pane-growth-")
    for variable in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR"):
        os.environ[variable] = isolated
    os.environ["SYMMETRIA_IDE_USAGE_POLL"] = "0"
    os.environ.pop("SYMMETRIA_IDE_AGENT_TMUX", None)

    pane_module = importlib.import_module("symmetria_ide.agent_pane_slots")
    app_module = importlib.import_module("symmetria_ide.app")
    app_module.shutil.which = lambda executable: f"/bin/{executable}"

    app = QGuiApplication([])
    controller = app_module.AppController()
    controller._agent_bridge = _Bridge()
    for _ in range(3):
        controller.spawn_agent("fresh", True, "claude")
    model = _pane_model(controller, pane_module.AgentPaneSlotModel)

    engine = QQmlEngine()
    engine.rootContext().setContextProperty("paneSlots", model)
    component = QQmlComponent(engine)
    component.setData(
        b"""
import QtQuick
Item {
    id: root
    property alias delegateCount: panes.count
    property int destroyedInitial: 0
    Repeater {
        id: panes
        model: paneSlots
        delegate: Item {
            required property int slot
            Component.onDestruction: {
                if (slot <= 3)
                    root.destroyedInitial += 1
            }
        }
    }
}
""",
        # setData still supplies the source; a local-file base URL keeps Qt's
        # type loader synchronous instead of routing an unknown scheme through
        # the network loader and leaving the component in Status.Loading.
        QUrl.fromLocalFile(str(REPO_ROOT / "tests" / "PaneGrowth.qml")),
    )
    root = component.create()
    if root is None:
        errors = "\n".join(error.toString() for error in component.errors())
        raise RuntimeError(errors or f"component status {component.status()}")
    app.processEvents()
    before = int(root.property("delegateCount"))

    for _ in range(6):
        controller.spawn_agent("fresh", True, "claude")
        app.processEvents()

    result = {
        "before": before,
        "after": int(root.property("delegateCount")),
        "destroyed_initial": int(root.property("destroyedInitial")),
    }
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
