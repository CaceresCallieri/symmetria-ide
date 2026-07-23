// A collapsible section header for the Active Changes panel's per-repo change
// sections (the displayed repo + each foreign repo the focused agent changed).
// Chevron + optional repo glyph (foreign only) + repo label + per-repo file
// count. Emits `toggled` on click — the PARENT owns the collapse state (the
// displayed section stores it on the panel root; each foreign section on its
// Repeater delegate), so this stays a pure presentational leaf reused by both
// sites without a duplicated markup block.
import QtQuick
import QtQuick.Layouts
import "design"

MouseArea {
    id: header

    property bool collapsed: false
    // Foreign repos carry the repo glyph and a slightly quieter label tone; the
    // displayed repo is "home" — no glyph, stronger label ("you are here").
    property bool foreign: false
    property string label: ""
    property int count: 0

    signal toggled()

    Layout.fillWidth: true
    implicitHeight: row.implicitHeight
    cursorShape: Qt.PointingHandCursor
    onClicked: header.toggled()

    RowLayout {
        id: row
        anchors.fill: parent
        spacing: Theme.spacing.xxs

        Text {
            text: header.collapsed ? "▶" : "▼"
            color: Theme.color.text.dim
            font.family: Theme.font.family
            font.pixelSize: Theme.font.size.xs
        }
        Text {
            visible: header.foreign
            text: Theme.glyph.repo
            color: Theme.color.text.dim
            // Icon font — the chrome UI font may lack the glyph.
            font.family: editorFontFamily
            font.pixelSize: Theme.font.size.xs
        }
        Text {
            Layout.fillWidth: true
            text: header.label
            color: header.foreign
                ? Theme.color.text.normal
                : Theme.color.text.strong
            font.family: Theme.font.family
            font.pixelSize: Theme.font.size.xs
            font.weight: Theme.font.weight.medium
            elide: Text.ElideMiddle
        }
        Text {
            text: "· " + header.count
            color: Theme.color.text.dim
            font.family: Theme.font.family
            font.pixelSize: Theme.font.size.xs
        }
    }
}
