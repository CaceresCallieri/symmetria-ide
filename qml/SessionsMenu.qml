// Saved-session view (Ctrl+Shift+S). Plan A = one implicit session per
// project, so this shows THAT session — the agents that were running plus
// their titles, so you can locate the conversation — and offers to restore it.
// r / Enter → restore, Esc → close. Built on ModalOverlay; Theme tokens only.
//
// The visual "session available" indicator is a later polish; until then this
// view is the canonical way to discover + restore a saved session, and it
// states plainly when there is none.

import QtQuick

import "design"

ModalOverlay {
    id: root

    panelWidth: 480
    z: 50

    // Saved agent descriptors ({harness, title, session_id}), refreshed on
    // open() from the controller (which reads the on-disk manifest).
    property var agents: []
    property bool hasSession: false

    // Override open() to refresh the snapshot before raising (per-modal reset,
    // like AgentSessionPicker fetches its list).
    function open() {
        root.agents = controller.saved_session_agents();
        root.hasSession = controller.savedSessionAvailable;
        _show();
    }

    onKeyPressed: function (event) {
        switch (event.key) {
        case Qt.Key_R:
        case Qt.Key_Return:
        case Qt.Key_Enter:
            if (root.hasSession) {
                // restore_session rebuilds the workspace (it sets the central
                // surface itself); then dismiss returns focus to it.
                controller.restore_session();
                root.dismiss();
            }
            break;
        default:
            break; // modal — swallow everything else
        }
    }

    // ---- Panel content (dropped into ModalOverlay's content Column) ----

    Text {
        anchors.horizontalCenter: parent.horizontalCenter
        text: "Saved session"
        color: Theme.color.text.strong
        font.family: Theme.font.family
        font.pixelSize: Theme.font.size.sm
        font.weight: Theme.font.weight.bold
        renderType: Text.NativeRendering
    }

    Text {
        anchors.horizontalCenter: parent.horizontalCenter
        text: !root.hasSession
            ? "no saved session for this project"
            : (root.agents.length === 0
                ? "editor / browser only — no agents"
                : root.agents.length
                    + (root.agents.length === 1 ? " agent" : " agents"))
        color: Theme.color.text.dim
        font.family: Theme.font.family
        font.pixelSize: Theme.font.size.xs
        renderType: Text.NativeRendering
    }

    Item { width: 1; height: Theme.spacing.sm }

    // One row per saved agent: harness · title (so the user recognises which
    // conversation is which). Elides on the right to keep the panel tidy.
    Column {
        anchors.left: parent.left
        anchors.right: parent.right
        spacing: 4

        Repeater {
            model: root.agents
            delegate: Text {
                width: parent.width
                elide: Text.ElideRight
                text: (modelData.harness && modelData.harness.length
                        ? modelData.harness : "claude")
                    + "   ·   "
                    + (modelData.title && modelData.title.length
                        ? modelData.title : "(untitled)")
                color: Theme.color.text.normal
                font.family: Theme.font.family
                font.pixelSize: Theme.font.size.xs
                renderType: Text.NativeRendering
            }
        }
    }

    Item { width: 1; height: Theme.spacing.sm }

    Text {
        anchors.horizontalCenter: parent.horizontalCenter
        text: root.hasSession ? "r → restore     Esc → close" : "Esc → close"
        color: Theme.color.text.dim
        font.family: Theme.font.family
        font.pixelSize: Theme.font.size.xs
        renderType: Text.NativeRendering
    }
}
