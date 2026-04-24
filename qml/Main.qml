// Application root window.
// Hosts the NvimView filling most of the window and the StatusBar at
// the bottom. No mouse-based interactions — focus always sits on the
// NvimView so keystrokes flow straight to NeoVim.

import QtQuick
import QtQuick.Window
import QtQuick.Layouts

import Symmetria.Ide 1.0
import "design"

Window {
    id: root
    width: 1280
    height: 720
    visible: true
    title: "Symmetria IDE"
    // Transparent clear so the compositor shows the wallpaper through
    // the editor viewport (matches Ghostty + other transparent terminals
    // on Hyprland). The status bar and cmdline overlay are opaque —
    // they paint `Theme.color.bg.chrome` (Symmetria Shell matte-pill)
    // on top. See `qml/design/Theme.qml` for the palette source.
    color: "transparent"
    minimumWidth: 800
    minimumHeight: 400

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        // Editor / agent view swap. Full-window mode, not a side
        // panel: when `controller.agentVisible` is True the editor
        // hides entirely and the agent pane takes over the main
        // content area; the StatusBar stays visible across both
        // modes for visual continuity (project / branch / mode
        // read as relevant context in either view). Only one of
        // the two Items has `visible: true` at a time so Qt's
        // scene graph skips the hidden subtree entirely.
        Item {
            id: mainContent
            Layout.fillWidth: true
            Layout.fillHeight: true

            NvimView {
                id: editor
                anchors.fill: parent
                visible: !controller.agentVisible
                backend: nvimBackend
                focus: visible

                Component.onCompleted: forceActiveFocus()
                onVisibleChanged: if (visible) forceActiveFocus()

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
                    anchors.left: editor.left
                    anchors.right: editor.right
                    anchors.bottom: editor.bottom
                    // Clamp to half the viewport so huge menus never hog
                    // the whole editor; scroll support is a v2 follow-up.
                    height: Math.min(implicitHeight, editor.height * 0.5)
                    z: 20
                }
            }

            AgentPane {
                id: agentPane
                anchors.fill: parent
                visible: controller.agentVisible
            }
        }

        StatusBar {
            id: statusBar
            Layout.fillWidth: true
            Layout.preferredHeight: Theme.size.statusBarHeight
        }
    }

    // Focus handoff when the window regains activation: whichever
    // view is currently visible grabs focus, so alt-tabbing back
    // never leaves the user typing into a dead surface.
    onActiveChanged: {
        if (!active)
            return
        if (controller.agentVisible)
            agentPane.forceActiveFocus()
        else
            editor.forceActiveFocus()
    }
}
