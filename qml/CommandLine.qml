// Native cmdline + wildmenu overlay.
//
// Floats above NvimView (parented to it in Main.qml) at ~20% from the
// top, horizontally centered, width ~60% of viewport. Visibility is
// driven entirely by cmdlineState.visible and popupmenuModel.visible —
// no mouse/focus interaction; keys always flow to the underlying
// NvimView and NeoVim owns the cmdline state.
//
// The autocomplete list uses ListView.isCurrentItem for selected-row
// highlight (bound via `currentIndex: popupmenuModel.selected`). Only
// the two affected delegates re-paint on selection change.

import QtQuick

Item {
    id: root
    anchors.fill: parent
    // Overlay shows whenever the cmdline is open. The popup slot tracks
    // `completionModel` (our own getcompletion-driven pipeline) rather
    // than the ext_popupmenu model so completions appear here regardless
    // of user plugin config (nvim-cmp, wilder, noice, etc.).
    visible: cmdlineState.visible
    z: 100

    // Palette — minimal Symmetria-aesthetic dark surfaces.
    property color bgColor: "#ee111418"
    property color borderColor: "#2f3540"
    property color firstCharColor: "#c8a37a"
    property color textColor: "#e8e8e8"
    property color cursorColor: "#e8e8e8"
    property color popupBgColor: "#f00e1116"
    property color popupBorderColor: "#2a303a"
    property color popupSelBgColor: "#233245"
    property color popupSelFgColor: "#f5f5f5"
    property color popupFgColor: "#b0b0b0"
    property color popupKindColor: "#6a7280"
    property string monoFont: "Iosevka, JetBrains Mono, monospace"
    property int fontSize: 14
    property int rowHeight: 24
    property int maxPopupRows: 10

    // Cmdline + popup stacked together so the popup drops directly
    // beneath the input line.
    Column {
        id: stack
        width: Math.min(900, Math.max(420, root.width * 0.6))
        anchors.horizontalCenter: parent.horizontalCenter
        y: Math.round(parent.height * 0.20)
        spacing: 6

        Rectangle {
            id: cmdBox
            visible: cmdlineState.visible
            width: parent.width
            height: Math.max(42, cmdRow.implicitHeight + 18)
            radius: 6
            color: root.bgColor
            border.color: root.borderColor
            border.width: 1

            Row {
                id: cmdRow
                anchors.fill: parent
                anchors.leftMargin: 14
                anchors.rightMargin: 14
                anchors.topMargin: 9
                anchors.bottomMargin: 9
                spacing: 4

                // `input("Name: ")` prompt shows before firstchar.
                Text {
                    visible: cmdlineState.prompt !== ""
                    text: cmdlineState.prompt
                    color: root.textColor
                    font.family: root.monoFont
                    font.pixelSize: root.fontSize
                    anchors.verticalCenter: parent.verticalCenter
                }

                // Firstchar glyph — `:`, `/`, `?`, `=`. Accent color so
                // the mode of the cmdline reads at a glance.
                Text {
                    visible: cmdlineState.firstchar !== ""
                    text: cmdlineState.firstchar
                    color: root.firstCharColor
                    font.family: root.monoFont
                    font.pixelSize: root.fontSize
                    font.weight: Font.Bold
                    anchors.verticalCenter: parent.verticalCenter
                    rightPadding: 4
                }

                // Text before cursor.
                Text {
                    id: textBefore
                    text: cmdlineState.text.substring(0, cmdlineState.cursorPos)
                    color: root.textColor
                    font.family: root.monoFont
                    font.pixelSize: root.fontSize
                    anchors.verticalCenter: parent.verticalCenter
                }

                // 2px bar cursor. Simpler than block-over-char and avoids
                // having to measure the single character at cursorPos.
                Rectangle {
                    width: 2
                    height: root.fontSize + 4
                    color: root.cursorColor
                    anchors.verticalCenter: textBefore.verticalCenter
                }

                // Text after cursor.
                Text {
                    text: cmdlineState.text.substring(cmdlineState.cursorPos)
                    color: root.textColor
                    font.family: root.monoFont
                    font.pixelSize: root.fontSize
                    anchors.verticalCenter: parent.verticalCenter
                }
            }
        }

        Rectangle {
            id: popupBox
            // popupList.count is reactive (unlike calling a model method
            // in a binding, which is the gotcha documented in CLAUDE.md).
            // Scoped to `cmdlineState.visible` so the popup disappears
            // when the cmdline closes even if the model still has items.
            visible: cmdlineState.visible && completionModel.visible && popupList.count > 0
            width: parent.width
            height: Math.min(root.maxPopupRows, popupList.count) * root.rowHeight + 12
            radius: 6
            color: root.popupBgColor
            border.color: root.popupBorderColor
            border.width: 1
            clip: true

            ListView {
                id: popupList
                anchors.fill: parent
                anchors.margins: 6
                model: completionModel
                // Selection comes from the Lua runtime via wildmenumode()
                // detection: during Tab cycling the list stays stable and
                // `completionModel.selected` points at the cycled row so
                // the delegate can highlight it. Outside of cycling the
                // value is -1 and nothing is highlighted.
                currentIndex: completionModel.selected
                interactive: false

                delegate: Rectangle {
                    width: popupList.width
                    height: root.rowHeight
                    color: ListView.isCurrentItem ? root.popupSelBgColor : "transparent"
                    radius: 3

                    Text {
                        anchors.fill: parent
                        anchors.leftMargin: 10
                        anchors.rightMargin: 10
                        verticalAlignment: Text.AlignVCenter
                        text: model.word
                        color: ListView.isCurrentItem ? root.popupSelFgColor : root.popupFgColor
                        font.family: root.monoFont
                        font.pixelSize: root.fontSize - 1
                        elide: Text.ElideRight
                    }
                }
            }
        }
    }
}
