// Branches panel — the leftmost column of the history sub-view.
//
// A keyboard-driven ListView over the injected `model` (a GitBranchListModel
// exposed from Python): local branches with the checked-out marker,
// ahead/behind vs upstream, age, and the worktree annotation ("main
// (worktree symmetria-ide-stable)") — the lazygit branches panel re-read
// through the comprehension-first lens (read-only in v1; checkout is a
// planned GitOpsController follow-up).
//
// Selection semantics are EXPLICIT-APPLY: j/k move the highlight freely with
// no side effects; Enter (or l) emits `branchSelected` and the host filters
// the commit log to that branch. Browsing names must not thrash `git log`
// subprocesses — the highlight is a cursor, not a filter.
//
// Pure intents, like its siblings: this view holds NO controller. The host
// (GitHistoryView) routes `branchSelected` to `logController.set_ref` and
// owns the focus handoffs.
//
// Bound to an INJECTED model (set by Main.qml), never a global — so this
// subtree stays extraction-ready for a future Symmetria.Git.UI module.

import QtQuick
import QtQuick.Layouts
import ".."
import "../design"

FocusScope {
    id: root

    // Injected from the host (Main.qml binds `gitBranchModel`).
    required property var model

    // The branch name the log is currently filtered to ("" = HEAD view).
    // Bound by the host to `logController.currentRef` so the filtered branch
    // renders distinctly from the checked-out one.
    property string activeFilter: ""

    // Enter / l on a row — the host applies the log filter (or clears it
    // when the row is the checked-out branch: "show me where I am" and the
    // unfiltered HEAD view are the same thing) and re-homes focus onto the
    // commit list.
    signal branchSelected(string name, bool isHead)
    // Esc with an active filter — the host clears the filter.
    signal clearFilterRequested()
    // h, or Esc with no filter — back to the commit list, filter unchanged.
    signal focusCommitListRequested()
    // Tab — same sub-view toggle contract as the sibling lists (Tab is Qt's
    // focus-traversal key, so it must be intercepted where focus lives).
    signal toggleRequested()

    // Re-request focus onto the list (host calls this on the `b` key).
    function focusList(): void {
        view.forceActiveFocus();
    }

    // Same row pitch as CommitListView (shared rhythm); the host's
    // content-hugging height cap multiplies this by the visible row count.
    readonly property int rowHeight: 34

    // Shared vim motions (j/k, gg/G, Ctrl+D/U) — see VimListNav.qml.
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
        // Content inset so the selected clay pill's shadow has room before
        // the clip edge (PillSurface gotcha — shadows render OUTSIDE).
        topMargin: Theme.spacing.xs
        bottomMargin: Theme.spacing.xs

        // Every branch refresh is a full model reset (no paging), which
        // clears currentIndex to -1 — re-seed to the top so j/k is live.
        Connections {
            target: root.model
            function onCountChanged(): void {
                if (view.count > 0 && view.currentIndex < 0)
                    view.currentIndex = 0;
            }
        }

        Keys.onPressed: function (event) {
            if (event.key === Qt.Key_Tab || event.key === Qt.Key_Backtab) {
                root.toggleRequested();
                event.accepted = true;
                return;
            }
            // Shared vim motions first (also runs the gg-cancel bookkeeping
            // for every other key — see VimListNav.handleKey's contract).
            if (nav.handleKey(event)) {
                event.accepted = true;
                return;
            }
            switch (event.key) {
            case Qt.Key_Return:
            case Qt.Key_Enter:
            case Qt.Key_L:
                if (view.currentItem)
                    root.branchSelected(view.currentItem.name, view.currentItem.isHead);
                event.accepted = true;
                break;
            case Qt.Key_H:
                root.focusCommitListRequested();
                event.accepted = true;
                break;
            case Qt.Key_Escape:
                // With a filter active Esc clears it (staying here); without
                // one it just hands focus back — vim-like "back out one level".
                if (root.activeFilter.length > 0)
                    root.clearFilterRequested();
                else
                    root.focusCommitListRequested();
                event.accepted = true;
                break;
            }
        }

        delegate: Item {
            id: rowItem
            required property string name
            required property bool isHead
            required property bool hasUpstream
            required property int ahead
            required property int behind
            required property bool upstreamGone
            required property string recency
            required property string worktreeName
            required property bool checkedOutElsewhere
            required property bool detached
            required property int index

            readonly property bool isCurrent: ListView.isCurrentItem
            // The branch the log is filtered to — rendered with the accent
            // ring even when the highlight cursor is elsewhere.
            readonly property bool isFiltered: root.activeFilter.length > 0
                                               && root.activeFilter === rowItem.name

            width: ListView.view.width
            height: root.rowHeight

            // Same clay selection capsule as the sibling lists; the filtered
            // branch keeps a hairline accent ring so the applied filter stays
            // visible while the cursor browses elsewhere.
            PillSurface {
                anchors.fill: parent
                anchors.topMargin: Theme.spacing.xxs
                anchors.bottomMargin: Theme.spacing.xxs
                anchors.leftMargin: Theme.spacing.xs
                anchors.rightMargin: Theme.spacing.xs
                radius: Theme.radius.sm
                elevated: rowItem.isCurrent
                color: rowItem.isCurrent ? Theme.color.bg.selected : "transparent"
                borderColor: rowItem.isCurrent ? Theme.color.border.hairline
                             : rowItem.isFiltered ? Theme.color.accent.bright
                             : "transparent"
            }

            MouseArea {
                anchors.fill: parent
                onClicked: {
                    view.currentIndex = rowItem.index;
                    view.forceActiveFocus();
                }
                onDoubleClicked: root.branchSelected(rowItem.name, rowItem.isHead)
            }

            RowLayout {
                anchors.left: parent.left
                anchors.leftMargin: Theme.spacing.md
                anchors.right: parent.right
                anchors.rightMargin: Theme.spacing.md
                anchors.verticalCenter: parent.verticalCenter
                spacing: Theme.spacing.xs

                // Checked-out marker column (fixed so names align).
                Text {
                    Layout.preferredWidth: 10
                    Layout.alignment: Qt.AlignVCenter
                    text: rowItem.isHead ? "*" : ""
                    color: Theme.color.accent.primary
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.sm
                    font.weight: Theme.font.weight.bold
                    renderType: Text.NativeRendering
                }

                Text {
                    Layout.fillWidth: true
                    Layout.alignment: Qt.AlignVCenter
                    // The worktree annotation rides the name text so it elides
                    // as one unit (name first to survive narrow widths).
                    text: rowItem.name
                          + (rowItem.checkedOutElsewhere
                             ? " (worktree " + rowItem.worktreeName + ")" : "")
                    color: rowItem.detached ? Theme.color.text.dim
                           : rowItem.isCurrent ? Theme.color.text.strong
                           : Theme.color.text.normal
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.sm
                    elide: Text.ElideRight
                    renderType: Text.NativeRendering
                }

                // Upstream status — lazygit's suffix vocabulary: ↑N ↓M when
                // diverged, ✓ when tracking and in sync, "gone" when the
                // upstream was deleted, nothing when no upstream configured.
                Text {
                    Layout.alignment: Qt.AlignVCenter
                    visible: rowItem.hasUpstream
                    text: rowItem.upstreamGone ? "gone"
                          : (rowItem.ahead === 0 && rowItem.behind === 0) ? "✓"
                          : (rowItem.ahead > 0 ? "↑" + rowItem.ahead : "")
                            + (rowItem.ahead > 0 && rowItem.behind > 0 ? " " : "")
                            + (rowItem.behind > 0 ? "↓" + rowItem.behind : "")
                    color: rowItem.upstreamGone ? Theme.color.mode.replace
                           : (rowItem.ahead === 0 && rowItem.behind === 0)
                             ? Theme.color.text.dim
                             : Theme.color.accent.bright
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.xs
                    renderType: Text.NativeRendering
                }

                // Age of the tip commit; the checked-out branch reads as
                // "now" so the slot is left blank (its `*` already leads).
                Text {
                    Layout.alignment: Qt.AlignVCenter
                    Layout.preferredWidth: 28
                    horizontalAlignment: Text.AlignRight
                    text: rowItem.isHead ? "" : rowItem.recency
                    color: Theme.color.text.dim
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.xs
                    renderType: Text.NativeRendering
                }
            }
        }
    }

    // Empty state — no repo or no branches yet.
    Text {
        anchors.centerIn: parent
        visible: view.count === 0
        text: "no branches"
        color: Theme.color.text.dim
        font.family: Theme.font.family
        font.pixelSize: Theme.font.size.sm
        renderType: Text.NativeRendering
    }
}
