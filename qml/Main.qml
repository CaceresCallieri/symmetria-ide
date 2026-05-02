// Application root window.
// Hosts the NvimView filling most of the window. Two chrome strips
// bracket the content area: AgentTopBar at the top (always-on agent
// dock — pool topology visible in editor mode AND agent mode so the
// user can see every running agent at all times) and StatusBar at
// the bottom (mode/file/branch/pos — replaces NeoVim's lualine).
// No mouse-based interactions — focus always sits on whichever pane
// is currently visible so keystrokes flow straight there.

import QtQuick
import QtQuick.Window
import QtQuick.Layouts

import Symmetria.Ide 1.0
import Symmetria.FileManager.UI as FmUi
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

        // Always-on agent dock at the top. Surfaces the multi-instance
        // pool topology (one bubble per slot, focused/active/empty
        // states) so the user can see every running agent regardless
        // of whether the editor or the agent pane is currently active.
        // Mirrors StatusBar's height + chrome at the bottom — together
        // they bracket the main content with matched chrome strips.
        AgentTopBar {
            id: agentTopBar
            Layout.fillWidth: true
            Layout.preferredHeight: Theme.size.statusBarHeight
        }

        // Editor / agent view swap. Full-window mode, not a side
        // panel: when `controller.agentVisible` is True the editor
        // hides entirely and the agent pane takes over the main
        // content area; the chrome bars (AgentTopBar above and
        // StatusBar below) stay visible across both modes for
        // visual continuity. Only one of the two inner Items has
        // `visible: true` at a time so Qt's scene graph skips the
        // hidden subtree entirely.
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

    // ------------------------------------------------------------------
    // File manager toggle-overlay.
    //
    // Imported as `FmUi` (alias) from Symmetria.FileManager.UI to avoid
    // singleton-name collision with the IDE's own Theme: both modules
    // export a `Theme`/`FmTheme` singleton, and the alias keeps the
    // FM's symbols in their own namespace so this file can mention
    // `Theme` (IDE) and `FmUi.FmTheme` (FM) without ambiguity.
    //
    // Loader.active gates instantiation so the panel doesn't pay
    // construction cost until the user first opens it. After that,
    // toggling visibility just flips the Item's `visible` flag — fast.
    Loader {
        id: fmOverlayLoader
        anchors.fill: parent
        z: 100
        // Once activated, stay loaded — re-instantiating the FileManager
        // tree each toggle would lose tab state, scroll position, and
        // selection. Visibility flag handles hide/show.
        active: controller.fmVisible || item !== null

        // Start picker mode when the overlay first opens. The panel reuses
        // its existing picker infrastructure (built for the XDG portal) as
        // a clean "select a file" affordance: confirming a selection emits
        // FileManagerService.pickerCompleted; cancelling emits
        // pickerCancelled. We connect to both below — no fifoPath is
        // passed, so the panel's standalone-host FIFO writer is dormant.
        onLoaded: {
            FmUi.FileManagerService.startPickerMode({
                title: "Open File",
                acceptLabel: "Open"
            })
        }

        // When the overlay closes (controller.fmVisible flips to false),
        // also clear picker mode so the panel returns to its idle state.
        // Without this, re-opening the overlay would pile a second
        // startPickerMode call on top of an already-active picker.
        Connections {
            target: controller
            function onFmVisibleChanged(): void {
                if (!controller.fmVisible
                        && fmOverlayLoader.item
                        && FmUi.FileManagerService.pickerMode) {
                    FmUi.FileManagerService.cancelPickerMode()
                }
                if (controller.fmVisible
                        && fmOverlayLoader.item
                        && !FmUi.FileManagerService.pickerMode) {
                    FmUi.FileManagerService.startPickerMode({
                        title: "Open File",
                        acceptLabel: "Open"
                    })
                }
            }
        }

        // Bridge picker completion → nvim :edit. The signal fires whether
        // the user pressed Enter on a file or the panel auto-completed
        // (e.g. Shift+Enter copy-then-confirm flow). controller.open_in_nvim
        // handles the fnameescape and dismisses the overlay itself.
        Connections {
            target: FmUi.FileManagerService
            function onPickerCompleted(fifoPath: string, paths: var): void {
                if (paths && paths.length > 0)
                    controller.open_in_nvim(paths[0])
                else
                    controller.hide_fm()
            }
            function onPickerCancelled(fifoPath: string): void {
                controller.hide_fm()
            }
        }

        sourceComponent: Item {
            id: fmOverlay
            anchors.fill: parent
            visible: controller.fmVisible
            focus: visible
            Component.onCompleted: forceActiveFocus()
            onVisibleChanged: if (visible) forceActiveFocus()

            Keys.onEscapePressed: event => {
                controller.hide_fm()
                event.accepted = true
            }

            // Dim scrim — clicking dismisses.
            Rectangle {
                anchors.fill: parent
                color: "#000000"
                opacity: 0.45

                Behavior on opacity {
                    NumberAnimation { duration: 120 }
                }

                MouseArea {
                    anchors.fill: parent
                    onClicked: controller.hide_fm()
                }
            }

            // Telescope-style centered panel.
            FmUi.FileManager {
                id: fmPanel
                anchors.centerIn: parent
                width: parent.width * 0.8
                height: parent.height * 0.8
                // initialPath flips between empty (overlay closed) and the
                // controller's resolved path (overlay open). Setting on
                // close clears panel state for the next open — see
                // controller.hide_fm which resets _fm_initial_path = "".
                initialPath: controller.fmInitialPath || ""
                onCloseRequested: controller.hide_fm()
            }
        }
    }

    // Focus handoff when the window regains activation: whichever
    // view is currently visible grabs focus, so alt-tabbing back
    // never leaves the user typing into a dead surface.
    onActiveChanged: {
        if (!active)
            return
        if (controller.fmVisible && fmOverlayLoader.item)
            fmOverlayLoader.item.forceActiveFocus()
        else if (controller.agentVisible)
            agentPane.forceActiveFocus()
        else
            editor.forceActiveFocus()
    }
}
