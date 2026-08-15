"""Fresh-process behavioural probe for repaired thread-rail edge cases."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Property, QObject, Qt, Signal, Slot
from PySide6.QtGui import QGuiApplication
from PySide6.QtQuick import QQuickItem, QQuickView
from PySide6.QtTest import QTest

from symmetria_ide.agent_thread_model import AgentThreadModel, ThreadRow

REPO_ROOT = Path(__file__).resolve().parents[2]


class Controller(QObject):
    """Production-shaped controller surface used by AgentThreadRail.qml."""

    focusedAgentChanged = Signal()
    agentActivityChanged = Signal()
    agentTitlesChanged = Signal()
    agentWorktreeChanged = Signal()
    agentBrowserCountChanged = Signal()
    agentBrowserAttentionChanged = Signal()
    agentCoordAttentionChanged = Signal()
    sttTargetSlotChanged = Signal()
    sttTranscribingChanged = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.end_calls: list[int] = []
        self.focus_calls: list[int] = []
        self.focus_sink: QQuickItem | None = None

    @Property(int, notify=focusedAgentChanged)
    def focusedAgent(self) -> int:
        return 1

    @Property("QVariantList", notify=agentActivityChanged)
    def agentActivity(self) -> list[dict[str, object]]:
        return [{"state": "working", "tool": "Edit", "agentType": "claude"}]

    @Property("QVariantList", notify=agentTitlesChanged)
    def agentTitles(self) -> list[str]:
        return ["live title from pool"]

    @Property("QVariantList", notify=agentWorktreeChanged)
    def agentWorktree(self) -> list[str]:
        return ["live-worktree-from-pool"]

    @Property("QVariantList", notify=agentBrowserCountChanged)
    def agentBrowserCount(self) -> list[int]:
        return [1]

    @Property("QVariantList", notify=agentBrowserAttentionChanged)
    def agentBrowserAttention(self) -> list[bool]:
        return [True]

    @Property("QVariantList", notify=agentCoordAttentionChanged)
    def agentCoordAttention(self) -> list[bool]:
        return [True]

    @Property(int, notify=sttTargetSlotChanged)
    def sttTargetSlot(self) -> int:
        # Production uses zero for "no STT target"; it must not match a dead row.
        return 0

    @Property(bool, notify=sttTranscribingChanged)
    def sttTranscribing(self) -> bool:
        return False

    @Slot(int)
    def focus_agent(self, slot: int) -> None:
        self.focus_calls.append(slot)

    @Slot(int)
    def close_agent(self, slot: int) -> None:
        self._end_agent(slot)

    @Slot(int)
    def sleep_agent(self, slot: int) -> None:
        self._end_agent(slot)

    def _end_agent(self, slot: int) -> None:
        self.end_calls.append(slot)
        if self.focus_sink is not None:
            # Both the historical close path and the Phase-3 sleep path focus a
            # surviving terminal synchronously.  This probe guards the rail's
            # focus restoration independently of that intentional rename.
            self.focus_sink.forceActiveFocus()

    @Slot(int)
    def focus_agent_browser(self, _slot: int) -> None:
        pass

    @Slot(str, result="QVariant")
    def harness_menu_entry(self, _harness: str) -> dict[str, str]:
        return {"icon": ""}


def _visual_items(root: QQuickItem) -> list[QQuickItem]:
    found: list[QQuickItem] = []
    stack = [root]
    while stack:
        item = stack.pop()
        found.append(item)
        stack.extend(item.childItems())
    return found


def _has_property(item: QObject, name: str) -> bool:
    return item.metaObject().indexOfProperty(name) >= 0


def _delegate_for_slot(root: QQuickItem, slot: int) -> QQuickItem:
    for item in _visual_items(root):
        if (
            _has_property(item, "slot")
            and _has_property(item, "sessionTitle")
            and item.property("slot") == slot
        ):
            return item
    raise RuntimeError(f"no rendered delegate for slot {slot}")


def _stt_target(delegate: QQuickItem) -> bool:
    for item in _visual_items(delegate):
        if _has_property(item, "isSttTarget"):
            return bool(item.property("isSttTarget"))
    raise RuntimeError("delegate has no rendered AgentChip STT state")


def _display_number_visible(delegate: QQuickItem) -> bool:
    """Whether the delegate's dense Ctrl+N label is actually rendered."""
    expected = str(delegate.property("displayNumber"))
    matches = [
        item
        for item in _visual_items(delegate)
        if _has_property(item, "text") and str(item.property("text")) == expected
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"expected one display-number Text for {expected}, got {len(matches)}"
        )
    return bool(matches[0].property("visible"))


def main() -> int:
    app = QGuiApplication([])
    controller = Controller()
    model = AgentThreadModel()
    model.set_rows(
        [
            ThreadRow(
                thread_id="claude:live",
                harness="claude",
                session_id="live",
                title="stale live row title",
                updated_at=2,
                work_root="/project/live-worktree-from-pool",
                worktree="stale-live-worktree",
                slot=1,
            ),
            ThreadRow(
                thread_id="claude:dead",
                harness="claude",
                session_id="dead",
                title="durable dead title",
                updated_at=1,
                work_root="/project/durable-dead-worktree",
                worktree="durable-dead-worktree",
                slot=0,
            ),
        ]
    )

    view = QQuickView()
    view.rootContext().setContextProperty("controller", controller)
    view.rootContext().setContextProperty("agentThreads", model)
    view.rootContext().setContextProperty("editorFontFamily", "monospace")
    view.setResizeMode(QQuickView.ResizeMode.SizeRootObjectToView)
    view.resize(420, 420)
    view.setSource((REPO_ROOT / "qml" / "AgentThreadRail.qml").resolve().as_uri())
    if view.status() != QQuickView.Status.Ready:
        errors = "\n".join(error.toString() for error in view.errors())
        raise RuntimeError(errors or f"view status {view.status()}")
    view.show()
    app.processEvents()

    root = view.rootObject()
    if not isinstance(root, QQuickItem):
        raise RuntimeError("thread rail did not create a QQuickItem root")
    live = _delegate_for_slot(root, 1)
    dead = _delegate_for_slot(root, 0)

    sink = QQuickItem(root)
    sink.setParentItem(root)
    controller.focus_sink = sink

    root.forceActiveFocus()
    app.processEvents()
    QTest.keyClick(view, Qt.Key.Key_X)
    app.processEvents()
    app.processEvents()

    result = {
        "live_title": live.property("sessionTitle"),
        "live_worktree": live.property("worktreeName"),
        "dead_title": dead.property("sessionTitle"),
        "dead_worktree": dead.property("worktreeName"),
        "dead_stt_target": _stt_target(dead),
        "dead_browser_owned": bool(dead.property("ownsBrowser")),
        "dead_browser_attention": bool(dead.property("browserAttention")),
        "dead_coord_attention": bool(dead.property("coordAttention")),
        "live_number_visible": _display_number_visible(live),
        "dead_number_visible": _display_number_visible(dead),
        "end_calls": controller.end_calls,
        "rail_focus_after_end": bool(root.property("activeFocus")),
        "sink_focus_after_end": bool(sink.property("activeFocus")),
    }
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
