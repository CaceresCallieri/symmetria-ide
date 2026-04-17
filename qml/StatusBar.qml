// Native status bar.
// Replaces NeoVim's lualine — `runtime/init.lua` sets `laststatus=0`
// and emits structured capsules (mode, project, branch, file, pos)
// which map to properties on `statusState`. QML bindings do the rest.

import QtQuick
import QtQuick.Layouts

Rectangle {
    id: root
    // Symmetria Shell matte-pill color. Derived from
    // `~/.config/quickshell/symmetria/services/Colours.qml` mattePill()
    // at intensity 0.5 with m3surfaceContainerHigh as base. The editor
    // above paints transparent against the wallpaper, but the status bar
    // stays opaque — it's chrome, not content, and reads as a solid
    // surface matching the shell's pills. Keep in sync with
    // CommandLine.qml's `bgColor`.
    color: "#252323"

    // Hairline divider between editor and status bar — white at 12%
    // alpha, matching the matte pill's border treatment in the shell.
    Rectangle {
        width: parent.width
        height: 1
        color: "#1fffffff"
        anchors.top: parent.top
    }

    property color colorDim: "#7a7a7a"
    property color colorNormal: "#b0b0b0"
    property color colorStrong: "#e0e0e0"
    property color colorAccent: "#c8a37a"
    property string monoFont: "Iosevka, JetBrains Mono, monospace"

    RowLayout {
        anchors.fill: parent
        anchors.leftMargin: 12
        anchors.rightMargin: 12
        spacing: 12

        // Mode badge — colored block like lualine's mode indicator.
        Rectangle {
            visible: statusState.mode !== ""
            Layout.alignment: Qt.AlignVCenter
            Layout.preferredHeight: 22
            Layout.preferredWidth: modeLabel.implicitWidth + 18
            radius: height / 2
            color: {
                switch (statusState.mode) {
                    case "INSERT": return "#6a9955"
                    case "VISUAL":
                    case "V-LINE":
                    case "V-BLOCK": return "#c586c0"
                    case "REPLACE": return "#d16969"
                    case "COMMAND": return "#dcdcaa"
                    case "TERMINAL": return "#9cdcfe"
                    default: return "#4e8cb3"
                }
            }
            Text {
                id: modeLabel
                anchors.centerIn: parent
                text: statusState.mode
                color: "#111111"
                font.family: root.monoFont
                font.pixelSize: 11
                font.weight: Font.Bold
                font.letterSpacing: 0.8
            }
        }

        // Project name.
        Text {
            visible: statusState.project !== ""
            text: statusState.project
            color: root.colorDim
            font.family: root.monoFont
            font.pixelSize: 13
            Layout.alignment: Qt.AlignVCenter
        }

        // Branch — prefixed with a git-like glyph.
        Row {
            visible: statusState.branch !== ""
            spacing: 4
            Layout.alignment: Qt.AlignVCenter
            Text {
                text: "\u2387"   // ⎇ branch glyph
                color: root.colorAccent
                font.family: root.monoFont
                font.pixelSize: 13
            }
            Text {
                text: statusState.branch
                color: root.colorNormal
                font.family: root.monoFont
                font.pixelSize: 13
            }
        }

        // File path (relative to cwd where possible).
        Text {
            text: statusState.file
            color: root.colorStrong
            font.family: root.monoFont
            font.pixelSize: 13
            font.weight: Font.Medium
            Layout.alignment: Qt.AlignVCenter
            Layout.fillWidth: true
            elide: Text.ElideMiddle
        }

        // Right-aligned cursor position.
        Text {
            visible: statusState.position !== ""
            text: statusState.position
            color: root.colorDim
            font.family: root.monoFont
            font.pixelSize: 12
            Layout.alignment: Qt.AlignVCenter
        }
    }
}
