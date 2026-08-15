// Thread rail — the left-hand index of this project's agent conversations.
//
// Replaces the chip strip that used to sit centered in AgentTopBar: a bar
// holds a handful of pills before it runs out of width, and the thing the user
// asked for is a LIST — every thread of the project, reachable at a glance,
// eventually including the ones whose CLI is no longer running.
//
// "Rail", not "sidebar": `sidebar` already names the right-hand file-tree
// panel throughout app.py and Main.qml (`treeVisible`,
// `SIDEBAR_MIN_WINDOW_WIDTH`), so reusing the word would make every one of
// those comments ambiguous. The user-facing word stays "sidebar".
//
// ⚠ THE ROW'S ONLY LINK TO A PANE IS THE INTEGER `slot`, and 0 means "no live
// pane" (a dead thread, from the sleep/resume phase). Everything else a live
// row shows — activity sparkle, worktree, browser ownership, coordination
// attention, STT — is read HERE from the per-slot `QVariantList` properties on
// the controller, indexed `slot - 1`, exactly as the pills read them. That is
// deliberate:
//
//   - it keeps the rail's coupling to the pool at one integer, so nothing in
//     this file can reach a KSession;
//   - it reuses wiring the pills already proved, rather than inventing a
//     second source of truth for activity that would drift the moment one of
//     those six signals changes shape.
//
// The cost is that EVERY such read must be guarded on `slot > 0` — a dead row
// indexing a per-slot list at -1 would read the last live agent's state. The
// guard lives on `live` below, and each indicator gates on it.
//
// Those properties are PySide `QVariantList`s: index then coerce, never
// `Array.isArray` (see .claude/memory/reference/qt-pyside/
// qml_qvariantlist_array_check.md).
//
// The model behind the list (`agentThreads`, agent_thread_model.py) is an
// ORDINARY list model — rows insert, move and leave. That is safe precisely
// because it is NOT the model behind the agent pane Repeater, which is
// append-only because each of its delegates owns a live agent CLI.

import QtQuick
import Symmetria.Agents.UI as AgentsUI

import "design"

FocusScope {
    id: root

    // Ctrl+H (the spatial-navigation block in Main.qml) calls
    // `forceActiveFocus()` on this scope directly. The ListView below carries
    // `focus: true`, so it is the scope's focus delegate and the grant lands
    // on its Keys.onPressed handler — verified in an offscreen probe rather
    // than assumed, since a FocusScope with no focused child swallows it.
    //
    // The single dispatch into the pool. `slot > 0` is the live/dead test;
    // resuming a dead thread arrives here in the sleep/resume phase.
    function activateThread(slot: int): void {
        if (slot > 0)
            controller.focus_agent(slot);
    }

    // ⚠ Through the MODEL, never through `threadList.currentItem.slot`.
    // Measured 2026-08-15 (offscreen probe, real AgentThreadModel): right
    // after a row is dropped, `currentIndex` is not yet clamped and
    // `currentItem` still points at the RECYCLED delegate of the removed row
    // (`reuseItems: true`) — Enter then focused the agent that had just gone
    // away. `slot_at` answers 0 for any out-of-range row, which is already
    // the "not live, do nothing" value.
    function _currentSlot(): int {
        return agentThreads.slot_at(threadList.currentIndex);
    }

    // Ending an agent from the rail must LEAVE the user in the rail.
    // `close_agent` refocuses a survivor, and `focus_agent` both swaps the
    // central surface to "agent" and lands keyboard focus in that agent's
    // terminal — so without this the first `x` throws you out of the column
    // you are working in, and a second `x` (meant for the next thread) is
    // typed as a literal `x` into the surviving agent's prompt.
    // `Qt.callLater` because the refocus has to run AFTER the focusAgentRequested
    // handler in Main.qml, not before it.
    function _endThread(slot: int): void {
        if (slot <= 0)
            return;
        controller.close_agent(slot);
        Qt.callLater(() => threadList.forceActiveFocus());
    }

    // Attention badge shared by the browser globe and the coordination dot —
    // one shape, two meanings, so the two cannot drift in size or cadence the
    // way the pills' hand-copied pair did.
    component AttentionDot: Rectangle {
        id: dot

        radius: width / 2
        border.width: 1
        border.color: Theme.color.bg.bar

        // `dot`, never `parent`: an Animation is not an Item, so `parent`
        // inside it does not resolve to the Rectangle it is declared in.
        SequentialAnimation {
            running: dot.visible
            loops: Animation.Infinite
            // alwaysRunToEnd so the badge never freezes mid-fade at a
            // half-opacity that reads as a rendering fault.
            alwaysRunToEnd: true
            NumberAnimation {
                target: dot
                property: "opacity"
                to: 0.45
                duration: Theme.anim.duration
                easing.type: Easing.InOutQuad
            }
            NumberAnimation {
                target: dot
                property: "opacity"
                to: 1.0
                duration: Theme.anim.duration
                easing.type: Easing.InOutQuad
            }
        }
    }

    Rectangle {
        anchors.fill: parent
        // Same chrome rung as the top bar and the file-tree column this rail
        // faces across the window; the central surface is the canvas rung.
        color: Theme.color.bg.bar
    }

    Text {
        id: railHeader
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.margins: Theme.spacing.md
        text: "THREADS"
        color: Theme.color.text.dim
        font.family: Theme.font.family
        font.pixelSize: Theme.font.size.xs
        font.weight: Theme.font.weight.medium
        font.letterSpacing: 0.8
        renderType: Text.NativeRendering
    }

    ListView {
        id: threadList

        anchors.top: railHeader.bottom
        anchors.topMargin: Theme.spacing.sm
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.leftMargin: Theme.spacing.sm
        anchors.rightMargin: Theme.spacing.sm
        clip: true
        focus: true
        spacing: Theme.spacing.xxs
        model: agentThreads
        reuseItems: true

        // Vim row navigation, in the same shape as the file tree's. Enter
        // activates; `x` ends the agent — it becomes an explicit sleep (the
        // thread survives, dead and resumable) in the sleep/resume phase.
        //
        // `currentIndex` is the rail's OWN selection and is deliberately NOT
        // `controller.focusedAgent`: moving down the list must not switch
        // panes, or browsing the list would cost a surface swap per keypress.
        // Enter is what commits. The delegate paints the selected row itself
        // (`selected`), so there is no `highlight` item to move.
        Keys.onPressed: event => {
            if (event.key === Qt.Key_J || event.key === Qt.Key_Down) {
                threadList.incrementCurrentIndex();
                event.accepted = true;
            } else if (event.key === Qt.Key_K || event.key === Qt.Key_Up) {
                threadList.decrementCurrentIndex();
                event.accepted = true;
            } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
                root.activateThread(root._currentSlot());
                event.accepted = true;
            } else if (event.key === Qt.Key_X) {
                root._endThread(root._currentSlot());
                event.accepted = true;
            }
        }

        delegate: Rectangle {
            id: row

            required property int index
            required property int slot
            required property string harness
            required property string title
            required property string worktree

            // The dead/live gate every per-slot read below depends on.
            readonly property bool live: row.slot > 0
            readonly property int displayNumber: row.index + 1
            readonly property bool selected: row.index === threadList.currentIndex
            readonly property bool focusedSlot: row.live && controller.focusedAgent === row.slot
            readonly property var activity: row.live ? controller.agentActivity[row.slot - 1] : null
            // Two fields read the LIVE per-slot list while the agent is
            // running and the row's own copy otherwise: a dead thread has no
            // slot to index, and its title and worktree are exactly what the
            // history has to remember about it. The live list stays the source
            // while there is one, so a running agent's title and worktree can
            // never lag the pool by a rebuild.
            readonly property string sessionTitle:
                row.live ? String(controller.agentTitles[row.slot - 1] || "") : row.title
            readonly property string worktreeName:
                row.live ? String(controller.agentWorktree[row.slot - 1] || "") : row.worktree
            readonly property bool ownsBrowser:
                row.live && (controller.agentBrowserCount[row.slot - 1] || 0) > 0
            readonly property bool browserAttention:
                row.live && !!(controller.agentBrowserAttention[row.slot - 1])
            readonly property bool coordAttention:
                row.live && !!(controller.agentCoordAttention[row.slot - 1])

            width: ListView.view.width
            height: Math.max(rowContent.implicitHeight, trailing.implicitHeight)
                + Theme.spacing.sm * 2
            radius: Theme.radius.sm
            color: row.selected ? Theme.color.bg.raisedSelected : "transparent"

            // Identity + title, filling everything the trailing cluster does
            // not claim. Anchored to `trailing.left` rather than to the row's
            // right edge so the title's elide point tracks how many
            // indicators are lit, with no width arithmetic to keep in sync.
            Row {
                id: rowContent
                anchors.verticalCenter: parent.verticalCenter
                anchors.left: parent.left
                anchors.right: trailing.left
                anchors.leftMargin: Theme.spacing.sm
                anchors.rightMargin: Theme.spacing.sm
                spacing: Theme.spacing.sm

                // Shared sparkle (Symmetria.Agents.UI) — the same element the
                // pills carried, driven by the same bridge-fed activity dict.
                AgentsUI.AgentChip {
                    anchors.verticalCenter: parent.verticalCenter
                    size: Theme.font.size.sm * 1.4
                    active: row.focusedSlot
                    activityState: row.activity ? row.activity.state : ""
                    activityTool: row.activity ? row.activity.tool : ""
                    // Falls back to the ROW's harness so a dead thread — which
                    // has no activity record at all — still reads as its own
                    // backend rather than as claude.
                    agentType: row.activity ? row.activity.agentType : row.harness
                    // `row.live &&` is load-bearing, not defensive: a dead
                    // row's slot is 0 and `sttTargetSlot` is 0 for "nobody is
                    // being dictated into", so an ungated compare lights EVERY
                    // dead row as the dictation target whenever no dictation
                    // is running.
                    isSttTarget: row.live && controller.sttTargetSlot === row.slot
                    sttIsTranscribing: controller.sttTranscribing
                }

                // Harness brand mark. New next to the pills, and the direct
                // answer to "did I talk to Claude or to OpenCode" — the thing
                // the rail exists to stop being invisible. Brand fills are
                // baked into the SVGs and intentionally not Theme-tokened,
                // the same call AgentSpawnMenu makes.
                Image {
                    anchors.verticalCenter: parent.verticalCenter
                    source: {
                        const entry = controller.harness_menu_entry(row.harness);
                        return entry ? Qt.resolvedUrl(entry.icon) : "";
                    }
                    sourceSize.width: Theme.font.size.sm * 2
                    sourceSize.height: Theme.font.size.sm * 2
                    width: Theme.font.size.sm
                    height: Theme.font.size.sm
                    smooth: true
                    fillMode: Image.PreserveAspectFit
                }

                Text {
                    anchors.verticalCenter: parent.verticalCenter
                    // Dense DISPLAY position, not the internal slot — the same
                    // number Ctrl+1..5 addresses.
                    text: row.displayNumber
                    color: row.focusedSlot ? Theme.color.text.strong : Theme.color.text.dim
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.xs
                    font.weight: row.focusedSlot
                        ? Theme.font.weight.bold
                        : Theme.font.weight.medium
                    font.letterSpacing: 0.6
                    renderType: Text.NativeRendering
                }

                Text {
                    id: threadTitle
                    anchors.verticalCenter: parent.verticalCenter
                    // Whatever the Row has left after the marks before it.
                    // No loop: a Row child's `x` depends only on the widths of
                    // its PRECEDING siblings, and rowContent's own width comes
                    // from anchors rather than from its children.
                    width: Math.max(0, rowContent.width - threadTitle.x)
                    text: row.sessionTitle !== "" ? row.sessionTitle : "untitled"
                    color: row.focusedSlot ? Theme.color.text.strong : Theme.color.text.normal
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.xs
                    renderType: Text.NativeRendering
                    elide: Text.ElideRight
                }
            }

            // Trailing indicator cluster — worktree, browser, coordination.
            // A SIBLING of rowContent, pinned to the row's right edge, so the
            // title above elides against it instead of pushing it off-row.
            Row {
                id: trailing
                anchors.verticalCenter: parent.verticalCenter
                anchors.right: parent.right
                anchors.rightMargin: Theme.spacing.sm
                spacing: Theme.spacing.xs

                // Worktree — this agent's live work root is a linked git
                // worktree of the project. Escape form of the glyph, never
                // the literal private-use character.
                Text {
                    anchors.verticalCenter: parent.verticalCenter
                    visible: row.worktreeName.length > 0
                    text: Theme.glyph.worktree
                    font.family: editorFontFamily
                    font.pixelSize: Theme.font.size.sm
                    color: Theme.color.accent.primary
                    renderType: Text.NativeRendering
                }

                // Browser ownership (globe) + its attention badge. Clicking
                // goes to that agent's browser — one-way, as on the pill.
                Item {
                    id: browserIndicator
                    anchors.verticalCenter: parent.verticalCenter
                    visible: row.ownsBrowser
                    width: visible ? browserGlyph.implicitWidth : 0
                    height: browserGlyph.implicitHeight

                    Text {
                        id: browserGlyph
                        anchors.centerIn: parent
                        text: ""  // nf-fa-globe
                        font.family: editorFontFamily
                        font.pixelSize: Theme.font.size.sm
                        color: row.browserAttention
                            ? Theme.color.text.strong
                            : Theme.color.text.dim
                        renderType: Text.NativeRendering
                    }

                    AttentionDot {
                        visible: row.browserAttention
                        width: Math.round(browserGlyph.implicitHeight * 0.34)
                        height: width
                        color: Theme.color.accent.primary
                        anchors.right: browserGlyph.right
                        anchors.top: browserGlyph.top
                        anchors.rightMargin: -Math.round(width * 0.25)
                        anchors.topMargin: Math.round(width * 0.15)
                    }

                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: controller.focus_agent_browser(row.slot)
                    }
                }

                // Coordination attention — a wait_for_agent trigger needs
                // the user. Blue, so it reads as coordination rather than
                // as the globe's amber notification.
                AttentionDot {
                    anchors.verticalCenter: parent.verticalCenter
                    visible: row.coordAttention
                    width: visible ? Math.round(Theme.font.size.sm * 0.55) : 0
                    height: Math.round(Theme.font.size.sm * 0.55)
                    color: Theme.color.mode.command
                }
            }

            // Pointer twin of Enter. Selecting and activating in one gesture:
            // a click is already an explicit choice, unlike j/k.
            MouseArea {
                anchors.fill: parent
                cursorShape: Qt.PointingHandCursor
                onClicked: root.activateThread(row.slot)
                onPressed: threadList.currentIndex = row.index
            }
        }
    }
}
