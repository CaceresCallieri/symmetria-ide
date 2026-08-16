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
//
// ─── THREE ROW STATES, THREE CHANNELS ───────────────────────────────────
// A row can be all three at once, so none of them may be a fill:
//
//   ACTIVE   — the agent on the central surface (`focusedSlot`): filled
//              background. With no focused agent NO row carries it.
//   SELECTED — the keyboard cursor (`selected`, moved with j/k): a marker bar
//              down the row's left edge. Deliberately NOT the fill, which is
//              the bug this replaced — the cursor painted itself in the
//              "active" colour, so the highlight stayed where the cursor had
//              last been while a different agent was on screen.
//   HOVER    — the pointer: a translucent wash painted OVER whichever fill is
//              underneath, so it composes with the other two instead of
//              replacing them.
//
// Tokens for all three live in `Theme.color.rail`.

import QtQuick
import Symmetria.Agents.UI as AgentsUI

import "design"

FocusScope {
    id: root

    // Above the separator and the central surface in the parent RowLayout's
    // paint order, for ONE reason: the peek panel at the bottom of this file
    // hangs off the rail's right edge over the central surface, and a panel
    // constrained to the 220px rail would only re-elide the title it exists to
    // show. Nothing else here overlaps a sibling, and window-scope overlays
    // (modal scrims, popups) are siblings of the whole RowLayout, so they
    // still paint above this.
    z: 1

    // ─── Peek state ─────────────────────────────────────────────────────
    // Which row the peek panel describes, and the snapshot it draws. The
    // delegates PUSH into these rather than the panel reading the model,
    // because a live row's title comes from the pool (`agentTitles`) and only
    // the delegate holds the resolved live-or-durable value.
    property int peekIndex: -1
    property string peekTitle: ""
    property string peekMeta: ""
    // The row's y in the ListView's CONTENT coordinates plus its height, so
    // the panel can track the row through a scroll with a live binding on
    // `contentY` instead of a stale absolute position.
    property real peekRowY: 0
    property real peekRowHeight: 0
    // Set by the delay timer, cleared the moment the target changes: the panel
    // opens after a pause, so running the list with j/k does not fire a burst.
    property bool peekShown: false
    // Index of the row under the pointer, -1 for none. The pointer WINS over
    // the keyboard cursor while it is on a row; when it leaves, the selected
    // row's binding re-evaluates and the panel returns to the cursor.
    property int hoverIndex: -1

    function _trackHover(index: int, hovered: bool): void {
        if (hovered)
            root.hoverIndex = index;
        else if (root.hoverIndex === index)
            root.hoverIndex = -1;
    }

    function _openPeek(index: int, title: string, meta: string, rowY: real, rowHeight: real): void {
        // A new target hides whatever is open BEFORE the delay restarts, so
        // the panel never lingers on the previous row's text while pointing at
        // this one.
        if (root.peekIndex !== index)
            root.peekShown = false;
        root.peekIndex = index;
        root.peekTitle = title;
        root.peekMeta = meta;
        root.peekRowY = rowY;
        root.peekRowHeight = rowHeight;
        peekDelay.restart();
    }

    // Index-guarded: delegates are recycled, so a row that stops being the
    // target after another row has already claimed the panel must not close
    // it. Same discipline as `_currentSlot` reading through the model.
    function _closePeek(index: int): void {
        if (root.peekIndex !== index)
            return;
        peekDelay.stop();
        root.peekIndex = -1;
        root.peekShown = false;
    }

    // Ctrl+H (the spatial-navigation block in Main.qml) calls
    // `forceActiveFocus()` on this scope directly. The ListView below carries
    // `focus: true`, so it is the scope's focus delegate and the grant lands
    // on its Keys.onPressed handler — verified in an offscreen probe rather
    // than assumed, since a FocusScope with no focused child swallows it.
    //
    // The single dispatch into the pool, and the one place the live/dead
    // split is decided: `slot > 0` means there is a pane to go to, `slot === 0`
    // means the conversation outlived its CLI and has to be brought back.
    //
    // The dead branch dispatches on `threadId` — the durable
    // "<harness>:<session_id>" identity — and NOT on the row's position or on
    // any per-slot index, because a dead row has no slot to index and its
    // position moves whenever a live agent comes or goes.
    function activateThread(slot: int, threadId: string): void {
        if (slot > 0)
            controller.focus_agent(slot);
        else if (threadId !== "")
            controller.resume_thread(threadId);
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

    // Same reason as `_currentSlot`, for the identity a dead row activates by.
    function _currentThreadId(): string {
        return agentThreads.thread_id_at(threadList.currentIndex);
    }

    // `x` — put the thread to SLEEP: the CLI dies and its RAM comes back, the
    // row stays and stays resumable. Deliberately not `close_agent`, which is
    // the internal primitive (and the VPS detach); the rail must never be the
    // surface that makes a conversation disappear.
    //
    // Ending an agent from the rail must LEAVE the user in the rail.
    // The close path refocuses a survivor, and `focus_agent` both swaps the
    // central surface to "agent" and lands keyboard focus in that agent's
    // terminal — so without this the first `x` throws you out of the column
    // you are working in, and a second `x` (meant for the next thread) is
    // typed as a literal `x` into the surviving agent's prompt.
    // `Qt.callLater` because the refocus has to run AFTER the focusAgentRequested
    // handler in Main.qml, not before it.
    function _endThread(slot: int): void {
        if (slot <= 0)
            return;
        controller.sleep_agent(slot);
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
        // activates the row — focusing a live agent, resuming a dead thread —
        // and `x` sleeps it: the CLI dies, the thread stays listed and stays
        // resumable.
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
                root.activateThread(root._currentSlot(), root._currentThreadId());
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
            required property string threadId
            required property string harness
            required property string title
            required property string worktree
            required property double updatedAt

            // The dead/live gate every per-slot read below depends on.
            readonly property bool live: row.slot > 0
            readonly property int displayNumber: row.index + 1
            readonly property bool selected: row.index === threadList.currentIndex
            readonly property bool focusedSlot: row.live && controller.focusedAgent === row.slot
            readonly property bool hovered: rowHover.hovered
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
            // One fallback, two readers (the row's own Text and the peek
            // panel's), so an empty title cannot render two different ways.
            readonly property string displayTitle:
                row.sessionTitle !== "" ? row.sessionTitle : "untitled"

            // Whether the peek panel should describe THIS row. The pointer
            // wins whenever it is on any row (`root.hoverIndex < 0` gates the
            // keyboard half), so moving the mouse does not fight the cursor,
            // and letting go of the mouse hands the panel back to the cursor.
            //
            // `threadList.activeFocus` is load-bearing: the rail keeps its
            // selection while the user types in a terminal, so without the
            // gate the panel would hang over the central surface for the rest
            // of the session, pointing at a row nobody is looking at.
            readonly property bool peeked: row.hovered
                || (root.hoverIndex < 0 && row.selected && threadList.activeFocus)

            onPeekedChanged: row._syncPeek()
            onHoveredChanged: root._trackHover(row.index, row.hovered)

            function _syncPeek(): void {
                if (row.peeked)
                    root._openPeek(row.index, row.displayTitle, row._peekMeta(), row.y, row.height);
                else
                    root._closePeek(row.index);
            }

            // The context the ROW cannot show: which harness the conversation
            // belongs to, its worktree in words rather than as a glyph, and
            // whether it is running or how long ago it last moved.
            //
            // A function, not a property: it reads `Date.now()`, which is not
            // a binding dependency, so a cached property would show an age
            // frozen at the moment the delegate was created (gotcha #3). It is
            // evaluated once per open instead.
            function _peekMeta(): string {
                const parts = [row.harness];
                if (row.worktreeName !== "")
                    parts.push(Theme.glyph.worktree + " " + row.worktreeName);
                if (row.live)
                    parts.push(row.activity && row.activity.state ? row.activity.state : "running");
                else if (row.updatedAt > 0)
                    parts.push(UsageFormat.age(row.updatedAt, Date.now()));
                return parts.join("   ");
            }

            width: ListView.view.width
            height: Math.max(rowContent.implicitHeight, trailing.implicitHeight)
                + Theme.spacing.sm * 2
            radius: Theme.radius.sm
            // ACTIVE only — the agent being VIEWED, never the keyboard cursor.
            // `focusedSlot` is false for every row when `focusedAgent` is 0, so
            // with no agent focused nothing is filled. The cursor gets the
            // marker bar below and the pointer gets the wash; see the
            // three-channel note in this file's header.
            color: row.focusedSlot ? Theme.color.rail.activeRow : "transparent"

            // HOVER — a wash ON TOP of the fill rather than a competing fill,
            // which is what lets a row be active AND hovered and still show
            // both. Translucent by construction (see Theme.color.rail).
            Rectangle {
                anchors.fill: parent
                radius: parent.radius
                visible: row.hovered
                color: Theme.color.rail.hoverRow
            }

            // SELECTED — the keyboard cursor. A marker bar down the left edge,
            // inside `rowContent`'s left margin so it displaces no content and
            // can coexist with the fill and the wash.
            Rectangle {
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
                anchors.leftMargin: Theme.spacing.xxs
                visible: row.selected
                width: Theme.spacing.xxs
                height: parent.height - Theme.spacing.sm * 2
                radius: width / 2
                color: Theme.color.rail.selectionMarker
            }

            HoverHandler {
                id: rowHover
                // Informational only — the panel it opens takes no focus and
                // handles no keys, so this must not change the cursor or eat
                // the click the MouseArea below owns.
            }

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
                // pills carried, driven by the same bridge-fed activity dict,
                // and the row's ONLY harness mark: the chip is coloured by
                // `agentType`, so claude and opencode already read apart at a
                // glance while the same element carries idle / active /
                // working. A separate brand `Image` shipped beside it and was
                // removed as redundant — the colour says which backend and the
                // animation says what it is doing. Do not add it back.
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

                Text {
                    anchors.verticalCenter: parent.verticalCenter
                    // ⚠ LIVE ROWS ONLY. `displayNumber` is the dense position
                    // in the list, and Ctrl+1..5 addresses `agentOrder`, which
                    // holds live slots only — the two coincide exactly while
                    // every row above this one is live, which is true because
                    // ThreadHistory appends dead rows AFTER the live ones. A
                    // dead row's position is therefore past the end of
                    // agentOrder: printing its number beside it would offer a
                    // digit that opens the spawn menu instead of that thread.
                    // Dead threads are reached by Enter or a click.
                    visible: row.live
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
                    text: row.displayTitle
                    // Three rungs, one per state: the focused agent reads
                    // strongest, other live agents normal, and a dead thread
                    // recedes to the dim rung — present and activatable, but
                    // visibly not something that is running.
                    color: row.focusedSlot
                        ? Theme.color.text.strong
                        : (row.live ? Theme.color.text.normal : Theme.color.text.dim)
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
                onClicked: root.activateThread(row.slot, row.threadId)
                onPressed: threadList.currentIndex = row.index
            }
        }
    }

    // The pause before the peek panel opens. Restarted by every change of
    // target, so a run down the list with j/k opens ONE panel — the one the
    // user stopped on — instead of a burst.
    Timer {
        id: peekDelay
        interval: Theme.anim.peekDelay
        repeat: false
        onTriggered: root.peekShown = root.peekIndex >= 0
    }

    // ─── Peek panel ─────────────────────────────────────────────────────
    // A row's title is elided, and finding a conversation again is the whole
    // reason the rail exists — a truncated title defeats it. This shows the
    // FULL title plus the context the row has no width for.
    //
    // Passive by construction: no focus, no Keys handler, no MouseArea. Esc is
    // load-bearing in the agent panes (it interrupts a running turn), so an
    // informational panel that took focus would cost the user a cancel — the
    // same rule UsageDetailPopup documents.
    PillCard {
        id: peekCard

        // Hangs off the rail's RIGHT edge, over the central surface: a panel
        // confined to the 220px rail would re-elide the very title it exists
        // to show. It never overlaps the rows themselves, so the pointer can
        // not land on it and end the hover that opened it. (`root.z` above is
        // what lets it paint over the central surface at all.)
        x: root.width + Theme.spacing.xs
        // Vertically centred on its row and clamped into the rail's height, and
        // it tracks the row through a scroll because `contentY` is a live
        // binding dependency rather than a value captured at open time.
        y: Math.max(0, Math.min(root.height - peekCard.height,
            threadList.y + root.peekRowY - threadList.contentY
                + (root.peekRowHeight - peekCard.height) / 2))
        width: Theme.size.threadPeekWidth
        implicitHeight: peekBody.implicitHeight + Theme.spacing.md * 2
        height: implicitHeight
        visible: root.peekShown && root.peekIndex >= 0
        // Grows out of the rail edge it is anchored to, the same FM scale-pop
        // every other popup in the IDE enters with.
        transformOrigin: Item.Left
        scale: Theme.anim.popFromScale
        opacity: 0

        states: State {
            name: "shown"
            when: peekCard.visible
            PropertyChanges {
                peekCard.scale: 1
                peekCard.opacity: 1
            }
        }
        transitions: Transition {
            to: "shown"
            NumberAnimation {
                target: peekCard
                property: "scale"
                duration: Theme.anim.duration
                easing.type: Easing.OutBack
                easing.overshoot: Theme.anim.popOvershoot
            }
            NumberAnimation {
                target: peekCard
                property: "opacity"
                duration: Theme.anim.duration
                easing.type: Easing.BezierSpline
                easing.bezierCurve: Theme.anim.standardCurve
            }
        }

        Column {
            id: peekBody
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.margins: Theme.spacing.md
            spacing: Theme.spacing.xs

            // The whole point: WRAPPED, never elided.
            Text {
                width: peekBody.width
                text: root.peekTitle
                color: Theme.color.text.strong
                font.family: Theme.font.family
                font.pixelSize: Theme.font.size.sm
                wrapMode: Text.Wrap
                renderType: Text.NativeRendering
            }

            Text {
                width: peekBody.width
                visible: root.peekMeta !== ""
                text: root.peekMeta
                color: Theme.color.text.dim
                font.family: Theme.font.family
                font.pixelSize: Theme.font.size.xs
                wrapMode: Text.Wrap
                renderType: Text.NativeRendering
            }
        }
    }
}
