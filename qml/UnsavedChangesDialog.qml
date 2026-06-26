// 3-way unsaved-changes gate, shown before a close OR reload tears the editor
// down with modified buffers. Keyboard-first single-letter actions (like the
// spawn + MCP menus): s = save changes, d = discard changes, h/Esc = hold
// (abort the teardown, keep working). Built on ModalOverlay (scrim + scale-pop
// + focus self-heal). The host wires the three signals to the AppController
// teardown_* slots. Theme tokens only.
//
// Verb-neutral on purpose ("save"/"discard", not "save & close"): the same
// dialog serves both the close button and the Ctrl+Shift+R reload, so it must
// not promise one or the other.

import QtQuick

import "design"

ModalOverlay {
    id: root

    panelWidth: 440
    z: 50 // above the agent modals (z 40), like ConfirmDialog

    // Dirty buffer paths, set by open(); drives the file list + count.
    property var paths: []

    signal saveChanges()
    signal discardChanges()
    signal held()

    // Esc (handled by ModalOverlay) → dismiss → held: aborting the modal is
    // "hold and keep working".
    onDismissed: root.held()

    function open(dirtyPaths) {
        root.paths = dirtyPaths || [];
        _show();
    }

    onKeyPressed: function (event) {
        switch (event.key) {
        case Qt.Key_S:
            // Direct hide (not dismiss): saveChanges owns the teardown that
            // follows; dismiss would also fire held() → _restoreCentralFocus
            // and race it. Same split ConfirmDialog uses for confirm vs cancel.
            root.visible = false;
            root.saveChanges();
            break;
        case Qt.Key_D:
            root.visible = false;
            root.discardChanges();
            break;
        case Qt.Key_H:
            root.dismiss(); // → held
            break;
        default:
            break; // modal — swallow everything else
        }
    }

    // ---- Panel content (dropped into ModalOverlay's content Column) ----

    Text {
        anchors.horizontalCenter: parent.horizontalCenter
        text: "Unsaved changes"
        color: Theme.color.text.strong
        font.family: Theme.font.family
        font.pixelSize: Theme.font.size.sm
        font.weight: Theme.font.weight.bold
        renderType: Text.NativeRendering
    }

    Text {
        anchors.horizontalCenter: parent.horizontalCenter
        text: root.paths.length === 1
            ? "1 file has unsaved changes"
            : root.paths.length + " files have unsaved changes"
        color: Theme.color.text.dim
        font.family: Theme.font.family
        font.pixelSize: Theme.font.size.xs
        renderType: Text.NativeRendering
    }

    Item { width: 1; height: Theme.spacing.sm }

    // The dirty files, so the user knows what's at stake. Capped to a handful;
    // the count above is the full tally. ElideLeft keeps the basename visible.
    Column {
        anchors.left: parent.left
        anchors.right: parent.right
        spacing: 2

        Repeater {
            model: root.paths.slice(0, 6)
            delegate: Text {
                width: parent.width
                elide: Text.ElideLeft
                text: modelData
                color: Theme.color.text.normal
                font.family: Theme.font.family
                font.pixelSize: Theme.font.size.xs
                renderType: Text.NativeRendering
            }
        }
        Text {
            visible: root.paths.length > 6
            text: "… and " + (root.paths.length - 6) + " more"
            color: Theme.color.text.dim
            font.family: Theme.font.family
            font.pixelSize: Theme.font.size.xs
            renderType: Text.NativeRendering
        }
    }

    Item { width: 1; height: Theme.spacing.sm }

    // Single-letter action legend (keyboard-first, like the spawn/MCP menus).
    Column {
        spacing: 3

        Text {
            text: "s   save changes, then continue"
            color: Theme.color.text.normal
            font.family: Theme.font.family
            font.pixelSize: Theme.font.size.xs
            renderType: Text.NativeRendering
        }
        Text {
            text: "d   discard changes, then continue"
            color: Theme.color.text.normal
            font.family: Theme.font.family
            font.pixelSize: Theme.font.size.xs
            renderType: Text.NativeRendering
        }
        Text {
            text: "h   hold — keep working (Esc)"
            color: Theme.color.text.dim
            font.family: Theme.font.family
            font.pixelSize: Theme.font.size.xs
            renderType: Text.NativeRendering
        }
    }
}
