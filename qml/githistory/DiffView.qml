// Shared colored unified-diff renderer for the git-history surface.
//
// Extracted from CommitDetailView so BOTH detail panes — the commit diff and
// the uncommitted-file diff (WorkingFileDetailView) — render patches the same
// way: a virtualized ListView over the patch's lines, each classified by its
// leading marker and tinted via the shared `Theme.color.diff.*` tokens (the
// same palette the agent pane's tool_diff rows use). Lines wrap rather than
// clip so no content is lost; horizontal scroll with no-wrap is a future
// refinement (tracked identically for both consumers now that the logic lives
// in one place).
//
// The renderer is intentionally state-agnostic — it knows nothing about
// commits vs working-tree files. The host supplies three booleans/strings:
//   - `active`  — a target (commit / file) is selected at all.
//   - `ready`   — `diffText` is current FOR that target (the host's own
//                 async-gap guard: hash-match for commits, path-match for
//                 files). While a target is active but its diff hasn't landed,
//                 we show `loadingText` rather than a stale previous patch.
//   - `diffText`— the unified-diff body.
// `loadingText` / `emptyText` let each host phrase the two non-content states
// in its own vocabulary ("merge commit" vs "no working-tree changes").

import QtQuick
import "../design"

Item {
    id: root

    // The diff body (patch only, no commit header). Empty until the host's
    // controller publishes it.
    property string diffText: ""
    // A target is selected (commit row / file row). When false the host owns
    // the empty state ("select a commit…") and this renderer stays blank.
    property bool active: false
    // `diffText` is current for the active target — the host computes this as
    // its async-gap guard (commit hash match / file path match) so a stale
    // diff for the previously-selected target never flashes during the fetch.
    property bool ready: false
    // Per-host copy for the two transient states.
    property string loadingText: "loading diff…"
    property string emptyText: "no textual diff"

    // Split lazily, and only once the diff is both active and current — an
    // inactive or not-yet-ready target keeps this empty so no rows render.
    readonly property var diffLines: root.active && root.ready && root.diffText.length > 0
        ? root.diffText.split("\n")
        : []

    // --- Transient states (anchored top-left, mirroring the old in-flow
    // placement directly beneath the metadata header) ----------------------
    Text {
        anchors.left: parent.left
        anchors.top: parent.top
        anchors.margins: Theme.spacing.md
        visible: root.active && !root.ready
        text: root.loadingText
        color: Theme.color.text.dim
        font.family: Theme.font.family
        font.pixelSize: Theme.font.size.sm
        renderType: Text.NativeRendering
    }

    Text {
        anchors.left: parent.left
        anchors.top: parent.top
        anchors.margins: Theme.spacing.md
        visible: root.active && root.ready && root.diffLines.length === 0
        text: root.emptyText
        color: Theme.color.text.dim
        font.family: Theme.font.family
        font.pixelSize: Theme.font.size.sm
        renderType: Text.NativeRendering
    }

    // --- Diff body ---------------------------------------------------------
    ListView {
        id: diffView
        anchors.fill: parent
        visible: root.active && root.ready && root.diffLines.length > 0
        clip: true
        model: root.diffLines
        cacheBuffer: 800
        boundsBehavior: Flickable.StopAtBounds
        // Reset to the top whenever a new target's diff lands.
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
