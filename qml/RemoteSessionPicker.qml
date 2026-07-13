// Keyboard-first VPS tmux-session picker — the ATTACH path of the location
// toggle. Lists the paired server's live tmux sessions on the shared socket
// (the same ones the Vigilia phone app sees), filtered to this project's
// remote repo; picking one attaches it into a pool slot (`tmux new -A` with
// no command = pure attach), which is how a phone-started Claude session is
// resumed at the desk.
//
// Flow: open(dangerous) → controller.request_remote_tmux_sessions() (async
// ssh worker) → onRemoteTmuxSessionsReady fills the list → j/k/arrows move,
// Enter attaches, Esc cancels. Structural clone of AgentSessionPicker.qml
// (the opencode resume picker) — same states, same modal contract; only the
// fetch slot, ready signal, and accept call differ.
//
// The scrim + centered panel + FM scale-pop entrance + keyboard-modal
// focus-self-heal all come from ModalOverlay (this is the root element);
// see that file. Placeholder-discipline styling otherwise (minimal panel,
// Theme tokens only).

import QtQuick

import "design"

ModalOverlay {
    id: root

    panelWidth: 420

    // Polarity chosen with the case of the `a` keypress in the spawn menu.
    // Recorded on the slot for bookkeeping symmetry — the claude already
    // running inside the attached session keeps its own permission mode.
    property bool dangerous: true
    // "loading" | "ready" | "empty" | "error"
    property string state_: "loading"
    property var sessions: []
    property int selectedIndex: 0

    // Override the base open() to (re)fetch the session list. reassert()
    // (Main.qml's modal guard on window re-activation) is INHERITED — it
    // must NOT re-fetch, only re-grab focus, which the base _show() does.
    function open(dangerous) {
        root.dangerous = dangerous;
        root.sessions = [];
        root.selectedIndex = 0;
        root.state_ = "loading";
        _show();
        controller.request_remote_tmux_sessions();
    }

    function _accept() {
        if (root.state_ !== "ready" || root.sessions.length === 0)
            return;
        var session = root.sessions[root.selectedIndex];
        root.visible = false;
        controller.attach_remote_session(session.name, root.dangerous);
        // Always run the dismissal focus restore: if attach no-ops (pool
        // filled during the fetch, pairing lost), focus would otherwise be
        // orphaned with the modal gone. On the happy path this is
        // idempotent with the attach's own focus handoff.
        root.dismissed();
    }

    function _move(delta) {
        if (root.sessions.length === 0)
            return;
        var next = root.selectedIndex + delta;
        root.selectedIndex = Math.max(0, Math.min(root.sessions.length - 1, next));
        sessionList.positionViewAtIndex(root.selectedIndex, ListView.Contain);
    }

    // Esc is handled by ModalOverlay (→ dismiss); the rest lands here
    // already accepted (the modal swallows it).
    onKeyPressed: function (event) {
        switch (event.key) {
        case Qt.Key_J:
        case Qt.Key_Down:
            root._move(1);
            break;
        case Qt.Key_K:
        case Qt.Key_Up:
            root._move(-1);
            break;
        case Qt.Key_Return:
        case Qt.Key_Enter:
            root._accept();
            break;
        default:
            // Swallow everything else — modal.
            break;
        }
    }

    // Non-visual: lands in the content Column's data but is ignored for
    // layout (Column only positions Item children).
    Connections {
        target: controller
        function onRemoteTmuxSessionsReady(payload) {
            // Stale-result guard: a result landing after Esc must not
            // resurrect the overlay's state.
            if (!root.visible)
                return;
            root.sessions = payload.sessions;
            root.selectedIndex = 0;
            root.state_ = !payload.ok ? "error"
                        : payload.sessions.length === 0 ? "empty"
                        : "ready";
        }
    }

    // ---- Panel content (dropped into ModalOverlay's content Column) ----

    Text {
        text: "Attach VPS session — " + controller.vpsServerName
              + (root.dangerous ? "  ⚠" : "")
        color: Theme.color.text.strong
        font.family: Theme.font.family
        font.pixelSize: Theme.font.size.sm
        font.weight: Theme.font.weight.bold
        renderType: Text.NativeRendering
    }

    Text {
        visible: root.state_ !== "ready"
        text: root.state_ === "loading" ? "Listing remote sessions…"
            : root.state_ === "empty" ? "No unattached sessions for this project"
            : "Could not list sessions (see log)"
        color: Theme.color.text.dim
        font.family: Theme.font.family
        font.pixelSize: Theme.font.size.xs
        renderType: Text.NativeRendering
    }

    ListView {
        id: sessionList
        visible: root.state_ === "ready"
        width: parent.width
        // Cap the panel: ~10 rows, then scroll (positionViewAtIndex
        // keeps the selection in view).
        height: Math.min(contentHeight, 10 * (Theme.font.size.xs + Theme.spacing.sm))
        interactive: false
        model: root.sessions
        reuseItems: true

        delegate: Rectangle {
            id: sessionRow
            required property var modelData
            required property int index
            readonly property bool selected: index === root.selectedIndex

            width: sessionList.width
            height: rowText.implicitHeight + Theme.spacing.sm
            radius: Theme.radius.sm
            color: selected ? Theme.color.bg.selected : "transparent"

            Row {
                id: rowText
                anchors.verticalCenter: parent.verticalCenter
                anchors.left: parent.left
                anchors.leftMargin: Theme.spacing.sm
                anchors.right: parent.right
                anchors.rightMargin: Theme.spacing.sm
                spacing: Theme.spacing.sm

                Text {
                    width: rowText.width - whenText.implicitWidth - Theme.spacing.sm
                    text: sessionRow.modelData.name
                    elide: Text.ElideRight
                    color: sessionRow.selected
                           ? Theme.color.text.selected
                           : Theme.color.text.normal
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.xs
                    renderType: Text.NativeRendering
                }
                Text {
                    id: whenText
                    text: sessionRow.modelData.when
                    color: Theme.color.text.dim
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.xs
                    renderType: Text.NativeRendering
                }
            }
        }
    }

    Text {
        text: "j/k move · Enter attach · Esc cancel"
        color: Theme.color.text.dim
        font.family: Theme.font.family
        font.pixelSize: Theme.font.size.xs
        renderType: Text.NativeRendering
    }
}
