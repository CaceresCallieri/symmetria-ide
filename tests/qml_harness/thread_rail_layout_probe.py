"""Fresh-process geometry probe for the thread rail's two-line rows.

Reads the rendered geometry rather than the QML source, because every way the
two-line row can be wrong is silent: a binding loop leaves a warning nobody
reads, a zero-height line renders as a row that merely looks tight, and an
overlapping anchor chain produces text drawn on top of text. None of those
raise, and none of them fail a source-level assertion.

What it answers:

- do the rows still have a positive, uniform height, and did they actually
  grow when the second line landed;
- is line two BELOW line one rather than on top of it;
- does the age render across every row state and every duration branch
  (working, idle, just-replied, and no longer running), and does the
  worktree name render beside it;
- is the age right-aligned to the row's content edge.
"""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Property, QObject, Signal, Slot
from PySide6.QtGui import QGuiApplication
from PySide6.QtQuick import QQuickItem, QQuickView

from symmetria_ide.agent_thread_model import AgentThreadModel, ThreadRow

REPO_ROOT = Path(__file__).resolve().parents[2]

# A fixed "now" the durations are measured against, so the probe's expected
# strings do not depend on when it runs. Date.now() inside QML still reads the
# real clock, so the stamps below are offsets from the real now, computed once.
NOW_SEC = 1_760_000_000


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

    def __init__(self, now_sec: int) -> None:
        super().__init__()
        self._now = now_sec

    @Property(int, notify=focusedAgentChanged)
    def focusedAgent(self) -> int:
        return 1

    @Property("QVariantList", notify=agentActivityChanged)
    def agentActivity(self) -> list[dict[str, object]]:
        return [
            {"state": "working", "tool": "Edit", "agentType": "claude"},
            # Slot 2 is IDLE, which in production means no entry at all — the
            # clear path pops it. The list still has to carry a placeholder,
            # because it is indexed by slot.
            {"state": "", "tool": "", "agentType": "claude"},
            {"state": "", "tool": "", "agentType": "claude"},
        ]

    @Property("QVariantList", notify=agentActivityChanged)
    def agentTiming(self) -> list[dict[str, object]]:
        return [
            # Working for 22 minutes.
            {"busySince": self._now - 22 * 60, "idleSince": 0},
            # Last replied 3 hours ago.
            {"busySince": 0, "idleSince": self._now - 3 * 3600},
            # Replied 10 seconds ago — the sub-minute branch, which is a real
            # state (the agent has just answered) and the only one that does
            # not print a number.
            {"busySince": 0, "idleSince": self._now - 10},
        ]

    @Property("QVariantList", notify=agentTitlesChanged)
    def agentTitles(self) -> list[str]:
        return ["a live thread with a title long enough to elide", "second", "third"]

    @Property("QVariantList", notify=agentWorktreeChanged)
    def agentWorktree(self) -> list[str]:
        return ["feat-some-worktree", "", ""]

    @Property("QVariantList", notify=agentBrowserCountChanged)
    def agentBrowserCount(self) -> list[int]:
        return [1, 0, 0]

    @Property("QVariantList", notify=agentBrowserAttentionChanged)
    def agentBrowserAttention(self) -> list[bool]:
        return [False, False, False]

    @Property("QVariantList", notify=agentCoordAttentionChanged)
    def agentCoordAttention(self) -> list[bool]:
        return [False, False, False]

    @Property(int, notify=sttTargetSlotChanged)
    def sttTargetSlot(self) -> int:
        return 0

    @Property(bool, notify=sttTranscribingChanged)
    def sttTranscribing(self) -> bool:
        return False

    @Slot(int)
    def focus_agent(self, _slot: int) -> None:
        pass

    @Slot(int)
    def sleep_agent(self, _slot: int) -> None:
        pass

    @Slot(int)
    def focus_agent_browser(self, _slot: int) -> None:
        pass


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


def _delegates(root: QQuickItem) -> list[QQuickItem]:
    rows = [
        item
        for item in _visual_items(root)
        if _has_property(item, "slot") and _has_property(item, "sessionTitle")
    ]
    rows.sort(key=lambda item: item.property("index"))
    return rows


def _texts(delegate: QQuickItem) -> list[QQuickItem]:
    """Every VISIBLE Text in the delegate, so a hidden one cannot be matched.

    `visible` is checked rather than `opacity` because the row hides its
    worktree pair outright; an invisible Text still reports a real width, and
    matching one would make an absent worktree look present.
    """
    return [
        item
        for item in _visual_items(delegate)
        if _has_property(item, "text")
        and _has_property(item, "elide")
        and bool(item.property("visible"))
    ]


def _find_text(delegate: QQuickItem, wanted: str) -> QQuickItem | None:
    for item in _texts(delegate):
        if str(item.property("text")) == wanted:
            return item
    return None


def _describe(delegate: QQuickItem, age: str) -> dict:
    """Geometry facts about one row, in the DELEGATE's own coordinates."""
    title = _find_text(delegate, str(delegate.property("displayTitle")))
    age_label = _find_text(delegate, age)
    worktree = _find_text(delegate, str(delegate.property("worktreeName")))

    def box(item: QQuickItem | None) -> dict | None:
        if item is None:
            return None
        # The origin of the item's OWN coordinate system, mapped into the
        # delegate — which is the box's top-left there. Mapping `position()`
        # instead would be wrong: that is already a coordinate in the item's
        # PARENT, so it would be offset by one level of the chain.
        origin = item.mapToItem(delegate, item.boundingRect().topLeft())
        return {
            "x": round(origin.x(), 1),
            "y": round(origin.y(), 1),
            "w": round(item.width(), 1),
            "h": round(item.height(), 1),
        }

    return {
        "slot": delegate.property("slot"),
        # The delegate's own y in the ListView's content item, which is what
        # lets a caller compute the gap BETWEEN rows. Taken raw rather than
        # mapped, so a transform on an ancestor cannot skew it.
        "y": round(delegate.y(), 1),
        "height": round(delegate.height(), 1),
        "width": round(delegate.width(), 1),
        "working": bool(delegate.property("working")),
        "ageText": str(delegate.property("ageText")),
        "title": box(title),
        "age": box(age_label),
        "worktree": box(worktree),
    }


def main() -> int:
    app = QGuiApplication([])
    controller = Controller(NOW_SEC)
    model = AgentThreadModel()
    model.set_rows(
        [
            ThreadRow(
                thread_id="claude:live-busy",
                harness="claude",
                session_id="live-busy",
                title="",
                updated_at=0,
                work_root="",
                worktree="",
                slot=1,
            ),
            ThreadRow(
                thread_id="claude:live-idle",
                harness="claude",
                session_id="live-idle",
                title="",
                updated_at=0,
                work_root="",
                worktree="",
                slot=2,
            ),
            ThreadRow(
                thread_id="claude:live-fresh",
                harness="claude",
                session_id="live-fresh",
                title="",
                updated_at=0,
                work_root="",
                worktree="",
                slot=3,
            ),
            ThreadRow(
                thread_id="claude:dead",
                harness="claude",
                session_id="dead",
                title="a dead thread",
                updated_at=NOW_SEC - 2 * 86400,
                work_root="",
                worktree="dead-worktree",
                slot=0,
            ),
        ]
    )

    view = QQuickView()
    view.rootContext().setContextProperty("controller", controller)
    view.rootContext().setContextProperty("agentThreads", model)
    view.rootContext().setContextProperty("editorFontFamily", "monospace")
    view.setResizeMode(QQuickView.ResizeMode.SizeRootObjectToView)
    view.resize(220, 480)
    view.setSource((REPO_ROOT / "qml" / "AgentThreadRail.qml").resolve().as_uri())
    if view.status() != QQuickView.Status.Ready:
        errors = "\n".join(error.toString() for error in view.errors())
        raise RuntimeError(errors or f"view status {view.status()}")
    view.show()
    app.processEvents()

    root = view.rootObject()
    if not isinstance(root, QQuickItem):
        raise RuntimeError("thread rail did not create a QQuickItem root")
    # Pin the rail's clock so the expected duration strings are deterministic.
    root.setProperty("nowMs", float(NOW_SEC) * 1000.0)
    app.processEvents()

    rows = _delegates(root)
    expected_ages = ["22m", "3h", "now", "2d"]
    described = [
        _describe(delegate, age)
        for delegate, age in zip(rows, expected_ages, strict=True)
    ]
    print(json.dumps({"rows": described}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
