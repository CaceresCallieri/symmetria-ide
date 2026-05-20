// Active Changes panel — a path-filtered FileTreeView of pending git
// changes.
//
// Sits ABOVE the main FileTreeView in the side panel column. Auto-hidden
// when the working tree is clean or we're not in a git repo
// (model.count == 0). The body is an embedded `FmUi.FileTreeView` whose
// `pathFilter` prop restricts visible rows to the set of currently-changed
// paths (plus their ancestors up to the repo root). The tree is
// always-expanded by default — `initialExpandDepth: -1` with the FM's
// existing caps (`maxExpandDepth: 8`, `_autoExpandModelCeiling: 100`,
// `_autoExpandFanoutCap: 200`) bounding the worst case.
//
// Each row carries a small status badge (M/A/D/?/U/R/C) via the FM's
// existing `statusProvider` extension point AND an inline `+adds -dels`
// accessory when the provider supplies non-zero counts. Clicking a file
// row opens that file in nvim — the panel's `fileActivated` signal
// carries the absolute path, which Main.qml routes to
// `controller.open_in_nvim(path)` (same path the main FileTreeView uses
// for `onFileActivated`). Clicking a directory row toggles its
// expand/collapse state via the FM's built-in handler.
//
// Visual hierarchy: the chrome wrapper uses the IDE's Theme palette
// (`Theme.color.bg.chrome`, hairline border) so it reads continuous with
// the status bar and other chrome panes. Per-row visuals come from the
// FM's FmTheme — the same palette the main file tree uses, so the two
// trees are visually unified by construction.
//
// Known v1 limitation: focus competes between this tree and the main
// FileTreeView below. Both have `focus: true` on their inner ListView;
// the first to call `forceActiveFocus()` on Component.onCompleted wins
// at startup. v1 accepts this — clicks resolve focus correctly. v2 plan:
// FocusScope wrappers + controller-side focused-pane state routed
// through Ctrl+H/Ctrl+L chords (the same mechanism used elsewhere in
// `Main.qml`).

import QtQuick
import QtQuick.Layouts
import Symmetria.FileManager.UI as FmUi
import "design"

Item {
    id: root

    // Backing model exposed via the `gitStatusList` context property — a
    // flat `GitStatusListModel`. Still used here ONLY for the header
    // file-count display (`model.count`); the embedded tree pulls its
    // rendering from the FM's FileSystemModel + pathFilter gate.
    property QtObject model: null

    // Header-bucket aggregates exposed by `GitController.stats`
    // (QVariantMap). Each bucket carries adds / dels / file-count; the
    // header repeater binds against these. Default `{}` lets us render
    // gracefully on first paint before the worker has produced numbers.
    property var stats: ({})

    // Absolute path to the git repo root. Drives the embedded
    // FileTreeView's `rootPath`. Empty string short-circuits via the
    // outer `visible:` guard since the panel auto-hides on clean trees.
    property string repoRoot: ""

    // FM duck-typed status seam — `statusForPath(absPath) -> {char, color,
    // tooltip, adds, dels}` or null. Forwarded straight to the embedded
    // FileTreeView. Same instance as the main tree below uses
    // (`gitProviderAdapter` in Main.qml).
    property var statusProvider: null

    // Absolute-path membership map driving the FM's `pathFilter`. Built
    // by `GitController.changedPathSet`; covers rootPath + every changed
    // leaf + every ancestor. Default `{}` keeps the embedded tree
    // empty-but-valid on first paint.
    property var pathFilter: ({})

    // Emitted when the user clicks a file row. Carries the ABSOLUTE
    // filesystem path of the activated file. Main.qml connects this to
    // `controller.open_in_nvim(path)` and then re-focuses the editor.
    signal fileActivated(string absolutePath)

    // Auto-hide when there are no changes. Hidden state collapses the
    // vertical real estate so the main file tree below claims it back.
    visible: model && model.count > 0
    // Include the asymmetric top+bottom margins so the chrome Rectangle
    // matches the actual content layout. Without the `+ topMargin +
    // bottomMargin`, ColumnLayout inside this Item gets anchors.fill with
    // margins which leaves it `2*margin` less tall than its content wants.
    implicitHeight: visible
        ? content.implicitHeight + Theme.spacing.sm * 2
        : 0
    Layout.preferredHeight: implicitHeight
    Layout.fillWidth: true

    // Chrome — same matte tone the status bar and which-key overlay use.
    // Drops slightly darker than the (transparent) main file tree below
    // to separate visually.
    Rectangle {
        anchors.fill: parent
        color: Theme.color.bg.chrome
        border.width: 1
        border.color: Theme.color.border.hairline
    }

    ColumnLayout {
        id: content
        anchors.fill: parent
        anchors.topMargin: Theme.spacing.sm
        anchors.bottomMargin: Theme.spacing.sm
        anchors.leftMargin: Theme.spacing.xs
        anchors.rightMargin: Theme.spacing.xs
        spacing: Theme.spacing.xs

        // Section header — quiet label + three aggregate bucket rows
        // (staged ●, unstaged ○, untracked ✦) carrying +adds -dels (n).
        // Bucket rows hide themselves when their file count is 0, so a
        // clean staging area collapses to just the title.
        ColumnLayout {
            Layout.fillWidth: true
            spacing: Theme.spacing.xxs

            Text {
                Layout.fillWidth: true
                text: "Changes" + (root.model ? " · " + root.model.count : "")
                color: Theme.color.text.dim
                font.family: Theme.font.family
                font.pixelSize: Theme.font.size.xs
            }

            Repeater {
                // Colors carried inline on each bucket item — the panel
                // no longer needs a `_colorForState` helper since the
                // per-row badge palette now lives entirely inside the
                // embedded FileTreeView (via `statusProvider`). Three
                // hardcoded lookups against `FmUi.FmTheme.gitStatus` keep
                // the bucket header visually unified with the row badges.
                model: [
                    {icon: "●", color: FmUi.FmTheme.gitStatus.stagedGreen,
                     add: (root.stats && root.stats.stagedAdd) || 0,
                     del: (root.stats && root.stats.stagedDel) || 0,
                     n:   (root.stats && root.stats.stagedFiles) || 0},
                    {icon: "○", color: FmUi.FmTheme.gitStatus.unstagedRed,
                     add: (root.stats && root.stats.unstagedAdd) || 0,
                     del: (root.stats && root.stats.unstagedDel) || 0,
                     n:   (root.stats && root.stats.unstagedFiles) || 0},
                    {icon: "✦", color: FmUi.FmTheme.gitStatus.untrackedBlue,
                     add: (root.stats && root.stats.untrackedLines) || 0,
                     del: 0,
                     n:   (root.stats && root.stats.untrackedCount) || 0},
                ]
                delegate: RowLayout {
                    visible: modelData.n > 0
                    spacing: Theme.spacing.xs

                    Text {
                        text: modelData.icon
                        color: modelData.color
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.xs
                    }
                    Text {
                        visible: modelData.add > 0
                        text: "+" + modelData.add
                        color: Theme.color.diff.addedFg
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.xs
                    }
                    Text {
                        visible: modelData.del > 0
                        text: "-" + modelData.del
                        color: Theme.color.diff.removedFg
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.xs
                    }
                    Text {
                        text: "(" + modelData.n + ")"
                        color: Theme.color.text.dim
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.xs
                    }
                    Item { Layout.fillWidth: true }
                }
            }
        }

        // The tree-shaped list of changed files. Reuses the FM's
        // FileTreeView with `pathFilter` narrowing visible rows to the
        // current changeset. Height is content-fit: we don't set
        // `Layout.preferredHeight`, so the Layout falls back to the
        // FileTreeView's own `implicitHeight` (FM-side: tracks
        // `view.contentHeight`). The pane grows with the changeset —
        // no internal scrollbar, no fixed cap. A 4-file changeset
        // takes ~5 rows of vertical space; a 50-file changeset takes
        // ~60 rows. The main FileTreeView below uses `Layout.fillHeight`
        // and is unaffected — Layouts override implicit height when
        // fillHeight is set.
        //
        // `respectGitignore: false` is deliberate — users genuinely want
        // to see force-added gitignored files (e.g. a build artifact
        // added with `git add -f`); hiding them would lie about the
        // working-tree state.
        //
        // `compactScale: 0.75` makes rows tighter than the main tree
        // below — the changes pane benefits from packing more rows
        // into the vertical space it claims.
        FmUi.FileTreeView {
            id: changesTree
            Layout.fillWidth: true

            rootPath: root.repoRoot
            initialExpandDepth: -1
            respectGitignore: false
            compactScale: 0.75
            statusProvider: root.statusProvider
            pathFilter: root.pathFilter

            onFileActivated: (path) => root.fileActivated(path)
        }
    }
}
