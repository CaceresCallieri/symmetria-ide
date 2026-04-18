// Application root window.
// Hosts the NvimView filling most of the window and the StatusBar at
// the bottom. No mouse-based interactions — focus always sits on the
// NvimView so keystrokes flow straight to NeoVim.

import QtQuick
import QtQuick.Window
import QtQuick.Layouts

import Symmetria.Ide 1.0

Window {
    id: root
    width: 1280
    height: 720
    visible: true
    title: "Symmetria IDE"
    // Transparent clear so the compositor shows the wallpaper through
    // the editor viewport (matches Ghostty + other transparent terminals
    // on Hyprland). The status bar and cmdline overlay are opaque —
    // they paint the Symmetria Shell matte-pill color (`#252323`) on
    // top. See StatusBar.qml / CommandLine.qml for the palette source.
    color: "transparent"
    minimumWidth: 800
    minimumHeight: 400

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        NvimView {
            id: editor
            Layout.fillWidth: true
            Layout.fillHeight: true
            backend: nvimBackend
            focus: true

            Component.onCompleted: forceActiveFocus()

            // Floating cmdline + wildmenu overlay — parented to the
            // editor so it clips within the viewport (not over the
            // status bar) and so its anchors.fill tracks editor resizes.
            // Focus stays on the NvimView; keys flow to NeoVim, which
            // emits ext_cmdline/ext_popupmenu events that this overlay
            // reads via cmdlineState / popupmenuModel.
            CommandLine {
                id: cmdlineOverlay
                anchors.fill: parent
            }

            // Native which-key overlay. Bottom-anchored inside the
            // editor so it visually sits above the status bar and
            // animates alongside editor resizes. Driven entirely by
            // `whichKeyState` + `whichKeyModel`; Lua side controls
            // show/hide via rpcnotify (see runtime/lua/orchestrator/
            // whichkey/init.lua).
            WhichKeyOverlay {
                id: whichKeyOverlay
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.bottom: parent.bottom
                // Clamp to half the viewport so huge menus never hog
                // the whole editor; scroll support is a v2 follow-up.
                height: Math.min(implicitHeight, parent.height * 0.5)
                z: 20
            }
        }

        StatusBar {
            id: statusBar
            Layout.fillWidth: true
            Layout.preferredHeight: 30
        }
    }

    // Ensure focus returns to the editor whenever the window regains it —
    // critical so the user never loses keystroke flow to NeoVim after
    // alt-tabbing away.
    onActiveChanged: if (active) editor.forceActiveFocus()
}
