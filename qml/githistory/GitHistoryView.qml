// Git viewer — root of the "git" central surface.
//
// Two keyboard-first master/detail comprehension sub-views, toggled with Tab
// (and the clay tab header at the top-left):
//
//   - "changes"  — the UNCOMMITTED working tree. WorkingFileListView (left)
//                  drives WorkingFileDetailView (right): j/k walk the changed
//                  files, the file's working-tree diff (vs HEAD) streams in
//                  live. This is the DEFAULT view on entry — "what haven't I
//                  committed yet" is the most immediately actionable question.
//   - "history"  — the committed log. CommitListView (left) drives
//                  CommitDetailView (right): j/k walk commits, each commit's
//                  diff streams in. The long view of "how did we get here".
//
// Both answer the same need from opposite ends — shrinking the cognitive gap
// agent-authored changes open up (you didn't type them, so comprehension has
// to be reconstructed). The working-tree view closes the gap on changes still
// in flight; the history view on changes already landed.
//
// Tab handling: each list emits `toggleRequested()` from its own
// Keys.onPressed (Tab is Qt's focus-traversal key, so it must be intercepted
// and accepted where focus lives), and this host flips `mode` + re-homes focus.
//
// Binds to INJECTED controllers/models (set by Main.qml from context
// properties), never globals — so the subtree lifts cleanly into a future
// standalone Symmetria.Git.UI module:
//   - logController / logModel  → committed history + commit diff (GitLogController)
//   - statusModel               → live working-tree file list (GitStatusListModel)
// The working-tree DIFF is also served by logController (request_working_diff +
// currentFileDiff* properties) — GitLogController owns every read-only git
// query for this surface; statusModel owns only the file list.

import QtQuick
import QtQuick.Layouts
import ".."
import "../design"

FocusScope {
    id: root

    // Injected by the host.
    property var logController: null
    property var logModel: null
    property var statusModel: null

    // Which sub-view is active. Default "changes" (working tree first) per the
    // surface's "what's uncommitted" framing; Tab toggles to "history".
    property string mode: "changes"

    // Move focus into the active sub-view's list (host calls this when the
    // surface becomes visible so j/k navigation is live immediately).
    function focusContent(): void {
        if (root.mode === "changes")
            workingList.focusList();
        else
            commitList.focusList();
    }

    // Set the active sub-view and re-home focus onto its list. Used by both the
    // Tab toggle and the clay tab-header clicks. Re-focusing even on a no-op
    // mode set means a tab click always lands keyboard focus on the list.
    function setMode(next: string): void {
        if (next !== root.mode)
            root.mode = next;
        focusContent();
    }

    function toggleMode(): void {
        setMode(root.mode === "changes" ? "history" : "changes");
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: Theme.spacing.sm
        spacing: Theme.spacing.sm

        // --- Tab header (clay two-segment switcher) ----------------------
        // Same active-tab idiom as AgentTopBar's surface switcher: the active
        // sub-view raises into a clay capsule, the inactive one stays flat. The
        // "changes" segment carries a live file count so the scope of
        // uncommitted work reads at a glance without entering the view.
        Row {
            id: tabHeader
            Layout.preferredHeight: Theme.size.modeBadgeHeight
            spacing: Theme.spacing.sm

            Repeater {
                model: [
                    { mode: "changes", label: "changes" },
                    { mode: "history", label: "history" },
                ]

                delegate: Item {
                    id: seg
                    required property var modelData
                    readonly property bool isCurrent: root.mode === seg.modelData.mode

                    height: Theme.size.modeBadgeHeight
                    implicitWidth: segLabel.implicitWidth + Theme.spacing.md * 2

                    PillSurface {
                        anchors.fill: parent
                        radius: height / 2
                        elevated: seg.isCurrent
                        color: seg.isCurrent ? Theme.color.bg.selected : "transparent"
                        borderColor: seg.isCurrent ? Theme.color.border.hairline : "transparent"
                    }

                    Text {
                        id: segLabel
                        anchors.centerIn: parent
                        text: seg.modelData.label
                              + (seg.modelData.mode === "changes"
                                 && root.statusModel && root.statusModel.count > 0
                                 ? " · " + root.statusModel.count : "")
                        color: seg.isCurrent ? Theme.color.text.strong : Theme.color.text.dim
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.xs
                        font.weight: seg.isCurrent ? Theme.font.weight.bold : Theme.font.weight.medium
                        font.letterSpacing: 0.6
                        renderType: Text.NativeRendering
                    }

                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: root.setMode(seg.modelData.mode)
                    }
                }
            }
        }

        // --- "changes": uncommitted working tree ------------------------
        // Two framed clay columns separated by a breathing gap — the File
        // Manager miller-column rhythm. Only the active mode's RowLayout is
        // visible; an invisible Layout child is excluded from sizing, so the
        // visible one claims all remaining height.
        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            visible: root.mode === "changes"
            spacing: Theme.spacing.sm

            // Master: changed-file list.
            Rectangle {
                Layout.preferredWidth: Math.round(root.width * 0.42)
                Layout.minimumWidth: 320
                Layout.fillHeight: true
                color: Theme.color.bg.chrome
                radius: Theme.radius.md
                border.width: 1
                border.color: Theme.color.border.hairline

                WorkingFileListView {
                    id: workingList
                    anchors.fill: parent
                    model: root.statusModel
                    focus: root.mode === "changes"

                    // Selection moved → load that file's working-tree diff.
                    // `currentFile` re-evaluates (and fires this) whenever the
                    // highlighted row swaps OR its numstat changes on a live
                    // worktree edit, so the diff tracks edits without a manual
                    // refresh. `untracked` selects the diff command.
                    onCurrentFileChanged: {
                        if (root.logController && workingList.currentFile)
                            root.logController.request_working_diff(
                                workingList.currentFile.displayName,
                                workingList.currentFile.untracked);
                    }
                    onToggleRequested: root.toggleMode()
                }
            }

            // Detail: file metadata + working-tree diff. radius/border on the
            // instance (its root is a Rectangle) so it pairs with the master.
            WorkingFileDetailView {
                Layout.fillWidth: true
                Layout.fillHeight: true
                radius: Theme.radius.md
                border.width: 1
                border.color: Theme.color.border.hairline
                file: workingList.currentFile
                diffText: root.logController ? root.logController.currentFileDiffText : ""
                diffPath: root.logController ? root.logController.currentFileDiffPath : ""
            }
        }

        // --- "history": committed log -----------------------------------
        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            visible: root.mode === "history"
            spacing: Theme.spacing.sm

            // Master: commit list.
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
                    focus: root.mode === "history"

                    // Selection moved → load that commit's diff.
                    onCurrentCommitChanged: {
                        if (root.logController && commitList.currentCommit)
                            root.logController.request_diff(commitList.currentCommit.hash);
                    }
                    onToggleRequested: root.toggleMode()
                }
            }

            // Detail: commit metadata + diff.
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
}
