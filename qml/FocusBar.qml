// FocusBar — per-pane focus indicator.
//
// A 1px vertical bar hugging one edge of the parent, shown when the tracked
// pane holds active focus. A single edge (rather than a full border envelope)
// keeps the indicator subtle and clear of the scrollbar. The bar is not a
// Layout item, so an enclosing ColumnLayout cannot resize it; anchors resolve
// against the parent pane's geometry.
//
// ⚠ THE ORIGINAL RULE WAS "flip `color`, NEVER geometry", because toggling
// width or visible costs a layout round-trip that is visibly janky mid-focus-
// transition. That reason still holds and the rule is still in force for
// geometry. The grow animation below does NOT break it: a `Scale` is a
// render-time transform applied by the scene graph, so it triggers no layout
// pass at all. Animating `height` or `width` would, and must not be tried.
//
// The colour is now CONSTANT and the scale does all the work. Driving it from
// colour as well would make the exit asymmetric — the bar would snap to
// transparent before its shrink could be seen.
import QtQuick

import "design"

Rectangle {
    id: bar

    // Bind to the pane's activeFocus (e.g. a FocusScope root) at the
    // instantiation site.
    required property bool focused

    // Which edge of the parent the bar hugs. The default suits the side
    // panels, which sit on the RIGHT of the window: their left edge is the one
    // facing the workspace. The thread rail sits on the opposite side and
    // passes `Qt.RightEdge` to get the same result, so on every pane the
    // indicator lands BETWEEN the pane and the workspace rather than against
    // the window frame. It also keeps the rail's bar clear of the per-row
    // selection marker, which is itself a left-edge line.
    property int edge: Qt.LeftEdge

    anchors.left: bar.edge === Qt.LeftEdge ? parent.left : undefined
    anchors.right: bar.edge === Qt.RightEdge ? parent.right : undefined
    anchors.top: parent.top
    anchors.bottom: parent.bottom
    width: 1
    color: Theme.color.accent.focus
    z: 50

    // Grows out from the middle when focus arrives and collapses back when it
    // leaves. A 1px hairline at 40% white appearing in one frame is easy to
    // miss; the motion is what makes it register at the edge of vision.
    transform: Scale {
        origin.x: 0
        origin.y: bar.height / 2
        yScale: bar.focused ? 1 : 0

        Behavior on yScale {
            NumberAnimation {
                duration: Theme.anim.quick
                easing.type: Easing.BezierSpline
                easing.bezierCurve: Theme.anim.standardCurve
            }
        }
    }
}
