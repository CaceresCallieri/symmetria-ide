// Pull-request detail — the full-surface conversation view of the PR tab.
//
// Shown IN PLACE of the PR list (a drill-in, not a side pane — user decision
// 2026-07-12): Enter on a PR swaps the list for this view; h/Esc swaps back.
// Shows the loaded PR's header (title, state, author, branches, description)
// plus the FLATTENED conversation timeline: issue comments, review summaries
// (approved / changes requested), and inline code-review comments prefixed
// with their `file:line` anchor — merged chronologically by the controller
// (v1 scope: conversation over code; no diff rendering here).
//
// The header card is the timeline ListView's `header` component, so the
// whole thing scrolls as ONE flow and the description renders UNTRUNCATED —
// nothing is ever cut (user decision 2026-07-12); a long body simply
// scrolls away as j/k walks into the conversation.
//
// Loading is Enter-driven, not selection-driven (each load is two network gh
// calls): the host sets `requestedNumber` when the user opens a PR, and the
// `ready` guard (`controller.detailNumber === requestedNumber`, the
// CommitDetailView diffReady idiom) suppresses the previously-loaded PR
// while the new one is in flight.
//
// Bodies render as PLAIN TEXT deliberately — PR bodies/comments are
// untrusted remote input and QML's MarkdownText would style (and follow)
// whatever arrives; the per-kind delegates below are the iteration seam if
// richer rendering is ever wanted.

import QtQuick
import ".."
import "../design"

Rectangle {
    id: root

    // Focus model: this ROOT owns keyboard focus, always programmatically
    // (the host's focusList() calls) — there is deliberately no declarative
    // `focus:` binding and no focusable child. The inner timeline must never
    // take focus (it's invisible during loading/error states and would go
    // deaf — see the focusList note below); keep it that way when adding
    // children.

    // GhPrController — the detail* properties + checkout target.
    property var controller: null
    // GhPrTimelineModel — the flattened conversation rows.
    property var timelineModel: null
    // The PR the host asked to open (0 = nothing requested yet). Set by
    // GitHistoryView on Enter; reset on surface re-entry.
    property int requestedNumber: 0

    // h / Esc → host swaps back to the PR list.
    signal backRequested()
    // Tab → host cycles sub-views (same contract as every other leaf).
    signal toggleRequested()
    // c → bubbles to Main's checkout ConfirmDialog.
    signal checkoutRequested(int number, string branch)

    color: Theme.color.bg.chrome

    readonly property bool hasRequest: root.requestedNumber > 0
    // The async-gap guard: only render once the controller's loaded detail
    // IS the requested PR (stale prior detail never shows).
    readonly property bool ready: hasRequest && root.controller != null
                                  && root.controller.detailNumber === root.requestedNumber
    readonly property bool showError: ready && root.controller.detailError.length > 0

    // Focus lands on the ROOT, not the timeline: the timeline is invisible
    // during the placeholder/loading/error states, and an invisible item
    // refuses focus — which left the whole view deaf to keys mid-load (no
    // way back, the original bug). The root is always visible while the
    // detail is open, so keys — including back — work in every state; the
    // timeline itself never takes focus and is driven programmatically.
    function focusList(): void {
        root.forceActiveFocus();
    }

    // All key handling lives here (see focusList note). S (Shift+S) is the
    // project-wide "go back" key for drill-in views (user decision
    // 2026-07-12 — see .claude/memory/feedback/shift_s_back_navigation.md);
    // h and Esc remain as vim-flavored aliases.
    Keys.onPressed: function (event) {
        if (event.key === Qt.Key_Tab || event.key === Qt.Key_Backtab) {
            root.toggleRequested();
            event.accepted = true;
            return;
        }
        if (nav.handleKey(event)) {
            event.accepted = true;
            return;
        }
        switch (event.key) {
        case Qt.Key_S:
            // Shift+S — the canonical back. Bare s stays free.
            if (event.modifiers & Qt.ShiftModifier) {
                root.backRequested();
                event.accepted = true;
            }
            break;
        case Qt.Key_H:
        case Qt.Key_Escape:
            // Back to the PR list. (This leaf's Esc is "back", not
            // toast-dismiss — a different focused leaf than the list.)
            root.backRequested();
            event.accepted = true;
            break;
        case Qt.Key_C:
            if (root.ready && !(event.modifiers & Qt.ControlModifier)) {
                root.checkoutRequested(root.controller.detailNumber,
                                       root.controller.detailHeadRef);
                event.accepted = true;
            }
            break;
        }
    }

    // A new PR was opened (Enter on a different row): reset the read cursor
    // and snap to the top so the new conversation starts at its header, not
    // wherever the previous one was scrolled to.
    onRequestedNumberChanged: {
        timeline.currentIndex = 0;
        timeline.positionViewAtBeginning();
    }

    // --- Center states ----------------------------------------------------

    Text {
        anchors.centerIn: parent
        visible: !root.hasRequest
        text: "select a pull request and press Enter to read it"
        color: Theme.color.text.dim
        font.family: Theme.font.family
        font.pixelSize: Theme.font.size.sm
        renderType: Text.NativeRendering
    }

    Text {
        anchors.centerIn: parent
        visible: root.hasRequest && !root.ready
        text: "loading PR #" + root.requestedNumber + "…"
        color: Theme.color.text.dim
        font.family: Theme.font.family
        font.pixelSize: Theme.font.size.sm
        renderType: Text.NativeRendering
    }

    Text {
        anchors.centerIn: parent
        width: parent.width - Theme.spacing.lg * 2
        visible: root.showError
        text: root.controller ? root.controller.detailError : ""
        color: Theme.color.text.dim
        font.family: Theme.font.family
        font.pixelSize: Theme.font.size.sm
        wrapMode: Text.Wrap
        horizontalAlignment: Text.AlignHCenter
        renderType: Text.NativeRendering
    }

    // --- Content: header + conversation as ONE scrolling flow -------------

    ListView {
        id: timeline
        anchors.fill: parent
        visible: root.ready && !root.showError
        clip: true
        model: root.timelineModel
        currentIndex: 0
        highlightMoveDuration: 0
        boundsBehavior: Flickable.StopAtBounds
        cacheBuffer: 600
        topMargin: Theme.spacing.xs
        bottomMargin: Theme.spacing.xs

        // Selection is a read cursor: j/k walk entries and keep the
        // current one in view. rowHeight is an approximation for the
        // half-page math — rows vary in height, so Ctrl+D/U jumps by
        // entry count, not exact pixels. Acceptable for a read cursor.
        VimListNav {
            id: nav
            view: timeline
            rowHeight: 56
        }

        // The header (title + description) lives ABOVE index 0, so plain
        // keep-current-visible positioning would strand it off-screen once
        // the user scrolls into the conversation. Whenever the cursor
        // returns to the first entry (k back up, or gg), snap to the very
        // top so the header is readable again — keyboard-first demands the
        // header be reachable without a mouse wheel.
        onCurrentIndexChanged: {
            if (currentIndex === 0)
                positionViewAtBeginning();
        }

        // Metadata header (clay card) — a ListView header so it scrolls
        // WITH the conversation; the description below it renders in full
        // (no maximumLineCount, no elide — nothing is ever cut).
        header: Item {
            width: timeline.width
            implicitHeight: headerCard.implicitHeight + Theme.spacing.sm * 2
            height: implicitHeight

            PillCard {
                id: headerCard
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.margins: Theme.spacing.sm
                implicitHeight: headerCol.implicitHeight + Theme.spacing.md * 2
                radius: Theme.radius.md
                elevated: true
                color: Theme.color.bg.raisedSelected

                Column {
                    id: headerCol
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.top: parent.top
                    anchors.margins: Theme.spacing.md
                    spacing: Theme.spacing.xs

                    Text {
                        width: parent.width
                        text: root.ready
                              ? "#" + root.controller.detailNumber + "  "
                                + root.controller.detailTitle
                                + (root.controller.detailIsDraft ? "  (draft)" : "")
                              : ""
                        color: Theme.color.text.emphasis
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.md
                        font.weight: Theme.font.weight.bold
                        wrapMode: Text.Wrap
                        renderType: Text.NativeRendering
                    }

                    Text {
                        width: parent.width
                        text: root.ready
                              ? root.controller.detailState.toLowerCase()
                                + "  ·  " + root.controller.detailAuthor
                                + "  ·  " + root.controller.detailHeadRef
                                + " → " + root.controller.detailBaseRef
                                + "  ·  opened " + root.controller.detailCreatedRelative
                              : ""
                        color: Theme.color.text.dim
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.xs
                        wrapMode: Text.Wrap
                        renderType: Text.NativeRendering
                    }

                    Text {
                        width: parent.width
                        text: root.ready
                              ? "+" + root.controller.detailAdditions
                                + " −" + root.controller.detailDeletions
                                + "  ·  " + root.controller.detailChangedFiles + " files"
                              : ""
                        color: Theme.color.text.dim
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.xs
                        renderType: Text.NativeRendering
                    }

                    // Description body — plain text (untrusted input),
                    // rendered IN FULL; the header scrolls, so length costs
                    // nothing but scroll distance.
                    Text {
                        width: parent.width
                        visible: root.ready && root.controller.detailBody.length > 0
                        text: root.ready ? root.controller.detailBody : ""
                        textFormat: Text.PlainText
                        color: Theme.color.text.normal
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.sm
                        wrapMode: Text.Wrap
                        topPadding: Theme.spacing.xs
                        renderType: Text.NativeRendering
                    }
                }
            }
        }

        delegate: Item {
            id: entryItem
            required property string kind
            required property string author
            required property string relativeTime
            required property string body
            required property string reviewState
            required property string location
            required property int index

            readonly property bool isCurrent: ListView.isCurrentItem

            width: ListView.view.width
            implicitHeight: entryCol.implicitHeight + Theme.spacing.sm * 2
            height: implicitHeight

            // Subtle read-cursor capsule (flat fill, no depth — the
            // timeline is prose, not a pick list).
            Rectangle {
                anchors.fill: parent
                anchors.topMargin: Theme.spacing.xxs
                anchors.bottomMargin: Theme.spacing.xxs
                anchors.leftMargin: Theme.spacing.xs
                anchors.rightMargin: Theme.spacing.xs
                radius: Theme.radius.sm
                color: entryItem.isCurrent ? Theme.color.bg.selected : "transparent"
                border.width: 1
                border.color: entryItem.isCurrent
                              ? Theme.color.border.hairline : "transparent"
            }

            MouseArea {
                anchors.fill: parent
                onClicked: {
                    timeline.currentIndex = entryItem.index;
                    root.focusList();
                }
            }

            Column {
                id: entryCol
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                // Inline (code-anchored) entries inset further so they
                // read as attached to code rather than the conversation.
                anchors.leftMargin: Theme.spacing.md
                                    + (entryItem.kind === "inline" ? Theme.spacing.lg : 0)
                anchors.rightMargin: Theme.spacing.md
                spacing: Theme.spacing.xxs

                // Header line — branched by kind.
                Row {
                    width: parent.width
                    spacing: Theme.spacing.sm

                    // Review verdict (reviews only) — diff palette, the
                    // same accepted/pushed-back semantic as the list glyph.
                    Text {
                        visible: entryItem.kind === "review"
                        text: entryItem.reviewState === "APPROVED" ? "✓ approved"
                              : entryItem.reviewState === "CHANGES_REQUESTED" ? "± changes requested"
                              : entryItem.reviewState === "DISMISSED" ? "review dismissed"
                              : "commented"
                        color: entryItem.reviewState === "APPROVED" ? Theme.color.diff.addedFg
                               : entryItem.reviewState === "CHANGES_REQUESTED" ? Theme.color.diff.removedFg
                               : Theme.color.text.dim
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.xs
                        font.weight: Theme.font.weight.bold
                        renderType: Text.NativeRendering
                    }

                    // file:line anchor (inline entries only).
                    Text {
                        visible: entryItem.kind === "inline"
                        text: entryItem.location
                        color: Theme.color.accent.primary
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.xs
                        renderType: Text.NativeRendering
                    }

                    Text {
                        text: entryItem.author
                        color: Theme.color.text.strong
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.xs
                        renderType: Text.NativeRendering
                    }

                    Text {
                        text: entryItem.relativeTime
                        color: Theme.color.text.dim
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.xs
                        renderType: Text.NativeRendering
                    }
                }

                // Body — plain text, wrapped, in full. Empty for verdict-only
                // reviews (the header line carries the whole message).
                Text {
                    width: parent.width
                    visible: entryItem.body.length > 0
                    text: entryItem.body
                    textFormat: Text.PlainText
                    color: Theme.color.text.normal
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.sm
                    wrapMode: Text.Wrap
                    renderType: Text.NativeRendering
                }
            }
        }

        // Empty conversation (a PR with no comments yet) — sits below the
        // header, not centered over it.
        Text {
            anchors.horizontalCenter: parent.horizontalCenter
            anchors.bottom: parent.bottom
            anchors.bottomMargin: Theme.spacing.lg * 2
            visible: timeline.count === 0
            text: "no conversation yet"
            color: Theme.color.text.dim
            font.family: Theme.font.family
            font.pixelSize: Theme.font.size.sm
            renderType: Text.NativeRendering
        }
    }
}
