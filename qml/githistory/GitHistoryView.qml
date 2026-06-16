// Git viewer — root of the "git" central surface.
//
// Two keyboard-first master/detail comprehension sub-views, toggled with Tab
// (and the clay tab header at the top-left):
//
//   - "changes"  — the UNCOMMITTED working tree. WorkingFileTreeView (left)
//                  drives WorkingFileDetailView (right): j/k walk the changed
//                  files — laid out as a FILE TREE so their place in the
//                  project structure is visible — and the file's working-tree
//                  diff (vs HEAD) streams in live. This is the DEFAULT view on
//                  entry — "what haven't I committed yet" is the most
//                  immediately actionable question.
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
//   - repoRoot / statusProvider / pathFilter → the FM file-tree inputs the
//     "changes" master pane (WorkingFileTreeView) needs, forwarded straight to
//     the embedded FmUi.FileTreeView (same three the Active Changes side panel
//     binds).
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

    // FM file-tree inputs for the "changes" master pane — forwarded to
    // WorkingFileTreeView's embedded FmUi.FileTreeView. Same three the Active
    // Changes side panel binds (gitController.repoRoot / gitProviderAdapter /
    // gitController.changedPathSet), set by Main.qml.
    property string repoRoot: ""
    property var statusProvider: null
    property var pathFilter: ({})

    // Bubbled up from the changes tree when the user activates a file row
    // (Enter / double-click). Main.qml routes it to open-in-nvim + surface
    // swap so "I've read this diff, take me to edit it" is one keystroke.
    signal fileActivated(string absolutePath)

    // Which sub-view is active. Default "changes" (working tree first) per the
    // surface's "what's uncommitted" framing; Tab toggles to "history".
    property string mode: "changes"

    // Move focus into the active sub-view's list (host calls this when the
    // surface becomes visible so j/k navigation is live immediately). On the
    // changes side we ALSO request the selected file's diff — this is what
    // loads the diff when the surface (re-)appears or the user toggles into
    // changes mode while a row is already selected (no currentFile change
    // fires in that case, so onCurrentFileChanged alone wouldn't cover it).
    function focusContent(): void {
        if (root.mode === "changes") {
            workingList.focusList();
            _requestCurrentDiff();
        } else {
            commitList.focusList();
        }
    }

    // Request the working-tree diff for the currently-selected changes-tree
    // file. The single gated entry point for every diff trigger: selection
    // moves (onCurrentFileChanged), worktree rescans (currentFile recomputes
    // via the tree's revision tap → onCurrentFileChanged), and surface
    // (re-)entry (focusContent). Gated on the changes view actually being on
    // screen so background scans while editing on another surface don't spawn
    // idle single-file `git diff`s for a hidden pane.
    function _requestCurrentDiff(): void {
        if (root.visible && root.mode === "changes"
                && root.logController && workingList.currentFile)
            root.logController.request_working_diff(
                workingList.currentFile.displayName,
                workingList.currentFile.untracked);
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

    // Re-home keyboard focus whenever the active sub-view changes, so the focus
    // invariant ("the visible list owns the keys") holds no matter how `mode`
    // was mutated — not just via setMode/toggleMode. setMode still calls
    // focusContent() directly so a click on the ALREADY-active tab also re-homes
    // focus (a no-op mode set fires no onModeChanged); the double-call when mode
    // does change is an idempotent forceActiveFocus.
    onModeChanged: focusContent()

    // NOTE: the flat-list era had a separate `Connections { target: statusModel;
    // onModelReset → request_working_diff }` here (seal finding #1) because that
    // list's `currentFile` only changed on a numstat role move, so an in-place
    // edit leaving add/del counts identical left the diff stale. The tree's
    // `currentFile` instead recomputes on EVERY scan (its `_statusRevision`
    // tap), so it fires `onCurrentFileChanged` on every reset and the gap is
    // closed through the single `_requestCurrentDiff` path — no separate
    // reset-Connection needed (and re-adding one would double-request).

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

                WorkingFileTreeView {
                    id: workingList
                    anchors.fill: parent
                    focus: root.mode === "changes"

                    // The FM file-tree inputs (forwarded from the host's
                    // injected props) + the status model for the clean-tree
                    // empty state and the currentFile recompute trigger.
                    repoRoot: root.repoRoot
                    statusProvider: root.statusProvider
                    pathFilter: root.pathFilter
                    model: root.statusModel

                    // Selection moved (j/k) OR the worktree was re-scanned
                    // (the tree's revision tap recomputes currentFile) → load
                    // the selected file's diff through the single gated path.
                    onCurrentFileChanged: root._requestCurrentDiff()
                    onToggleRequested: root.toggleMode()
                    onFileActivated: (path) => root.fileActivated(path)
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
