// Native which-key overlay.
//
// Bottom-anchored panel (above the StatusBar, below the editor) that
// mirrors the user's reference layout: a grid of `<key> → [icon] desc`
// entries arranged into columns, with an ESC/⏎ footer. Data is entirely
// driven by `whichKeyModel` + `whichKeyState`; the emitter lives in
// `runtime/lua/orchestrator/whichkey/init.lua` and the model/state
// wiring is in `src/symmetria_ide/app.py`.
//
// Visibility is `whichKeyState.visible` — no timers, no mouse, no
// focus transfer. Keystrokes flow to NeoVim; Lua decides when to emit
// show/hide.
//
// Layout strategy: a fixed number of columns, items poured top-to-bottom
// within each column (column-major order, matching which-key's default).
// Column count = floor(width / DESIRED_COL_WIDTH). Row height fixed so
// the panel's implicit height is `rowsPerColumn * rowHeight + footer`.

import QtQuick

Rectangle {
    id: root

    // --- Palette — kept in lockstep with CommandLine.qml / StatusBar.qml
    // so overlay chrome stays visually consistent across components.
    color: "#252323"
    border.color: "#1fffffff"
    border.width: 1
    radius: 6

    property color keyColor: "#e0e0e0"
    property color arrowColor: "#c8a37a"
    property color leafColor: "#e8e8e8"
    property color groupColor: "#e8ab6f"   // amber — matches arrow accent
    property color dimColor: "#7a7a7a"
    property string monoFont: "Iosevka, JetBrains Mono, monospace"
    property int fontSize: 13
    property int rowHeight: 22
    property int desiredColWidth: 280
    property int horizontalPadding: 24
    property int verticalPadding: 14
    property int footerHeight: 28

    // --- Layout math. Kept as properties so bindings stay reactive.
    property int innerWidth: Math.max(0, width - 2 * horizontalPadding)
    property int columnCount: Math.max(1, Math.floor(innerWidth / desiredColWidth))
    property int columnWidth: columnCount > 0 ? Math.floor(innerWidth / columnCount) : innerWidth
    property int itemCount: whichKeyModel.rowCount()
    property int rowsPerColumn: Math.max(1, Math.ceil(itemCount / columnCount))

    // Panel's natural height — parent clamps to a max so huge menus
    // don't swallow the viewport. v1: no scroll; excess items are just
    // cut off by the clip at the content bottom. Scroll lands post-v1.
    implicitHeight: rowsPerColumn * rowHeight + footerHeight + 2 * verticalPadding

    visible: whichKeyState.visible
    clip: true

    // `whichKeyModel.rowCount()` is a function call, not a bindable
    // property. Re-seed `itemCount` via a Connection so the layout
    // rebinds when the model resets. See CLAUDE.md gotcha #3.
    Connections {
        target: whichKeyModel
        function onModelReset() {
            root.itemCount = whichKeyModel.rowCount()
        }
    }

    // --- Entry grid. Each delegate positions itself by index into a
    // column-major order: column = floor(i / rowsPerColumn), row = i % rowsPerColumn.
    Item {
        id: content
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.leftMargin: root.horizontalPadding
        anchors.rightMargin: root.horizontalPadding
        anchors.topMargin: root.verticalPadding
        height: root.rowsPerColumn * root.rowHeight

        Repeater {
            model: whichKeyModel
            delegate: Item {
                id: entry
                width: root.columnWidth
                height: root.rowHeight
                x: Math.floor(index / root.rowsPerColumn) * root.columnWidth
                y: (index % root.rowsPerColumn) * root.rowHeight

                Row {
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: 6

                    // Key label. Fixed minimum width so arrows align
                    // across rows within a column.
                    Text {
                        width: 22
                        horizontalAlignment: Text.AlignLeft
                        text: model.key
                        color: root.keyColor
                        font.family: root.monoFont
                        font.pixelSize: root.fontSize
                    }

                    Text {
                        text: "→"
                        color: root.arrowColor
                        font.family: root.monoFont
                        font.pixelSize: root.fontSize
                    }

                    // Icon slot. Absent → empty string, collapses the
                    // horizontal run so descriptions still align
                    // reasonably within the column.
                    Text {
                        visible: model.icon !== ""
                        text: model.icon
                        color: model.iconColor !== "" ? model.iconColor : root.leafColor
                        font.family: root.monoFont
                        font.pixelSize: root.fontSize
                    }

                    Text {
                        // Groups render with a leading `+` (matches
                        // which-key convention the user knows by sight).
                        text: (model.isGroup ? "+" : "") + model.desc
                        color: model.isGroup ? root.groupColor : root.leafColor
                        font.family: root.monoFont
                        font.pixelSize: root.fontSize
                        elide: Text.ElideRight
                        // Cap to remaining column width so long descriptions
                        // don't overflow into the next column.
                        width: Math.max(0, root.columnWidth - 60
                               - (model.icon !== "" ? 24 : 0))
                    }
                }
            }
        }
    }

    // --- Footer: ESC close / ⏎ back hints.
    Row {
        id: footer
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.bottom: parent.bottom
        anchors.bottomMargin: 6
        spacing: 18
        height: root.footerHeight

        Row {
            spacing: 5
            anchors.verticalCenter: parent.verticalCenter
            Text {
                text: "ESC"
                color: root.keyColor
                font.family: root.monoFont
                font.pixelSize: root.fontSize - 1
                font.weight: Font.Medium
            }
            Text {
                text: "close"
                color: root.dimColor
                font.family: root.monoFont
                font.pixelSize: root.fontSize - 1
            }
        }

        Row {
            visible: whichKeyState.canGoBack
            spacing: 5
            anchors.verticalCenter: parent.verticalCenter
            Text {
                text: "⏎"
                color: root.keyColor
                font.family: root.monoFont
                font.pixelSize: root.fontSize - 1
                font.weight: Font.Medium
            }
            Text {
                text: "back"
                color: root.dimColor
                font.family: root.monoFont
                font.pixelSize: root.fontSize - 1
            }
        }
    }
}
