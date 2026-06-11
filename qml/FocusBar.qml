// FocusBar — per-sub-pane focus indicator.
//
// A 1px vertical bar hugging the parent's left edge, shown when the
// tracked pane holds active focus. Left-edge-only (rather than a full
// border envelope) keeps the indicator subtle and clear of the
// scrollbar on the right edge. The focus flip lives on `color`
// (accent.focus ↔ transparent), never on width/visible — toggling
// geometry costs a layout round-trip that's visibly janky during
// focus transitions. The bar is not a Layout item, so an enclosing
// ColumnLayout cannot resize it; anchors resolve against the parent
// pane's geometry.
import QtQuick

import "design"

Rectangle {
    // Bind to the pane's activeFocus (e.g. a FocusScope root) at the
    // instantiation site.
    required property bool focused

    anchors.left: parent.left
    anchors.top: parent.top
    anchors.bottom: parent.bottom
    width: 1
    color: focused ? Theme.color.accent.focus : "transparent"
    z: 50
}
