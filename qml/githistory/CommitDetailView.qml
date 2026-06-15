// Commit detail — the "detail" pane of the git-history viewer.
//
// Shows the selected commit's metadata (from the injected `commit` object,
// surfaced by CommitListView.currentCommit) plus its diff. The diff text is
// loaded asynchronously by the controller; we render it only once
// `diffHash === commit.hash` so a stale diff for the previously-selected
// commit never shows during the async gap.
//
// The diff is rendered as a virtualized ListView over the patch's lines, each
// classified by its leading character and coloured via the shared
// `Theme.color.diff.*` tokens (the same palette the agent pane's tool_diff
// rows use). Lines wrap (rather than clip) so no content is lost — horizontal
// scroll with no-wrap is a future refinement.

import QtQuick
import QtQuick.Layouts
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
    readonly property var diffLines: diffReady && root.diffText.length > 0
        ? root.diffText.split("\n")
        : []

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

        // --- Metadata header --------------------------------------------
        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: headerCol.implicitHeight + Theme.spacing.md * 2
            color: Theme.color.bg.selected

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

        // --- Diff body ---------------------------------------------------
        Text {
            Layout.fillWidth: true
            Layout.margins: Theme.spacing.md
            visible: root.hasCommit && !root.diffReady
            text: "loading diff…"
            color: Theme.color.text.dim
            font.family: Theme.font.family
            font.pixelSize: Theme.font.size.sm
            renderType: Text.NativeRendering
        }

        Text {
            Layout.fillWidth: true
            Layout.margins: Theme.spacing.md
            visible: root.diffReady && root.diffLines.length === 0
            text: "no textual diff (merge commit, or no content change)"
            color: Theme.color.text.dim
            font.family: Theme.font.family
            font.pixelSize: Theme.font.size.sm
            renderType: Text.NativeRendering
        }

        ListView {
            id: diffView
            Layout.fillWidth: true
            Layout.fillHeight: true
            visible: root.diffReady && root.diffLines.length > 0
            clip: true
            model: root.diffLines
            cacheBuffer: 800
            boundsBehavior: Flickable.StopAtBounds
            // Reset to the top whenever a new commit's diff lands.
            onModelChanged: positionViewAtBeginning()

            delegate: Rectangle {
                id: lineItem
                required property string modelData
                readonly property string kind: root._lineKind(modelData)
                width: ListView.view.width
                implicitHeight: lineText.implicitHeight
                height: implicitHeight
                color: lineItem.kind === "add" ? Theme.color.diff.addedBg
                     : lineItem.kind === "del" ? Theme.color.diff.removedBg
                     : lineItem.kind === "hunk" ? Theme.color.diff.hunkBg
                     : "transparent"

                Text {
                    id: lineText
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.leftMargin: Theme.spacing.sm
                    anchors.rightMargin: Theme.spacing.sm
                    text: lineItem.modelData.length > 0 ? lineItem.modelData : " "
                    color: lineItem.kind === "add" ? Theme.color.diff.addedFg
                         : lineItem.kind === "del" ? Theme.color.diff.removedFg
                         : lineItem.kind === "hunk" ? Theme.color.diff.hunkFg
                         : lineItem.kind === "meta" ? Theme.color.text.dim
                         : Theme.color.diff.contextFg
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.sm
                    wrapMode: Text.WrapAnywhere
                    textFormat: Text.PlainText
                    renderType: Text.NativeRendering
                }
            }
        }
    }

    // Classify one unified-diff line by its leading marker. Order matters:
    // the "+++"/"---" file headers must be caught as meta BEFORE the bare
    // +/- added/removed checks.
    function _lineKind(line: string): string {
        if (line.startsWith("@@"))
            return "hunk";
        if (line.startsWith("+++") || line.startsWith("---")
                || line.startsWith("diff ") || line.startsWith("index ")
                || line.startsWith("new file") || line.startsWith("deleted file")
                || line.startsWith("similarity ") || line.startsWith("rename ")
                || line.startsWith("old mode") || line.startsWith("new mode"))
            return "meta";
        if (line.startsWith("+"))
            return "add";
        if (line.startsWith("-"))
            return "del";
        return "context";
    }
}
