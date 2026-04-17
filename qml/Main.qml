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
    color: "#1a1a1a"
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
