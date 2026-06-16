// Working-file detail — the "detail" pane of the uncommitted-files sub-view.
//
// The Tab-toggle counterpart to CommitDetailView. Shows the selected
// uncommitted file's identity (path + status + ±counts, from the injected
// `file` object surfaced by WorkingFileListView.currentFile) plus its
// working-tree diff. The diff is loaded asynchronously by GitLogController via
// `request_working_diff`; we render it only once `diffPath === file.displayName`
// so a stale diff for the previously-selected file never shows during the
// async gap — the path is the working-tree analogue of CommitDetailView's
// hash-match guard.
//
// The diff body is the shared DiffView (same colored-line renderer the commit
// detail uses); this pane only supplies the metadata header + the diff text and
// its readiness guard. Root is a Rectangle so the host (GitHistoryView) can set
// `radius`/`border` on the instance to frame it as a clay column, exactly as it
// does for CommitDetailView.

import QtQuick
import QtQuick.Layouts
import ".."
import "../design"

Rectangle {
    id: root

    // Selected file metadata (or null). Set by the host from the list's
    // `currentFile` — carries path / displayName / statusChar / statusState /
    // tooltip / additions / deletions / untracked.
    property var file: null
    // Diff plumbing — bound to GitLogController's currentFileDiff* properties.
    property string diffText: ""
    property string diffPath: ""

    color: Theme.color.bg.chrome

    readonly property bool hasFile: root.file !== null
    // Path-match guard: the published diff is current only when its path equals
    // the selected file's repo-relative path (mirrors CommitDetailView's
    // hash-match). Until then DiffView shows "loading…", never a stale patch.
    readonly property bool diffReady: hasFile && root.diffPath === root.file.displayName

    // Empty state — nothing selected (clean tree, or first paint).
    Text {
        anchors.centerIn: parent
        visible: !root.hasFile
        text: "select a file to view its changes"
        color: Theme.color.text.dim
        font.family: Theme.font.family
        font.pixelSize: Theme.font.size.sm
        renderType: Text.NativeRendering
    }

    ColumnLayout {
        anchors.fill: parent
        visible: root.hasFile
        spacing: 0

        // --- Metadata header (clay card) --------------------------------
        // The file's identity floats as a clay PillCard above the diff — the
        // same framed-header read CommitDetailView uses. Inset on all four
        // sides so the card's convex shadow has room (it renders OUTSIDE the
        // rect) and its downward drop lifts the header off the changes below.
        PillCard {
            Layout.fillWidth: true
            Layout.leftMargin: Theme.spacing.sm
            Layout.rightMargin: Theme.spacing.sm
            Layout.topMargin: Theme.spacing.sm
            Layout.bottomMargin: Theme.spacing.sm
            Layout.preferredHeight: headerCol.implicitHeight + Theme.spacing.md * 2
            radius: Theme.radius.md
            elevated: true
            color: Theme.color.bg.selected

            Column {
                id: headerCol
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.margins: Theme.spacing.md
                spacing: Theme.spacing.xs

                // File path (repo-relative) — the row's identity.
                Text {
                    width: parent.width
                    text: root.hasFile ? root.file.displayName : ""
                    color: Theme.color.text.emphasis
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.md
                    font.weight: Theme.font.weight.bold
                    wrapMode: Text.WrapAnywhere
                    renderType: Text.NativeRendering
                }

                // Status label + line-count deltas — the "what kind of change
                // and how big" line. Status word tinted by state; +adds green,
                // −dels red, reusing the diff palette.
                Row {
                    width: parent.width
                    spacing: Theme.spacing.sm

                    Text {
                        text: root.hasFile ? root.file.tooltip : ""
                        // Badge colour resolved once by WorkingFileListView and
                        // carried on `currentFile` — no duplicate state→colour map.
                        color: root.hasFile ? root.file.stateColor : Theme.color.text.dim
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.xs
                        font.weight: Theme.font.weight.medium
                        renderType: Text.NativeRendering
                    }

                    Text {
                        visible: root.hasFile && root.file.additions > 0
                        text: root.hasFile ? "+" + root.file.additions : ""
                        color: Theme.color.diff.addedFg
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.xs
                        renderType: Text.NativeRendering
                    }

                    Text {
                        visible: root.hasFile && root.file.deletions > 0
                        text: root.hasFile ? "-" + root.file.deletions : ""
                        color: Theme.color.diff.removedFg
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.xs
                        renderType: Text.NativeRendering
                    }
                }
            }
        }

        // --- Diff body (shared renderer) ---------------------------------
        // `emptyText` is phrased for the working-tree case — a tracked file
        // with no net change vs HEAD (e.g. staged then reverted) lands here.
        DiffView {
            Layout.fillWidth: true
            Layout.fillHeight: true
            active: root.hasFile
            ready: root.diffReady
            diffText: root.diffText
            emptyText: "no textual changes (binary, mode-only, or already matching HEAD)"
        }
    }
}
