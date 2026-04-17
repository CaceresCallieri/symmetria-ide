// Native status bar.
// Renders one row of capsules from the model passed in as `capsules`.
// The capsule concept comes from orchestrator.nvim: a small, composable
// state indicator (buffer name, mode, LSP progress, etc.).
//
// This is deliberately minimal for Phase 0 — one capsule renders as a
// dim label and a brighter value. Aesthetic refinement happens after
// the spine is proved.

import QtQuick
import QtQuick.Layouts

Rectangle {
    id: root
    color: "#111111"

    required property var capsules

    // Thin divider above the bar — echoes Symmetria Shell's hairline
    // dividers without importing a shared style yet.
    Rectangle {
        width: parent.width
        height: 1
        color: "#2a2a2a"
        anchors.top: parent.top
    }

    RowLayout {
        anchors.fill: parent
        anchors.leftMargin: 12
        anchors.rightMargin: 12
        spacing: 16

        Text {
            text: "symmetria"
            color: "#5a5a5a"
            font.family: "Iosevka, JetBrains Mono, monospace"
            font.pixelSize: 12
            font.letterSpacing: 1.0
            Layout.alignment: Qt.AlignVCenter
        }

        Item {
            Layout.fillWidth: true
        }

        Repeater {
            model: root.capsules
            delegate: Row {
                spacing: 6
                Text {
                    text: model.label
                    color: "#6e6e6e"
                    font.family: "Iosevka, JetBrains Mono, monospace"
                    font.pixelSize: 13
                    font.letterSpacing: 0.3
                }
                Text {
                    text: model.value
                    color: "#e0e0e0"
                    font.family: "Iosevka, JetBrains Mono, monospace"
                    font.pixelSize: 13
                    font.weight: Font.Medium
                }
            }
        }
    }
}
