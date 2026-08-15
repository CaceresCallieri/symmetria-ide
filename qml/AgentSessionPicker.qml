// Keyboard-first session picker — the resume path for the OpenCode harness.
// Claude and Pi need no equivalent (their bare `-r` opens an interactive
// picker inside the terminal), but `opencode --session` requires an id, so
// the IDE lists the project's sessions itself —
// same approach as orchestrator.nvim's resume_picker, with QML as the
// selection UI instead of vim.ui.select.
//
// ⚠ OpenCode-SPECIFIC, despite the harness-parameterised spawn below: the
// list comes from `controller.request_opencode_sessions()`, which answers
// from the thread index's cached OpenCode records and shells
// `opencode session list` only when that cache is cold or unusable. The
// harness rides through so the SPAWN is correct, not so the FETCH is general.
// A second `resumeRequiresId` harness therefore does NOT work here for free —
// it needs a harness-parameterised fetch on the controller (its own list
// command and parser) before this modal can serve it.
//
// Flow: open(harness, dangerous) → controller.request_opencode_sessions() →
// onOpencodeSessionsReady fills the list → j/k/arrows move, Enter spawns
// `opencode --session <id>`, Esc cancels. That handler can fire TWICE for one
// open: a cache hit answers synchronously (inside the call above — which is
// why `_show()` runs first, so the stale-result guard does not swallow it),
// and the refresh scan it forces re-emits when it lands. The cold path is a
// single async answer ~1s later.
//
// The scrim + centered panel + FM scale-pop entrance + keyboard-modal
// focus-self-heal all come from ModalOverlay (this is the root element);
// see that file. Placeholder-discipline styling otherwise (minimal panel,
// Theme tokens only); richer treatment lands once the picker has real
// usage cadence.

import QtQuick

import "design"

ModalOverlay {
    id: root

    panelWidth: 420

    // Polarity chosen with the case of the `r` keypress in the spawn
    // menu — carried through to the eventual spawn_agent call.
    property bool dangerous: true
    // The harness this picker is resuming — an open() ARGUMENT, so the
    // ordering ("set the harness, then raise") cannot be got wrong by a
    // caller: the raise sets it. It stays a property because it must survive
    // the inherited reassert() (a focus-only re-grab that must not re-fetch
    // or re-decide anything). OpenCode is the default only as the value a
    // caller that supplies none would get; see the header note on why a
    // future `resumeRequiresId` harness needs more than this property.
    property string harness: "opencode"
    // Display name for the title bar — the catalog's label ("OpenCode"),
    // falling back to the raw name if the harness is somehow unregistered.
    // The lookup itself lives on the controller (shared with AgentSpawnMenu);
    // only the fallback is this modal's own.
    readonly property string _harnessLabel: {
        var entry = controller.harness_menu_entry(root.harness);
        return entry ? entry.label : root.harness;
    }
    // "loading" | "ready" | "empty" | "error"
    property string state_: "loading"
    property var sessions: []
    property int selectedIndex: 0

    // Override the base open() to (re)fetch the session list. reassert()
    // (Main.qml's modal guard on window re-activation) is INHERITED — it
    // must NOT re-fetch, only re-grab focus, which the base _show() does.
    function open(harness, dangerous) {
        root.harness = harness;
        root.dangerous = dangerous;
        root.sessions = [];
        root.selectedIndex = 0;
        root.state_ = "loading";
        _show();
        controller.request_opencode_sessions();
    }

    function _accept() {
        if (root.state_ !== "ready" || root.sessions.length === 0)
            return;
        var session = root.sessions[root.selectedIndex];
        root.visible = false;
        controller.spawn_agent("resume", root.dangerous, root.harness, session.id);
        // Always run the dismissal focus restore: if spawn_agent no-ops
        // (pool filled during the ~1s list fetch, opencode vanished from
        // PATH), focus would otherwise be orphaned with the modal gone.
        // On the happy path this is idempotent with spawn_agent's own
        // focus handoff — _restoreCentralFocus re-asserts the same agent.
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

    // ---- Panel content (dropped into ModalOverlay's content Column) ----

    Text {
        text: "Resume " + root._harnessLabel + " session"
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
        // keeps the selection in view). The estimate mirrors the
        // delegate height: rowText implicitHeight (≈ font size) +
        // a single spacing.sm.
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
            color: selected ? Theme.color.bg.raisedSelected : "transparent"

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
