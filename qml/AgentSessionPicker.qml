// Keyboard-first OpenCode session picker — the resume path for the
// OpenCode harness. Claude needs no equivalent (its bare `-r` opens an
// interactive picker inside the terminal), but `opencode --session`
// requires an id, so the IDE lists the project's sessions itself —
// same approach as orchestrator.nvim's resume_picker, with QML as the
// selection UI instead of vim.ui.select.
//
// Flow: open(dangerous) → controller.request_opencode_sessions() (async
// worker; `opencode session list` takes ~1s) → onOpencodeSessionsReady
// fills the list → j/k/arrows move, Enter spawns
// `opencode --session <id>`, Esc cancels.
//
// Placeholder-discipline styling (minimal panel, Theme tokens only);
// richer treatment lands once the picker has real usage cadence.

import QtQuick

import "design"

Item {
    id: root

    anchors.fill: parent
    visible: false
    z: 40 // same modal layer as AgentSpawnMenu (never open simultaneously)

    // Polarity chosen with the case of the `r` keypress in the spawn
    // menu — carried through to the eventual spawn_agent call.
    property bool dangerous: true
    // "loading" | "ready" | "empty" | "error"
    property string state_: "loading"
    property var sessions: []
    property int selectedIndex: 0

    signal dismissed()

    function open(dangerous) {
        root.dangerous = dangerous;
        root.sessions = [];
        root.selectedIndex = 0;
        root.state_ = "loading";
        root.visible = true;
        keyCatcher.forceActiveFocus();
        controller.request_opencode_sessions();
    }

    function dismiss() {
        root.visible = false;
        root.dismissed();
    }

    // Idempotent focus re-assert for Main.qml's modal guard in
    // _restoreCentralFocus — open() is NOT idempotent (it re-fetches
    // the session list), so window re-activation uses this instead.
    function reassert() {
        keyCatcher.forceActiveFocus();
    }

    function _accept() {
        if (root.state_ !== "ready" || root.sessions.length === 0)
            return;
        var session = root.sessions[root.selectedIndex];
        root.visible = false;
        controller.spawn_agent("resume", root.dangerous, "opencode", session.id);
    }

    function _move(delta) {
        if (root.sessions.length === 0)
            return;
        var next = root.selectedIndex + delta;
        root.selectedIndex = Math.max(0, Math.min(root.sessions.length - 1, next));
        sessionList.positionViewAtIndex(root.selectedIndex, ListView.Contain);
    }

    Connections {
        target: controller
        function onOpencodeSessionsReady(payload) {
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

    // Dim the surface behind the panel so the modal state is legible.
    Rectangle {
        anchors.fill: parent
        color: Theme.color.bg.scrim
    }

    Rectangle {
        id: panel
        anchors.centerIn: parent
        width: 420
        implicitHeight: column.implicitHeight + Theme.spacing.lg * 2
        height: implicitHeight
        color: Theme.color.bg.chrome
        border.color: Theme.color.border.hairline
        border.width: 1
        radius: Theme.radius.md

        Column {
            id: column
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.margins: Theme.spacing.lg
            spacing: Theme.spacing.sm

            Text {
                text: "Resume OpenCode session"
                      + (root.dangerous ? "  ⚠" : "")
                color: Theme.color.text.strong
                font.family: Theme.font.family
                font.pixelSize: Theme.font.size.sm
                font.weight: Theme.font.weight.bold
                renderType: Text.NativeRendering
            }

            Text {
                visible: root.state_ !== "ready"
                text: root.state_ === "loading" ? "Loading sessions…"
                    : root.state_ === "empty" ? "No sessions found for this project"
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
                height: Math.min(contentHeight, 10 * (Theme.font.size.xs + Theme.spacing.sm * 2))
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
                            text: sessionRow.modelData.title
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
                text: "j/k move · Enter resume · Esc cancel"
                color: Theme.color.text.dim
                font.family: Theme.font.family
                font.pixelSize: Theme.font.size.xs
                renderType: Text.NativeRendering
            }
        }
    }

    Item {
        id: keyCatcher
        // Modal key routing + focus self-heal — same contract as
        // AgentSpawnMenu's keyCatcher (see the comment there).
        onActiveFocusChanged: {
            if (!activeFocus && root.visible)
                Qt.callLater(() => {
                    if (root.visible)
                        keyCatcher.forceActiveFocus();
                });
        }
        Keys.onPressed: function (event) {
            event.accepted = true;
            switch (event.key) {
            case Qt.Key_Escape:
                root.dismiss();
                break;
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
    }
}
