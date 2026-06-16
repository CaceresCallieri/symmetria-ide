// Git history viewer — root of the "git" central surface.
//
// A keyboard-first master/detail comprehension surface: the commit list (left)
// drives the commit detail + diff (right). Selection-move in the list auto-
// requests that commit's diff, so walking history with j/k streams the diffs
// live — the surface's whole reason to exist (shrink the "cognitive gap" left
// when agents author the commits).
//
// Binds to INJECTED `logController` / `logModel` (set by Main.qml from the
// `gitLogController` / `gitLogModel` context properties), never globals — so
// the subtree lifts cleanly into a future standalone Symmetria.Git.UI module.

import QtQuick
import QtQuick.Layouts
import "../design"

FocusScope {
    id: root

    // Injected by the host.
    property var logController: null
    property var logModel: null

    // Move focus into the list (host calls this when the surface becomes
    // visible so j/k navigation is live immediately).
    function focusContent(): void {
        commitList.focusList();
    }

    // Two framed "columns" separated by a breathing gap — the File Manager's
    // miller-column rhythm. The old hard hairline divider is replaced by a
    // gap + per-pane rounded frame so the split reads as two calm surfaces
    // rather than two flat halves butted together.
    RowLayout {
        anchors.fill: parent
        anchors.margins: Theme.spacing.sm
        spacing: Theme.spacing.sm

        // --- Master: commit list ----------------------------------------
        Rectangle {
            Layout.preferredWidth: Math.round(root.width * 0.42)
            Layout.minimumWidth: 320
            Layout.fillHeight: true
            color: Theme.color.bg.chrome
            radius: Theme.radius.md
            border.width: 1
            border.color: Theme.color.border.hairline

            CommitListView {
                id: commitList
                anchors.fill: parent
                model: root.logModel
                controller: root.logController
                focus: true

                // Selection moved → load that commit's diff. `currentCommit`
                // re-evaluates (and fires this) whenever the highlighted row
                // swaps, including the first populate after a repo switch.
                onCurrentCommitChanged: {
                    if (root.logController && commitList.currentCommit)
                        root.logController.request_diff(commitList.currentCommit.hash);
                }
            }
        }

        // --- Detail: metadata + diff ------------------------------------
        // Matching rounded frame — `radius`/`border` set on the instance
        // (CommitDetailView's root is a Rectangle) so it pairs visually with
        // the master column.
        CommitDetailView {
            Layout.fillWidth: true
            Layout.fillHeight: true
            radius: Theme.radius.md
            border.width: 1
            border.color: Theme.color.border.hairline
            commit: commitList.currentCommit
            diffText: root.logController ? root.logController.currentDiffText : ""
            diffHash: root.logController ? root.logController.currentDiffHash : ""
        }
    }
}
