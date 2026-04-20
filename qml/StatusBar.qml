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
    // at intensity 0.3 (Colours.glass.subtle — the canonical value used
    // by the shell's bar pills: workspaces, tray, PillContainer) with
    // m3surfaceContainerHigh as the palette base. The editor above
    // paints transparent against the wallpaper, but the status bar stays
    // opaque — it's chrome, not content, and reads as a solid surface
    // matching the shell's pills exactly. Keep in sync with
    // CommandLine.qml's `bgColor` and WhichKeyOverlay.qml's `color`.
    color: "#201F1F"

    // Hairline divider between editor and status bar — white at 12%
    // alpha, matching the matte pill's border treatment in the shell.
    Rectangle {
        width: root.width
        height: 1
        color: "#1fffffff"
        anchors.top: root.top
    }

    property color colorDim: "#7a7a7a"
    property color colorNormal: "#b0b0b0"
    property color colorStrong: "#e0e0e0"
    property color colorAccent: "#c8a37a"
    // Font family — see CommandLine.qml for rationale. Bound to
    // `editorFontFamily` (Python context property) so grid + overlays
    // all pick the same family.
    property string monoFont: editorFontFamily

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
            // Mode badge colors drawn from the user's active wine_theme
            // colorscheme (`~/.config/nvim/.../wine_theme.lua`) so the
            // status bar reads as a continuation of the editor palette
            // instead of a generic terminal scheme.
            //   NORMAL   → keyword            (#C28B12)
            //   INSERT   → string             (#62BA46)
            //   VISUAL*  → term_bright_magenta(#D86DE9)
            //   REPLACE  → error_red          (#D2602D)
            //   COMMAND  → accent_blue        (#6D94E9)
            //   TERMINAL → term_bright_cyan   (#5BDFD8)
            color: {
                switch (statusState.mode) {
                    case "INSERT": return "#62BA46"
                    case "VISUAL":
                    case "V-LINE":
                    case "V-BLOCK": return "#D86DE9"
                    case "REPLACE": return "#D2602D"
                    case "COMMAND": return "#6D94E9"
                    case "TERMINAL": return "#5BDFD8"
                    default: return "#C28B12"
                }
            }
            Text {
                id: modeLabel
                anchors.centerIn: parent
                text: statusState.mode
                // wine_theme.bg_primary — the colorscheme's darkest tone,
                // so the badge label appears painted in the editor's own
                // background rather than an arbitrary near-black.
                color: "#131313"
                font.family: root.monoFont
                font.pixelSize: 11
                font.weight: Font.Bold
                font.letterSpacing: 0.8
                renderType: Text.NativeRendering
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
            renderType: Text.NativeRendering
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
                renderType: Text.NativeRendering
            }
            Text {
                text: statusState.branch
                color: root.colorNormal
                font.family: root.monoFont
                font.pixelSize: 13
                renderType: Text.NativeRendering
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
            renderType: Text.NativeRendering
        }

        // Right-aligned cursor position.
        Text {
            visible: statusState.position !== ""
            text: statusState.position
            color: root.colorDim
            font.family: root.monoFont
            font.pixelSize: 12
            Layout.alignment: Qt.AlignVCenter
            renderType: Text.NativeRendering
        }
    }
}
