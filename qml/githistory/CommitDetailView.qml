// Commit detail — the "detail" pane of the git-history viewer.
//
// Shows the selected commit's metadata (from the injected `commit` object,
// surfaced by CommitListView.currentCommit) plus its diff. The diff text is
// loaded asynchronously by the controller; we render it only once
// `diffHash === commit.hash` so a stale diff for the previously-selected
// commit never shows during the async gap.
//
// The diff itself is rendered by the shared `DiffView` (extracted so the
// uncommitted-file detail pane renders patches identically): a virtualized
// ListView over the patch's lines, each classified by its leading character
// and coloured via the shared `Theme.color.diff.*` tokens (the same palette
// the agent pane's tool_diff rows use). This pane only supplies the metadata
// header + the diff text and its readiness guard.

import QtQuick
import QtQuick.Layouts
import ".."
import "../design"

Rectangle {
    id: root

    // Selected commit metadata (or null). Set by the host from the list's
    // `currentCommit`.
    property var commit: null
    // Diff plumbing — bound to the controller's currentDiff* properties.
    property string diffText: ""
    property string diffHash: ""

    color: Theme.color.bg.chrome

    readonly property bool hasCommit: root.commit !== null
    readonly property bool diffReady: hasCommit && root.diffHash === root.commit.hash

    // Empty state — nothing selected.
    Text {
        anchors.centerIn: parent
        visible: !root.hasCommit
        text: "select a commit to view its changes"
        color: Theme.color.text.dim
        font.family: Theme.font.family
        font.pixelSize: Theme.font.size.sm
        renderType: Text.NativeRendering
    }

    ColumnLayout {
        anchors.fill: parent
        visible: root.hasCommit
        spacing: 0

        // --- Metadata header (clay card) --------------------------------
        // The commit's identity floats as a clay PillCard above the diff —
        // the File Manager's framed-header read. Inset with margins so the
        // card's convex shadow has room (it renders OUTSIDE the rect) and its
        // downward drop lifts the header off the changes below. The header
        // Column reparents into the card body (PillCard's default content slot).
        PillCard {
            Layout.fillWidth: true
            Layout.leftMargin: Theme.spacing.sm
            Layout.rightMargin: Theme.spacing.sm
            Layout.topMargin: Theme.spacing.sm
            // Bottom margin too, so the card's downward (card-preset) shadow
            // has clearance before the diff below — without it the next
            // ColumnLayout sibling paints over the drop and the "floats above
            // the changes" cue is lost. Symmetric with the other three sides.
            Layout.bottomMargin: Theme.spacing.sm
            Layout.preferredHeight: headerCol.implicitHeight + Theme.spacing.md * 2
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
                    text: root.hasCommit ? root.commit.subject : ""
                    color: Theme.color.text.emphasis
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.md
                    font.weight: Theme.font.weight.bold
                    wrapMode: Text.Wrap
                    renderType: Text.NativeRendering
                }

                Text {
                    width: parent.width
                    text: root.hasCommit
                        ? root.commit.abbrevHash + "  ·  "
                          + root.commit.authorName + " <" + root.commit.authorEmail + ">"
                          + "  ·  " + root.commit.relativeDate
                        : ""
                    color: Theme.color.text.dim
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.xs
                    wrapMode: Text.Wrap
                    renderType: Text.NativeRendering
                }

                Text {
                    width: parent.width
                    visible: root.hasCommit && root.commit.refs.length > 0
                    text: root.hasCommit ? root.commit.refs : ""
                    color: Theme.color.accent.bright
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.xs
                    wrapMode: Text.Wrap
                    renderType: Text.NativeRendering
                }

                Text {
                    width: parent.width
                    visible: root.hasCommit && root.commit.body.length > 0
                    text: root.hasCommit ? root.commit.body : ""
                    color: Theme.color.text.normal
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.sm
                    wrapMode: Text.Wrap
                    topPadding: Theme.spacing.xs
                    renderType: Text.NativeRendering
                }
            }
        }

        // --- Diff body (shared renderer) ---------------------------------
        // The colored unified-diff list now lives in DiffView, reused by both
        // this commit-detail pane and the uncommitted-file detail pane. The
        // host owns the async-gap guard: `ready: root.diffReady` (hash match)
        // suppresses a stale prior patch while the new commit's diff streams in.
        DiffView {
            Layout.fillWidth: true
            Layout.fillHeight: true
            active: root.hasCommit
            ready: root.diffReady
            diffText: root.diffText
            emptyText: "no textual diff (merge commit, or no content change)"
        }
    }
}
