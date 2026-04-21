// Native status bar.
// Replaces NeoVim's lualine — `runtime/init.lua` sets `laststatus=0`
// and emits structured capsules (mode, project, branch, file, pos)
// which map to properties on `statusState`. QML bindings do the rest.
//
// All color and typography values bind against the `Theme` singleton
// (`qml/design/Theme.qml`) — local palette/size literals belong in
// Theme, not here. See Theme.qml for the provenance of chrome colors
// (Symmetria Shell mattePill) and mode colors (wine_theme colorscheme).

import QtQuick
import QtQuick.Layouts

import "design"

Rectangle {
    id: root
    color: Theme.color.bg.chrome

    // Hairline divider between editor and status bar.
    Rectangle {
        width: root.width
        height: 1
        color: Theme.color.border.hairline
        anchors.top: root.top
    }

    // Wine_theme mode → color mapping (sources in Theme.color.mode):
    //   NORMAL   → keyword             (#C28B12)
    //   INSERT   → string              (#62BA46)
    //   VISUAL*  → term_bright_magenta (#D86DE9)
    //   REPLACE  → error_red           (#D2602D)
    //   COMMAND  → accent_blue         (#6D94E9)
    //   TERMINAL → term_bright_cyan    (#5BDFD8)

    RowLayout {
        anchors.fill: parent
        anchors.leftMargin: Theme.spacing.md
        anchors.rightMargin: Theme.spacing.md
        spacing: Theme.spacing.md

        // Mode badge — colored block like lualine's mode indicator.
        Rectangle {
            visible: statusState.mode !== ""
            Layout.alignment: Qt.AlignVCenter
            Layout.preferredHeight: Theme.size.modeBadgeHeight
            Layout.preferredWidth: modeLabel.implicitWidth + 16
            radius: height / 2
            color: {
                switch (statusState.mode) {
                    case "INSERT": return Theme.color.mode.insert
                    case "VISUAL":
                    case "V-LINE":
                    case "V-BLOCK": return Theme.color.mode.visual
                    case "REPLACE": return Theme.color.mode.replace
                    case "COMMAND": return Theme.color.mode.command
                    case "TERMINAL": return Theme.color.mode.terminal
                    default: return Theme.color.mode.normal   // NORMAL + any unrecognised mode (SELECT, S-LINE, etc.)
                }
            }
            Text {
                id: modeLabel
                anchors.centerIn: parent
                text: statusState.mode
                color: Theme.color.mode.badgeLabel
                font.family: Theme.font.family
                font.pixelSize: Theme.font.size.xs
                font.weight: Theme.font.weight.bold
                font.letterSpacing: 0.8
                renderType: Text.NativeRendering
            }
        }

        // Project name.
        Text {
            visible: statusState.project !== ""
            text: statusState.project
            color: Theme.color.text.dim
            font.family: Theme.font.family
            font.pixelSize: Theme.font.size.sm
            Layout.alignment: Qt.AlignVCenter
            renderType: Text.NativeRendering
        }

        // Branch — prefixed with a git-like glyph.
        Row {
            visible: statusState.branch !== ""
            spacing: Theme.spacing.xs
            Layout.alignment: Qt.AlignVCenter
            Text {
                text: "\u2387"   // ⎇ branch glyph
                color: Theme.color.accent.primary
                font.family: Theme.font.family
                font.pixelSize: Theme.font.size.sm
                renderType: Text.NativeRendering
            }
            Text {
                text: statusState.branch
                color: Theme.color.text.normal
                font.family: Theme.font.family
                font.pixelSize: Theme.font.size.sm
                renderType: Text.NativeRendering
            }
        }

        // File path (relative to cwd where possible).
        Text {
            text: statusState.file
            color: Theme.color.text.strong
            font.family: Theme.font.family
            font.pixelSize: Theme.font.size.sm
            font.weight: Theme.font.weight.medium
            Layout.alignment: Qt.AlignVCenter
            Layout.fillWidth: true
            elide: Text.ElideMiddle
            renderType: Text.NativeRendering
        }

        // Right-aligned cursor position.
        Text {
            visible: statusState.position !== ""
            text: statusState.position
            color: Theme.color.text.dim
            font.family: Theme.font.family
            font.pixelSize: Theme.font.size.sm
            Layout.alignment: Qt.AlignVCenter
            renderType: Text.NativeRendering
        }
    }
}
