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


def _lightness(colour) -> float | None:
    """A colour's lightness 0..1, or None when it is fully transparent.

    None rather than 0 for a transparent fill: an inactive row paints nothing
    and shows the rail behind it, so calling that "black" would let a caller
    compare it against a real colour and get a meaningless answer.
    """
    if colour is None or colour.alpha() == 0:
        return None
    return round(colour.lightnessF(), 4)


def _rail_ground(root: QQuickItem) -> float:
    """Lightness of the rail's own background — what an active row sits on.

    A DIRECT child of the root that fills it: the rail paints its ground as
    the first thing in the file. Delegates are Rectangles too, hence the
    direct-children walk rather than the recursive one.
    """
    for item in root.childItems():
        colour = item.property("color")
        if colour is not None and item.width() == root.width():
            lightness = _lightness(colour)
            if lightness is not None:
                return lightness
    raise RuntimeError("the rail drew no background rectangle")


def _edge_lightness(delegate: QQuickItem, shot) -> float | None:
    """Lightness of the pixel on the row's top edge, in the rendered image.

    Sampled at the horizontal MIDPOINT so it cannot land in a corner, where a
    rounded rectangle is antialiasing between the row and the rail whatever
    the border does. Returns None when the row is scrolled out of the grab.
    """
    top_left = delegate.mapToItem(None, delegate.boundingRect().topLeft())
    x = int(top_left.x() + delegate.width() / 2)
    y = int(top_left.y())
    if not (0 <= x < shot.width() and 0 <= y < shot.height()):
        return None
    return round(shot.pixelColor(x, y).lightnessF(), 4)


def _describe(delegate: QQuickItem, age: str, shot) -> dict:
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
        # The ONLY answer to "is an outline drawn" — see the border note above.
        "edgeLightness": _edge_lightness(delegate, shot),
        # The delegate's own y in the ListView's content item, which is what
        # lets a caller compute the gap BETWEEN rows. Taken raw rather than
        # mapped, so a transform on an ancestor cannot skew it.
        "y": round(delegate.y(), 1),
        "height": round(delegate.height(), 1),
        "width": round(delegate.width(), 1),
        "working": bool(delegate.property("working")),
        "focused": bool(delegate.property("focusedSlot")),
        "radius": round(delegate.property("radius"), 1),
        # ⚠ NO border properties are reported, and that is a finding rather
        # than an omission. Measured 2026-08-19 against a bare
        # `Rectangle { color: "red" }`: Qt Quick's defaults are
        # `border.width: 1` and `border.color: #000000` at full alpha — yet
        # nothing is drawn, because `QQuickPen` is only applied once it has
        # been ASSIGNED, and that valid/invalid state is not a property.
        #
        # So reading `border.width` cannot tell "no border" from "a 1px black
        # border", and a test asserting `width == 0` would fail on a Rectangle
        # that draws no border at all. The only thing that can answer is the
        # rendered pixel — see `edgeLightness` below.
        #
        # (Reading it at all needs QQmlProperty's DOTTED path. Plain
        # `property("border")` returns a `QQuickPen*`, a Qt C++ type PySide has
        # no converter for, and RAISES rather than returning None — the same
        # shape as the KSession case in CLAUDE.md.)
        "fill": _lightness(delegate.property("color")),
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

    # ONE grab for every row's edge sample: `grabWindow` is synchronous and
    # re-grabbing per row would be four full renders for four pixels.
    shot = view.grabWindow()

    rows = _delegates(root)
    expected_ages = ["22m", "3h", "now", "2d"]
    described = [
        _describe(delegate, age, shot)
        for delegate, age in zip(rows, expected_ages, strict=True)
    ]
    print(
        json.dumps(
            {"rows": described, "railGround": _rail_ground(root)}, sort_keys=True
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
