"""Out-of-process behavioural probe for AgentSpawnMenu.qml.

The main pytest process owns a session-scoped QCoreApplication, which cannot
be upgraded to the QGuiApplication required by Qt Quick.  Keeping this probe
in a subprocess also isolates Qt Quick teardown from the rest of the suite.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PySide6.QtCore import Property, QMetaObject, QObject, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtQml import QQmlComponent, QQmlEngine, QQmlExpression
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest

# Registry metadata as the real catalog projects it.  Availability is NOT in
# here — it is per-probe state, mirroring the production split between the
# compile-time registry and the PATH probe behind it.
_HARNESS_METADATA = (
    {
        "name": "pi",
        "label": "Pi",
        "menuKey": "P",
        "icon": "assets/pi-icon.svg",
        "resumeLabel": "resume (pi's picker)",
        "resumeRequiresId": False,
    },
    {
        "name": "claude",
        "label": "Claude",
        "menuKey": "C",
        "icon": "assets/claude-icon.svg",
        "resumeLabel": "resume (claude's picker)",
        "resumeRequiresId": False,
    },
    {
        "name": "opencode",
        "label": "OpenCode",
        "menuKey": "O",
        "icon": "assets/opencode-icon.svg",
        "resumeLabel": "resume (session picker)",
        "resumeRequiresId": True,
    },
)


class Controller(QObject):
    """Double for AppController's spawn-chooser surface.

    Mirrors the production property contract exactly, because the contract is
    part of what is under test: `agentHarnessCatalog` NOTIFIES rather than
    being constant, its `available` flags move only when
    `refresh_harness_availability()` re-reads PATH, and single-harness lookups
    go through `harness_menu_entry`.
    """

    locationChanged = Signal()
    agentHarnessCatalogChanged = Signal()
    opencodeSessionsReady = Signal("QVariant")

    def __init__(self) -> None:
        super().__init__()
        self._location = "local"
        # What "PATH" says right now, vs what the published catalog has picked
        # up — the gap a refresh closes.
        self._on_path = {"pi": True, "claude": True, "opencode": False}
        self._available = dict(self._on_path)
        self.refresh_calls = 0
        self.spawns: list[list[object]] = []
        self.events: list[str] = []
        self.session_requests = 0

    @Property(str, notify=locationChanged)
    def location(self) -> str:
        return self._location

    @Property("QVariantList", constant=True)
    def agentOrder(self) -> list[int]:
        return []

    @Property("QVariantList", notify=agentHarnessCatalogChanged)
    def agentHarnessCatalog(self) -> list[dict[str, object]]:
        return [
            {**row, "available": self._available[row["name"]]}
            for row in _HARNESS_METADATA
        ]

    @Slot()
    def refresh_harness_availability(self) -> None:
        self.refresh_calls += 1
        if self._on_path == self._available:
            return
        self._available = dict(self._on_path)
        self.agentHarnessCatalogChanged.emit()

    @Slot(str, result="QVariant")
    def harness_menu_entry(self, name: str) -> dict[str, object] | None:
        for row in self.agentHarnessCatalog:
            if row["name"] == name:
                return row
        return None

    def set_location(self, location: str) -> None:
        self._location = location
        self.locationChanged.emit()

    def set_opencode_on_path(self, present: bool) -> None:
        """Move PATH only — the catalog follows on the next refresh."""
        self._on_path["opencode"] = present

    @Slot(str, bool, str)
    @Slot(str, bool, str, str)
    def spawn_agent(
        self,
        spawn_type: str,
        dangerous: bool,
        harness: str,
        session_id: str = "",
    ) -> None:
        self.events.append("spawn")
        self.spawns.append([spawn_type, dangerous, harness, session_id])

    @Slot()
    def request_opencode_sessions(self) -> None:
        self.session_requests += 1


def _invoke(obj: QObject, method: str) -> None:
    if not QMetaObject.invokeMethod(obj, method):
        raise RuntimeError(f"could not invoke {method}")


def _evaluate(engine: QQmlEngine, scope: QObject, source: str) -> object:
    """Evaluate a QML call whose arguments cannot use invokeMethod cleanly."""
    expression = QQmlExpression(engine.rootContext(), scope, source)
    value = expression.evaluate()
    if expression.hasError():
        raise RuntimeError(expression.error().toString())
    return value


def _variant(value: object) -> object:
    """Turn a QJSValue property into the JSON-compatible value it represents."""
    to_variant = getattr(value, "toVariant", None)
    return to_variant() if to_variant is not None else value


def _key(window: QObject, key: Qt.Key, modifiers=Qt.KeyboardModifier.NoModifier):
    QTest.keyClick(window, key, modifiers)
    QGuiApplication.processEvents()


def _state(menu: QObject) -> dict[str, object]:
    return {
        "visible": bool(menu.property("visible")),
        "stage": menu.property("stage"),
        "harness": menu.property("harness"),
    }


def _visual_items(root: QQuickItem) -> list[QQuickItem]:
    """Every item in the VISUAL tree under `root`, delegates included.

    `QObject.findChildren` cannot see Repeater delegates: they carry
    JavaScript ownership and are attached with `setParentItem`, which sets the
    VISUAL parent and leaves the QObject parent null, so metaobject traversal
    walks straight past them.  `QQuickItem.childItems()` is the visual axis and
    does see them — measured, and the reason the menu's rows are read through
    here rather than through findChildren.
    """
    found: list[QQuickItem] = []
    stack = [root]
    while stack:
        item = stack.pop()
        found.append(item)
        stack.extend(item.childItems())
    return found


def _named(root: QQuickItem, object_name: str) -> QQuickItem | None:
    for item in _visual_items(root):
        if item.objectName() == object_name:
            return item
    return None


def _text_item(root: QQuickItem, text: str) -> QQuickItem | None:
    for item in _visual_items(root):
        if (
            item.metaObject().indexOfProperty("text") >= 0
            and item.property("text") == text
        ):
            return item
    return None


def _row_label(root: QQuickItem, prefix: str) -> QQuickItem | None:
    """The stage-0 label starting with `prefix`.

    Prefix rather than equality because an unavailable harness's row appends
    its reason ("OpenCode (not installed)") — the suffix is itself asserted, so
    the lookup cannot hardcode either form.
    """
    for item in _visual_items(root):
        value = (
            item.property("text")
            if item.metaObject().indexOfProperty("text") >= 0
            else None
        )
        if isinstance(value, str) and value.startswith(prefix):
            return item
    return None


def _effective_opacity(item: QObject, stop: QObject) -> float:
    opacity = 1.0
    current: QObject | None = item
    while current is not None and current is not stop:
        if current.metaObject().indexOfProperty("opacity") >= 0:
            opacity *= float(current.property("opacity"))
        parent_item = getattr(current, "parentItem", None)
        current = parent_item() if parent_item is not None else current.parent()
    return opacity


def _visual(item: QObject | None, stop: QObject) -> dict[str, object] | None:
    if item is None:
        return None
    color = item.property("color")
    color_name = (
        color.name(QColor.NameFormat.HexArgb) if isinstance(color, QColor) else ""
    )
    return {"opacity": _effective_opacity(item, stop), "color": color_name}


def _image_state(item: QQuickItem | None) -> dict[str, object] | None:
    if item is None:
        return None
    source = item.property("source")
    return {
        "visible": bool(item.property("visible")),
        "source": source.toString() if isinstance(source, QUrl) else str(source),
    }


def _header(menu: QQuickItem) -> dict[str, object]:
    title = _named(menu, "headerTitle")
    return {
        "title": title.property("text") if title is not None else None,
        "icon": _image_state(_named(menu, "headerIcon")),
    }


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    qml_dir_url = QUrl.fromLocalFile(str(repo_root / "qml")).toString()
    app = QGuiApplication(sys.argv)
    controller = Controller()
    engine = QQmlEngine()
    engine.rootContext().setContextProperty("controller", controller)
    engine.rootContext().setContextProperty("editorFontFamily", "monospace")

    source = f'''
import QtQuick
import QtQuick.Window
import "{qml_dir_url}" as Ide

Window {{
    width: 800
    height: 600
    visible: true
    Ide.AgentSpawnMenu {{
        objectName: "spawnMenu"
        anchors.fill: parent
    }}
    Ide.AgentSessionPicker {{
        objectName: "sessionPicker"
        anchors.fill: parent
    }}
}}
'''
    component = QQmlComponent(engine)
    component.setData(source.encode(), QUrl.fromLocalFile(str(repo_root / "tests")))
    window = component.create()
    if window is None:
        errors = "\n".join(error.toString() for error in component.errors())
        print(json.dumps({"load_error": errors}))
        return 1

    window.show()
    window.requestActivate()
    app.processEvents()
    menu = window.findChild(QQuickItem, "spawnMenu")
    picker = window.findChild(QQuickItem, "sessionPicker")
    if menu is None or picker is None:
        print(json.dumps({"load_error": "probe objects not found"}))
        return 1

    resume_signals: list[list[object]] = []
    attach_signals: list[list[object]] = []
    menu.resumePickerRequested.connect(lambda *args: resume_signals.append(list(args)))
    menu.attachPickerRequested.connect(lambda *args: attach_signals.append(list(args)))
    menu.dismissed.connect(lambda: controller.events.append("dismissed"))

    results: dict[str, object] = {}

    _invoke(menu, "open")
    results["open"] = _state(menu)
    results["header_stage0"] = _header(menu)
    pi_text = _row_label(menu, "Pi")
    opencode_text = _row_label(menu, "OpenCode")
    results["stage0_rows"] = {
        "pi": _visual(pi_text, menu),
        "opencode": _visual(opencode_text, menu),
    }
    results["stage0_labels"] = {
        "pi": pi_text.property("text") if pi_text is not None else None,
        "opencode": (
            opencode_text.property("text") if opencode_text is not None else None
        ),
    }
    _key(window, Qt.Key.Key_O)
    results["unavailable_opencode"] = _state(menu)

    # Install opencode "on PATH": the catalog must only pick it up through the
    # refresh the menu's own open() performs.
    controller.set_opencode_on_path(True)
    results["catalog_before_refresh"] = [
        row["available"] for row in controller.agentHarnessCatalog
    ]

    _invoke(menu, "open")
    results["catalog_after_refresh"] = [
        row["available"] for row in controller.agentHarnessCatalog
    ]
    _key(window, Qt.Key.Key_P)
    results["select_pi"] = _state(menu)
    results["header_stage1"] = _header(menu)
    results["pi_stage1"] = {
        "resume_label_present": _text_item(menu, "resume (pi's picker)") is not None,
    }
    controller.spawns.clear()
    _key(window, Qt.Key.Key_N)
    results["pi_new"] = list(controller.spawns)

    _invoke(menu, "open")
    _key(window, Qt.Key.Key_P)
    controller.spawns.clear()
    _key(window, Qt.Key.Key_N, Qt.KeyboardModifier.ShiftModifier)
    results["pi_new_safe"] = list(controller.spawns)

    _invoke(menu, "open")
    _key(window, Qt.Key.Key_P)
    _key(window, Qt.Key.Key_Escape)
    results["escape_stage1"] = _state(menu)
    _key(window, Qt.Key.Key_Escape)
    results["escape_stage0"] = _state(menu)

    _invoke(menu, "open")
    _key(window, Qt.Key.Key_P)
    _invoke(menu, "reassert")
    results["reassert"] = _state(menu)
    _invoke(menu, "open")
    results["reopen"] = _state(menu)

    _invoke(menu, "open")
    _key(window, Qt.Key.Key_P)
    controller.spawns.clear()
    resume_signals.clear()
    _key(window, Qt.Key.Key_R)
    results["pi_resume"] = {
        "spawns": list(controller.spawns),
        "signals": list(resume_signals),
    }

    _invoke(menu, "open")
    _key(window, Qt.Key.Key_C)
    results["select_claude"] = _state(menu)
    controller.spawns.clear()
    _key(window, Qt.Key.Key_C)
    results["claude_continue"] = list(controller.spawns)

    _invoke(menu, "open")
    _key(window, Qt.Key.Key_O)
    results["select_opencode"] = _state(menu)
    controller.spawns.clear()
    resume_signals.clear()
    _key(window, Qt.Key.Key_R)
    results["opencode_resume"] = {
        "spawns": list(controller.spawns),
        "signals": list(resume_signals),
    }

    # Location toggled WHILE the chooser is open: vps offers claude alone, so
    # the wizard must restart rather than keep a now-impossible selection.
    _invoke(menu, "open")
    _key(window, Qt.Key.Key_P)
    controller.set_location("vps")
    app.processEvents()
    results["vps_toggled_while_open"] = _state(menu)
    controller.set_location("local")
    app.processEvents()
    results["local_toggled_while_open"] = _state(menu)
    _key(window, Qt.Key.Key_Escape)

    controller.set_location("vps")
    _invoke(menu, "open")
    results["vps_open"] = _state(menu)
    attach_signals.clear()
    _key(window, Qt.Key.Key_A)
    results["vps_attach"] = list(attach_signals)
    controller.set_location("local")

    _invoke(menu, "open")
    _key(window, Qt.Key.Key_P)
    controller.events.clear()
    controller.spawns.clear()
    _key(window, Qt.Key.Key_N)
    results["spawn_focus_restore"] = {
        "events": list(controller.events),
        "spawns": list(controller.spawns),
        "visible": bool(menu.property("visible")),
    }

    _invoke(menu, "open")
    _key(window, Qt.Key.Key_P)
    _invoke(menu, "dismiss")
    results["direct_dismiss_stage1"] = _state(menu)

    controller.spawns.clear()
    picker.setProperty("harness", "opencode")
    picker.setProperty("dangerous", True)
    picker.setProperty("state_", "ready")
    picker.setProperty("sessions", [{"id": "stale_a"}, {"id": "stale_b"}])
    picker.setProperty("selectedIndex", 1)
    requests_before = controller.session_requests
    _evaluate(engine, picker, 'open("pi", false)')
    results["session_picker_open"] = {
        "harness": picker.property("harness"),
        "dangerous": picker.property("dangerous"),
        "state": picker.property("state_"),
        "sessions": _variant(picker.property("sessions")),
        "selected_index": picker.property("selectedIndex"),
        "session_requests": controller.session_requests - requests_before,
        "visible": bool(picker.property("visible")),
    }
    controller.opencodeSessionsReady.emit(
        {"ok": True, "sessions": [{"id": "ses_probe"}]}
    )
    app.processEvents()
    _invoke(picker, "_accept")
    picker_titles = [
        item.property("text")
        for item in _visual_items(picker)
        if item.metaObject().indexOfProperty("text") >= 0
        and isinstance(item.property("text"), str)
        and item.property("text").startswith("Resume ")
    ]
    results["session_picker"] = {
        "spawns": list(controller.spawns),
        "titles": picker_titles,
    }
    results["refresh_calls"] = controller.refresh_calls

    print(json.dumps(results, sort_keys=True))
    window.close()
    window.deleteLater()
    engine.deleteLater()
    app.processEvents()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
