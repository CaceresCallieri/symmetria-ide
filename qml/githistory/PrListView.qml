// Pull-request list — the "master" pane of the git surface's PR tab.
//
// A keyboard-driven ListView over the injected `model` (a GhPrListModel
// exposed from Python). Unlike the commit list, selection-move does NOT load
// the detail pane — each PR detail is two network `gh` calls, so the list
// navigates locally and only an explicit Enter/l opens a PR's conversation
// (openDetailRequested). This is the deliberate network-priced counterpart
// to the commit list's live-selection idiom.
//
// Bound to an INJECTED model + controller (set by Main.qml through
// GitHistoryView), never globals — extraction-ready like its siblings.

import QtQuick
import QtQuick.Layouts
import ".."
import "../design"

FocusScope {
    id: root

    // Injected from the host (GitHistoryView forwards Main.qml's props).
    required property var model
    // GhPrController — refresh / toggle_state_filter / list meta states.
    property var controller: null

    // Tab / Backtab → host cycles sub-views (same contract as CommitListView).
    signal toggleRequested()
    // Enter / l on a row → host loads that PR's detail + focuses the pane.
    signal openDetailRequested(int number)
    // c on a row → bubbles to Main's checkout ConfirmDialog (branch shown in
    // the dialog message, hence the second arg).
    signal checkoutRequested(int number, string branch)
    // Esc dismisses a stuck git-ops status toast. Same UNCONDITIONAL-accept
    // caveat as CommitListView: Esc is swallowed here whether or not a toast
    // is up; gate the accept on toast visibility if another Esc binding ever
    // lands on this leaf.
    signal dismissStatusRequested()

    // The highlighted row's data, surfaced for the key handlers (Enter/c).
    // Null when the list is empty. Same view.currentItem idiom as
    // CommitListView.currentCommit — a C++ model exposes no get(i) to QML.
    readonly property var currentPr: view.currentItem
        ? ({
            number: view.currentItem.number,
            title: view.currentItem.title,
            headRefName: view.currentItem.headRefName,
            state: view.currentItem.state,
            isDraft: view.currentItem.isDraft,
        })
        : null

    // Re-request focus onto the list (host calls this when the tab shows).
    function focusList(): void {
        view.forceActiveFocus();
    }

    // Fixed delegate row height — shared with VimListNav's half-page math.
    readonly property int rowHeight: 34

    VimListNav {
        id: nav
        view: view
        rowHeight: root.rowHeight
    }

    ListView {
        id: view
        anchors.fill: parent
        clip: true
        focus: true
        model: root.model
        currentIndex: 0
        highlightMoveDuration: 0
        boundsBehavior: Flickable.StopAtBounds
        cacheBuffer: 600
        // Content inset for the selection pill's shadow (see CommitListView).
        topMargin: Theme.spacing.xs
        bottomMargin: Theme.spacing.xs

        // After a reset (refresh / filter toggle / repo switch) re-seed the
        // selection onto the first row. `currentIndex < 0` discriminates a
        // reset from ordinary navigation (see CommitListView's note).
        Connections {
            target: root.model
            function onCountChanged(): void {
                if (view.count > 0 && view.currentIndex < 0)
                    view.currentIndex = 0;
            }
        }

        Keys.onPressed: function (event) {
            // Tab / Backtab → ask the host to cycle sub-views. Accept so
            // Qt's focus traversal doesn't also fire.
            if (event.key === Qt.Key_Tab || event.key === Qt.Key_Backtab) {
                root.toggleRequested();
                event.accepted = true;
                return;
            }
            // Shared vim motions first (j/k, gg/G, Ctrl+D/U).
            if (nav.handleKey(event)) {
                event.accepted = true;
                return;
            }
            switch (event.key) {
            case Qt.Key_Return:
            case Qt.Key_Enter:
            case Qt.Key_L:
                // Open the selected PR's conversation (the one network-priced
                // action — deliberately not fired per j/k step).
                if (root.currentPr) {
                    root.openDetailRequested(root.currentPr.number);
                    event.accepted = true;
                }
                break;
            case Qt.Key_R:
                // Manual refresh — THE freshness mechanism (no polling, ever).
                // Ctrl-guarded like `c` so Ctrl+R stays a no-op here.
                if (root.controller && !(event.modifiers & Qt.ControlModifier)) {
                    root.controller.refresh();
                    event.accepted = true;
                }
                break;
            case Qt.Key_O:
                // Toggle open ↔ closed (closed includes merged; the row's
                // state chip distinguishes). Ctrl-guarded like `c`.
                if (root.controller && !(event.modifiers & Qt.ControlModifier)) {
                    root.controller.toggle_state_filter();
                    event.accepted = true;
                }
                break;
            case Qt.Key_C:
                // Checkout the selected PR's branch — bubbles to Main's
                // confirm dialog (checkout moves HEAD + the working tree).
                if (root.currentPr && !(event.modifiers & Qt.ControlModifier)) {
                    root.checkoutRequested(root.currentPr.number,
                                           root.currentPr.headRefName);
                    event.accepted = true;
                }
                break;
            case Qt.Key_Escape:
                root.dismissStatusRequested();
                event.accepted = true;
                break;
            }
        }

        delegate: Item {
            id: rowItem
            required property int number
            required property string title
            required property string authorLogin
            required property string relativeUpdated
            required property string state
            required property string headRefName
            required property bool isDraft
            required property string reviewDecision
            required property int index

            readonly property bool isCurrent: ListView.isCurrentItem

            width: ListView.view.width
            height: root.rowHeight

            // Clay selection capsule — same idiom as CommitListView.
            PillSurface {
                anchors.fill: parent
                anchors.topMargin: Theme.spacing.xxs
                anchors.bottomMargin: Theme.spacing.xxs
                anchors.leftMargin: Theme.spacing.xs
                anchors.rightMargin: Theme.spacing.xs
                radius: Theme.radius.sm
                elevated: rowItem.isCurrent
                color: rowItem.isCurrent ? Theme.color.bg.selected : "transparent"
                borderColor: rowItem.isCurrent ? Theme.color.border.hairline : "transparent"
            }

            MouseArea {
                anchors.fill: parent
                onClicked: {
                    view.currentIndex = rowItem.index;
                    view.forceActiveFocus();
                }
                onDoubleClicked: root.openDetailRequested(rowItem.number)
            }

            RowLayout {
                anchors.left: parent.left
                anchors.leftMargin: Theme.spacing.md
                anchors.right: parent.right
                anchors.rightMargin: Theme.spacing.md
                anchors.verticalCenter: parent.verticalCenter
                spacing: Theme.spacing.sm

                Text {
                    Layout.preferredWidth: 48
                    Layout.alignment: Qt.AlignVCenter
                    text: "#" + rowItem.number
                    color: Theme.color.accent.primary
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.sm
                    elide: Text.ElideRight
                    renderType: Text.NativeRendering
                }

                Text {
                    Layout.fillWidth: true
                    Layout.alignment: Qt.AlignVCenter
                    text: rowItem.title
                    color: rowItem.isCurrent ? Theme.color.text.strong : Theme.color.text.normal
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.sm
                    elide: Text.ElideRight
                    renderType: Text.NativeRendering
                }

                // Draft badge — outline pill like the commit list's ref badge.
                Rectangle {
                    visible: rowItem.isDraft
                    Layout.preferredWidth: visible ? draftLabel.implicitWidth + Theme.spacing.sm : 0
                    Layout.preferredHeight: draftLabel.implicitHeight + Theme.spacing.xxs * 2
                    Layout.alignment: Qt.AlignVCenter
                    radius: Theme.radius.sm
                    color: "transparent"
                    border.color: Theme.color.text.dim
                    border.width: 1
                    Text {
                        id: draftLabel
                        anchors.centerIn: parent
                        text: "draft"
                        color: Theme.color.text.dim
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.xs
                        renderType: Text.NativeRendering
                    }
                }

                // MERGED/CLOSED state chip — meaningful only under the
                // "closed" filter (open rows all say OPEN, so it's hidden).
                Text {
                    visible: rowItem.state !== "OPEN"
                    Layout.alignment: Qt.AlignVCenter
                    text: rowItem.state === "MERGED" ? "merged" : "closed"
                    color: rowItem.state === "MERGED"
                           ? Theme.color.accent.bright : Theme.color.text.dim
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.xs
                    renderType: Text.NativeRendering
                }

                // Review-decision glyph: ✓ approved / ± changes requested.
                // Borrow the diff palette — same "accepted/pushed-back"
                // semantic family as added/removed lines.
                Text {
                    visible: rowItem.reviewDecision === "APPROVED"
                             || rowItem.reviewDecision === "CHANGES_REQUESTED"
                    Layout.alignment: Qt.AlignVCenter
                    text: rowItem.reviewDecision === "APPROVED" ? "✓" : "±"
                    color: rowItem.reviewDecision === "APPROVED"
                           ? Theme.color.diff.addedFg : Theme.color.diff.removedFg
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.sm
                    renderType: Text.NativeRendering
                }

                Text {
                    Layout.alignment: Qt.AlignVCenter
                    text: rowItem.authorLogin
                    color: Theme.color.text.dim
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.xs
                    renderType: Text.NativeRendering
                }

                Text {
                    Layout.alignment: Qt.AlignVCenter
                    text: rowItem.relativeUpdated
                    color: Theme.color.text.dim
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.xs
                    renderType: Text.NativeRendering
                }
            }
        }
    }

    // Center states — loading, error (gh missing / unauthenticated / no
    // GitHub remote), and empty. Mutually exclusive by evaluation order.
    Text {
        anchors.centerIn: parent
        width: Math.min(implicitWidth, parent.width - Theme.spacing.lg * 2)
        visible: root.controller != null && root.controller.listLoading
        text: "loading pull requests…"
        color: Theme.color.text.dim
        font.family: Theme.font.family
        font.pixelSize: Theme.font.size.sm
        renderType: Text.NativeRendering
    }

    Text {
        anchors.centerIn: parent
        width: parent.width - Theme.spacing.lg * 2
        visible: root.controller != null && !root.controller.listLoading
                 && root.controller.listError.length > 0
        text: root.controller ? root.controller.listError : ""
        color: Theme.color.text.dim
        font.family: Theme.font.family
        font.pixelSize: Theme.font.size.sm
        wrapMode: Text.Wrap
        horizontalAlignment: Text.AlignHCenter
        renderType: Text.NativeRendering
    }

    Text {
        anchors.centerIn: parent
        visible: view.count === 0
                 && root.controller != null
                 && !root.controller.listLoading
                 && root.controller.listError.length === 0
        text: "no " + (root.controller ? root.controller.stateFilter : "open")
              + " pull requests"
        color: Theme.color.text.dim
        font.family: Theme.font.family
        font.pixelSize: Theme.font.size.sm
        renderType: Text.NativeRendering
    }
}
