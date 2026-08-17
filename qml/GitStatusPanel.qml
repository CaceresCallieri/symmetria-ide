// Active Changes panel — a path-filtered FileTreeView of pending git changes.
//
// The CHANGES tab of the side panel. It shared that column vertically with the
// main FileTreeView until 2026-08-15, when the two became tabs — so this panel
// now owns the full column height whenever its tab is current, and the host
// (not the panel) decides when that is. On a clean working tree, or outside a
// git repo, it draws a quiet empty state rather than hiding: a tab body that
// vanishes under a tab header that stays is worse than one that says nothing.
// The body is an embedded `FmUi.FileTreeView` whose `pathFilter` restricts visible
// rows to the repo's changed paths (plus ancestors up to the root). It is
// always-expanded by default — `initialExpandDepth: -1` with the FM's caps
// (`maxExpandDepth: 8`, `_autoExpandModelCeiling: 100`,
// `_autoExpandFanoutCap: 200`) bounding it.
//
// The panel showed ONE changeset until 2026-07-21 and does again: a per-agent
// "all | this agent" scope, its foreign-repo sections, and the Flickable that
// stacked them were removed on 2026-08-13. Attributing a working-tree change to
// a specific agent could not be inferred reliably, and git hygiene (one worktree
// or one branch per agent) makes the attribution structural instead of guessed —
// the worktree follow already re-roots this panel onto the focused agent's
// worktree, which is the replacement. Do not rebuild the scope switcher here.
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
// (`Theme.color.bg.bar`, hairline border) — the rung the whole side-panel
// column shares with the two chrome bars, so this panel and the file tree
// below it read as one column and the column reads as one piece of chrome.
// It sat on `bg.chrome` until 2026-08-13; the reason it moved is recorded
// once, in the ladder note in `qml/design/Theme.qml`. Per-row visuals come
// from the FM's FmTheme — the same palette the main file tree uses, so the
// two trees are visually unified by construction.
//
// Keyboard navigation: sub-pane parity with the main FileTreeView.
//
// The embedded `FmUi.FileTreeView` owns a comprehensive `Keys.onPressed`
// handler on its inner ListView — j/k/h/l, Ctrl+D / Ctrl+U half-page
// scroll, Return/Enter activation, gg/G jump-to-end, `/` search,
// `s` flash. ALL of those keys work the moment focus reaches that
// ListView. The `onFileActivated` signal Main.qml binds to is the
// SAME signal the main FileTreeView below emits, so an Enter
// keystroke in either tree ends up calling `controller.open_in_nvim`
// via an identical path — no per-tree handler divergence.
//
// Focus reachability is wired through three coordinated surfaces:
//   1. FM `FileTreeView.focusInternal()` (cross-repo) — public
//      function that delegates `forceActiveFocus()` to the FM's
//      internal `view` ListView. The outer Item is NOT a FocusScope,
//      so calling `forceActiveFocus()` on it is a no-op for keys.
//   2. This panel's `focusInternal()` proxy — forwards to the
//      embedded tree so consumers don't reach inside.
//   3. Main.qml's Tab handler on `treeScope` — one key toggling the two
//      tabs, which routes focus through `_focusSidePanelTab()`. It is a
//      focus-chain `Keys.onPressed`, not an ApplicationShortcut, so Tab
//      keeps its own meaning in the terminal and in nvim. Ctrl+L from a
//      central pane re-enters on whichever tab is current.
//      (This was a Ctrl+J / Ctrl+K pair for directional nav between two
//      vertically-stacked sub-panes, removed with the stacking.)
//
// Auto-fallback when the changeset empties UNDER the user: Main.qml's
// `onHasChangesChanged` Connection on this item re-parks focus onto this
// FocusScope, because the inner tree hides for the empty state and an
// invisible item cannot hold activeFocus — focus has to move proactively
// or Qt drops it silently and the panel swallows every key, Tab included.

import QtQuick
import QtQuick.Layouts
import Symmetria.FileManager.UI as FmUi
import "design"

// FocusScope (not plain Item) — `activeFocus` propagates true when the
// embedded FileTreeView's inner ListView has the active focus, which lets
// Main.qml render this tab's focus bar via a plain binding
// (`gitStatusPanel.activeFocus ? Theme.color.accent.focus : ...`) for both
// keyboard arrivals AND mouse clicks (the inner ListView gains focus
// naturally on either path, and FocusScope.activeFocus bubbles up
// regardless).
//
// It is load-bearing a second way since the tabs landed: on a clean working
// tree the inner tree is hidden, so this scope is the only thing in the tab
// that CAN take focus, and Main.qml focuses it directly to keep the Tab key
// reachable. A plain Item here would drop focus outside the side panel.
FocusScope {
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

    // Whether the current changeset has anything in it. Drives the empty state
    // below, and the host reads it to decide whether focus can land on the
    // inner tree at all (an empty tree has no focusable row).
    //
    // THREE properties stood here until the side panel became tabbed:
    // `maxHeight` (a cap so a huge changeset could not push the file tree off
    // the bottom of a SHARED column), `collapsed` (a host-driven fold, so this
    // mini-tree got out of the way while the central git surface showed the
    // same changeset), and `reachable` (visible && !collapsed, which the focus
    // chords gated on). All three answered questions that only exist when two
    // trees share one column vertically. With one tab visible at a time, the
    // panel simply fills its tab, the host's tab state is the fold, and
    // "reachable" is just "this tab is current" — a question the HOST owns and
    // this panel cannot answer. Do not reintroduce them alongside the tabs.
    readonly property bool hasChanges: model !== null && model.count > 0

    // Emitted when the user clicks a file row. Carries the ABSOLUTE
    // filesystem path of the activated file. Main.qml connects this to
    // `controller.open_in_nvim(path)` and then re-focuses the editor.
    signal fileActivated(string absolutePath)

    // Public focus-routing proxy. Delegates to the embedded
    // `FmUi.FileTreeView`'s `focusInternal()` — the FM-side public
    // function that hands focus to the inner ListView (which is what
    // actually owns `Keys.onPressed`, so j/k/Ctrl+D/Ctrl+U/Enter only
    // fire when THAT item has activeFocus, not the FileTreeView's
    // outer Item). Symmetric with `fileTreeView.focusInternal()` in
    // Main.qml — a future chord can hand focus to either tree
    // identically. See the file header comment for the focus routing
    // rationale (FocusScope, ApplicationShortcut gating, auto-fallback).
    function focusInternal(): void {
        changesTree.focusInternal();
    }

    // The panel NO LONGER decides whether it is on screen — the host's tab
    // state does, and it fills whatever height the tab grants it.
    //
    // It self-hid on `model.count > 0` while it was stacked above the file
    // tree, where a clean working tree meant "give the space back". As a tab
    // that same rule would make the tab body vanish under a header that is
    // still there, so the clean case became an EMPTY STATE instead (see
    // `emptyState` below). The tab header stays put and the keybind keeps
    // meaning the same thing on a clean tree as on a dirty one.
    Layout.fillWidth: true
    // Content-fit implicit height is still published for any consumer that
    // wants to size to content; the tabbed host overrides it with
    // `Layout.fillHeight`. Includes the asymmetric top+bottom margins, or the
    // ColumnLayout inside (anchors.fill with margins) ends up `2*margin`
    // shorter than its content wants.
    implicitHeight: content.implicitHeight + Theme.spacing.sm * 2

    // `bg.bar`, matching the side-panel column this sits in — see the matte
    // on `treeScope` in Main.qml for why the whole column shares the bars'
    // rung. The two must move together; a rung between them splits the column
    // in half. The fill is therefore NOT what separates this panel from the
    // tree below (an older comment claimed it "drops slightly darker", which
    // stopped being true once both took the column's matte) — the hairline
    // border is, and it is the only thing that is.
    Rectangle {
        anchors.fill: parent
        color: Theme.color.bg.bar
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

        // Section header — three aggregate bucket rows (staged ●, unstaged ○,
        // untracked ✦) carrying +adds -dels (n). Bucket rows hide themselves
        // when their file count is 0, so a clean staging area collapses this
        // whole block to nothing.
        //
        // A `Changes · N` title sat above the buckets until the side panel
        // became tabbed. The tab header now draws that exact pair — the
        // source-control glyph and the file count as its badge — directly
        // above this block, so the title restated its own header one line
        // down. Do not add it back; put anything the tab cannot say (a
        // per-bucket total, a branch ref) in the buckets instead.
        ColumnLayout {
            Layout.fillWidth: true
            // ⚠ Explicit `false`, and it is load-bearing. A Layout nested
            // directly inside another Layout gets `Layout.fillHeight: true` by
            // DEFAULT (Qt Quick Layouts, unlike a plain Item, which defaults
            // false) — so without this line the header competes with the tab
            // body for the leftover column height. It was invisible while this
            // panel was sized to its own content (there was no leftover to
            // take); the moment the panel became a full-height tab it pushed
            // the empty state into the vertical middle of the column and stole
            // rows from the changes tree.
            Layout.fillHeight: false
            spacing: Theme.spacing.xxs

            Repeater {
                // Bucket rows summarise WORK-BY-SIDE (staged / unstaged /
                // untracked) — an index-state axis distinct from the per-file
                // badges' operation axis. Since the 2026-06-27 switch to
                // operation-based badge colour, the side glyphs are deliberately
                // NEUTRAL (text.normal): the ● / ○ / ✦ SHAPES carry the
                // staged/unstaged/untracked distinction, while COLOUR is reserved
                // for the operation grammar — green +adds, red −dels below — so
                // "green = additions, red = deletions" reads the same here as on
                // the badges. (These glyphs formerly borrowed
                // stagedGreen/unstagedRed/untrackedBlue, which made green mean
                // "staged" here but "added" on the badges — one colour, two
                // meanings. Don't reintroduce per-bucket fills.)
                model: [
                    {icon: "●",
                     add: (root.stats && root.stats.stagedAdd) || 0,
                     del: (root.stats && root.stats.stagedDel) || 0,
                     n:   (root.stats && root.stats.stagedFiles) || 0},
                    {icon: "○",
                     add: (root.stats && root.stats.unstagedAdd) || 0,
                     del: (root.stats && root.stats.unstagedDel) || 0,
                     n:   (root.stats && root.stats.unstagedFiles) || 0},
                    {icon: "✦",
                     add: (root.stats && root.stats.untrackedLines) || 0,
                     del: 0,
                     n:   (root.stats && root.stats.untrackedCount) || 0},
                ]
                delegate: RowLayout {
                    id: bucket
                    required property var modelData
                    visible: bucket.modelData.n > 0
                    spacing: Theme.spacing.xs

                    Text {
                        text: bucket.modelData.icon
                        color: Theme.color.text.normal
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.xs
                    }
                    Text {
                        visible: bucket.modelData.add > 0
                        text: "+" + bucket.modelData.add
                        color: Theme.color.diff.addedFg
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.xs
                    }
                    Text {
                        visible: bucket.modelData.del > 0
                        text: "-" + bucket.modelData.del
                        color: Theme.color.diff.removedFg
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.xs
                    }
                    Text {
                        text: "(" + bucket.modelData.n + ")"
                        color: Theme.color.text.dim
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.xs
                    }
                    Item { Layout.fillWidth: true }
                }
            }
        }

        // Clean-working-tree state. Replaces the panel's old self-hide: as a
        // tab body it must draw SOMETHING, or the tab header sits above a void
        // and the user cannot tell "no changes" from "this pane is broken".
        //
        // Deliberately one quiet line, `text.dim`, top-aligned rather than
        // centred in the tab: a centred empty state pulls the eye to the middle
        // of an otherwise empty column and makes nothing look like an event.
        // Clean is the normal state of a repo, so it should read as a footnote.
        Text {
            Layout.fillWidth: true
            Layout.topMargin: Theme.spacing.xs
            Layout.leftMargin: Theme.spacing.xs
            visible: !root.hasChanges
            text: "No changes"
            color: Theme.color.text.dim
            font.family: Theme.font.family
            font.pixelSize: Theme.font.size.xs
        }

        // Absorbs the leftover column height in the empty state, so the
        // "No changes" line stays pinned under the buckets instead of being
        // vertically centred by the ColumnLayout's own distribution.
        Item {
            Layout.fillWidth: true
            Layout.fillHeight: true
            visible: !root.hasChanges
        }

        // The tree-shaped list of changed files. Reuses the FM's FileTreeView
        // with `pathFilter` narrowing visible rows to the current changeset.
        //
        // `Layout.fillHeight` makes the tree claim whatever the tab granted the
        // panel; the FM ListView scrolls internally once the changeset exceeds
        // it (FM-side gate: `view.contentHeight > view.height + 0.5`). The
        // old dual-mode sizing note here described a `maxHeight` cap that the
        // tabbed layout removed — a tab is never in competition for height with
        // the file tree, so there is nothing left to clamp against.
        //
        // A Flickable wrapped this tree while the panel stacked several
        // per-repo sections; with one tree again the FM ListView does its own
        // scrolling and the wrapper only added a second scroll surface.
        //
        // `respectGitignore: false` — users genuinely want force-added
        // gitignored files (`git add -f`) visible; hiding them would lie about
        // the working tree. `showHidden: true` — a CHANGED dotfile is not noise
        // (the pathFilter already bounds rows to the actual changeset), the same
        // principle, and the main tree in Main.qml sets it too. `compactScale:
        // 0.75` packs rows tighter than that main tree below.
        FmUi.FileTreeView {
            id: changesTree
            Layout.fillWidth: true
            Layout.fillHeight: true
            // Hidden rather than empty on a clean tree: the FM tree with an
            // empty `pathFilter` still mounts its root row, so leaving it
            // visible would draw a lone project folder under "No changes" and
            // read as one changed directory.
            visible: root.hasChanges

            rootPath: root.repoRoot
            initialExpandDepth: -1
            respectGitignore: false
            showHidden: true
            compactScale: 0.75
            statusProvider: root.statusProvider
            pathFilter: root.pathFilter

            onFileActivated: (path) => root.fileActivated(path)
        }
    }
}
