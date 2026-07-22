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
//   2. This panel's `focusInternal()` proxy — forwards to
//      `changesTree.focusInternal()` so consumers don't reach inside.
//   3. Main.qml's Ctrl+J / Ctrl+K ApplicationShortcuts — vim-style
//      directional sub-pane nav. Ctrl+K (up) lands here, Ctrl+J
//      (down) lands on the main tree. Both gated on
//      `treeScope.activeFocus` so the chords pass through to
//      nvim/terminal when the side panel isn't focused. Ctrl+L from
//      a central pane respects a sticky `activeTreeSubPane`
//      property — re-entering the side panel lands on whichever
//      sub-pane the user was last in, not always on the main tree.
//
// Auto-fallback when the panel hides (clean tree): Main.qml's
// `onVisibleChanged` Connection on this item resets
// `activeTreeSubPane` to 0 and re-routes focus to the main tree
// if needed — invisible items can't hold activeFocus, so we have to
// move focus proactively before Qt silently drops it.

import QtQuick
import QtQuick.Layouts
import Symmetria.FileManager.UI as FmUi
import "design"

// FocusScope (not plain Item) — `activeFocus` propagates true when the
// embedded FileTreeView's inner ListView has the active focus, which
// lets Main.qml render a per-sub-pane focus border via a plain binding
// (`gitStatusPanel.activeFocus ? Theme.color.accent.focus : ...`) AND
// drive the sticky `activeTreeSubPane` property via
// `onActiveFocusChanged` — works for both keyboard chords AND mouse
// clicks (the inner ListView gains focus naturally on either path,
// and FocusScope.activeFocus bubbles up regardless).
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

    // --- Per-agent change scope (v1) -------------------------------------
    // The FOCUSED agent's uncommitted changes as a `pathFilter`-shaped
    // membership map (its write-tool provenance ∩ the git dirty set) plus the
    // leaf count, supplied by `AppController.focusedAgentChangesPathSet` /
    // `focusedAgentChangesCount`. `scope` toggles between the whole-repo
    // changeset ("all") and just this map ("agent"); the "a" Shortcut below
    // flips it while the panel sub-pane holds focus.
    property var agentPathFilter: ({})
    property int agentCount: 0
    // The focused agent's 1-based slot (0 = none). Used only to word the
    // empty-state honestly — "no agent focused" vs "no changes from this agent"
    // — so `scope === "agent"` with an empty pool doesn't imply an agent exists.
    property int focusedAgentSlot: 0
    property string scope: "all"
    // True only in agent scope with nothing to show — drives the empty-state
    // line. Kept distinct from the whole-repo clean case (which hard-hides the
    // panel via `visible:`) so the toggle stays reachable when THIS agent owns
    // none of the repo's changes.
    readonly property bool agentScopeEmpty: scope === "agent" && agentCount === 0

    // Optional upper bound on the pane's height. `-1` (default) preserves
    // pure content-fit behaviour. Consumers in a tall column (e.g.
    // Main.qml's side panel) bind this to a fraction of the column
    // height so a pathologically large changeset can't push the
    // FileTreeView below off-screen — when implicitHeight exceeds
    // maxHeight, the panel clamps and the embedded FileTreeView's
    // own ListView scrollbar engages (the inner tree uses
    // `Layout.fillHeight: true` so its rendered height tracks whatever
    // the panel was actually granted, not its content-fit ideal).
    property real maxHeight: -1

    // When true, the panel folds away (height → 0, opacity → 0) even though
    // the working tree is dirty — the host sets this while the dedicated git
    // "changes" central surface is on screen, since that surface already shows
    // the full changes tree and this side mini-panel would just duplicate it.
    // The fold/unfold is animated (see the Layout.preferredHeight + opacity
    // Behaviors below) so the main file tree below glides up to claim the
    // space and back down on return — "the changes rise away, then settle
    // back" rather than a jarring pop.
    property bool collapsed: false

    // Whether the panel is actually reachable for keyboard focus — visible AND
    // not collapsed. The host's focus-routing chords (Ctrl+K, Ctrl+L re-entry)
    // gate on this instead of bare `visible`, because a collapsed panel is
    // still `visible: true` (it animates rather than hard-hiding) but can no
    // longer hold focus. The matching `reachableChanged` drives the host's
    // focus-release fallback.
    readonly property bool reachable: visible && !collapsed

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
    // Collapsed → 0 height so the ColumnLayout reclaims the space for the
    // main file tree below; the Behavior eases the fold/unfold. The clean-tree
    // case (visible:false) already collapses implicitHeight to 0, so this
    // multiplexes both reasons the panel can occupy no space.
    Layout.preferredHeight: collapsed ? 0 : implicitHeight
    Behavior on Layout.preferredHeight {
        NumberAnimation {
            duration: Theme.anim.duration
            easing.type: Easing.BezierSpline
            easing.bezierCurve: Theme.anim.standardCurve
        }
    }
    // `-1` is Qt's "no cap" sentinel for Layout.maximumHeight, matching
    // the property's own default. Binding rather than gating keeps the
    // expression reactive when maxHeight changes (e.g. window resize).
    Layout.maximumHeight: maxHeight > 0 ? maxHeight : -1
    Layout.fillWidth: true

    // Fade in concert with the height fold so the panel "dissolves" upward
    // rather than just shrinking. Same curve/duration as the height Behavior
    // so the two read as one motion.
    opacity: collapsed ? 0 : 1
    Behavior on opacity {
        NumberAnimation {
            duration: Theme.anim.duration
            easing.type: Easing.BezierSpline
            easing.bezierCurve: Theme.anim.standardCurve
        }
    }
    // Clip during the fold so the content (chrome + tree) is cropped to the
    // shrinking height instead of bleeding past it mid-animation.
    clip: true
    // Inert while collapsed — no stray clicks land on the invisible tree.
    enabled: !collapsed

    // Keyboard-first scope toggle: "a" flips all ⇄ this agent. Gated on
    // `activeFocus` (the FocusScope reports true whenever the embedded tree
    // owns focus — see the file header) so it fires ONLY while the changes
    // sub-pane is focused; elsewhere "a" types normally. Wins over the inner
    // ListView's key handling because a matching Shortcut is dispatched before
    // per-item key delivery. `root.scope` has THREE writers that all flip the
    // same property directly: this `a` key, the header segment clicks below, and
    // Main.qml's global `Ctrl+Shift+D` ApplicationShortcut.
    // SAFE against key-hijack: the embedded bare FmUi.FileTreeView hosts NO
    // focusable text input (verified — rename/create/fuzzy are separate popup
    // components the full FileManager wires, not this tree) and does not bind
    // `a`, so this can neither steal a keystroke from a text field nor shadow a
    // tree op. If a future FM change adds an inline text field here, tighten
    // this gate (e.g. require the ListView, not a TextInput, to hold focus).
    Shortcut {
        sequence: "a"
        enabled: root.activeFocus
        onActivated: root.scope = root.scope === "agent" ? "all" : "agent"
    }

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

            RowLayout {
                Layout.fillWidth: true
                spacing: Theme.spacing.sm

                Text {
                    Layout.fillWidth: true
                    // Count follows the active scope: whole-repo file count in
                    // "all", the focused agent's leaf count in "agent".
                    text: {
                        const n = root.scope === "agent"
                            ? root.agentCount
                            : (root.model ? root.model.count : 0);
                        return "Changes · " + n;
                    }
                    color: Theme.color.text.dim
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.xs
                }

                // Scope switcher — clay two-segment control (same idiom as
                // GitHistoryView's tab header / AgentTopBar's surface switcher):
                // the ACTIVE scope raises into a clay capsule, the inactive
                // stays flat. Reads as a proper toggle at a glance. Keyboard:
                // Ctrl+Shift+D (global) or `a` in-panel; the clicks are parity.
                Row {
                    Layout.alignment: Qt.AlignVCenter
                    // Right margin gives the rightmost segment's clay shadow room
                    // to render inside the panel's `clip: true` root — PillSurface
                    // shadows paint OUTSIDE the pill, so a flush edge would slice
                    // the active "this agent" segment's convex shadow (see the
                    // PillSurface header gotcha).
                    Layout.rightMargin: Theme.spacing.xs
                    spacing: Theme.spacing.xxs

                    Repeater {
                        model: [
                            {scope: "all", label: "all"},
                            {scope: "agent", label: "this agent"},
                        ]
                        delegate: Item {
                            id: seg
                            required property var modelData
                            readonly property bool isCurrent: root.scope === seg.modelData.scope

                            height: Theme.size.modeBadgeHeight
                            implicitWidth: segLabel.implicitWidth + Theme.spacing.sm * 2

                            PillSurface {
                                anchors.fill: parent
                                radius: height / 2
                                elevated: seg.isCurrent
                                color: seg.isCurrent ? Theme.color.bg.selected : "transparent"
                                borderColor: seg.isCurrent
                                    ? Theme.color.border.hairline
                                    : "transparent"
                            }

                            Text {
                                id: segLabel
                                anchors.centerIn: parent
                                text: seg.modelData.label
                                color: seg.isCurrent
                                    ? Theme.color.text.strong
                                    : Theme.color.text.dim
                                font.family: Theme.font.family
                                font.pixelSize: Theme.font.size.xs
                                font.weight: seg.isCurrent
                                    ? Theme.font.weight.bold
                                    : Theme.font.weight.medium
                                font.letterSpacing: 0.6
                                renderType: Text.NativeRendering
                            }

                            MouseArea {
                                anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor
                                onClicked: root.scope = seg.modelData.scope
                            }
                        }
                    }
                }
            }

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
                    // Repo-wide staged/unstaged/untracked aggregates — they
                    // don't apply per-agent, so hide them in "this agent" scope
                    // (the header count already conveys the agent's file count).
                    // Otherwise "this agent · 0" would sit above misleading
                    // repo-wide +N buckets (the live-demo inconsistency).
                    visible: bucket.modelData.n > 0 && root.scope === "all"
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

        // The tree-shaped list of changed files. Reuses the FM's
        // FileTreeView with `pathFilter` narrowing visible rows to the
        // current changeset.
        //
        // Sizing is dual-mode. The inner FileTreeView sets
        // `Layout.fillHeight: true`, so it claims whatever vertical
        // space the panel was granted. Two cases:
        //   1. Panel under maxHeight (or maxHeight unset). The root
        //      Item's `implicitHeight` is the sum of header impl +
        //      tree impl (= contentHeight + padding), so the
        //      `Layout.preferredHeight: implicitHeight` upstream
        //      hands the panel exactly enough room for all rows.
        //      fillHeight gives the tree the remaining granted height,
        //      which in this case equals its contentHeight, and the
        //      ScrollBar stays hidden (FM-side gate:
        //      `view.contentHeight > view.height + 0.5`).
        //   2. Panel clamped by maxHeight. Tree gets less than its
        //      contentHeight, the FM ListView starts scrolling, and
        //      the ScrollBar appears. Users mouse-wheel through the
        //      changeset within the cap; when the cap is engaged,
        //      the main FileTreeView below is guaranteed at least
        //      `column.height - maxHeight` of vertical space.
        //
        // `respectGitignore: false` is deliberate — users genuinely want
        // to see force-added gitignored files (e.g. a build artifact
        // added with `git add -f`); hiding them would lie about the
        // working-tree state.
        //
        // `showHidden: true` is the same principle applied to dotfiles
        // and dotdirs (`.claude/`, `.config/`, `.github/`, `.vscode/`).
        // The FM's default hides them as filesystem noise; a CHANGED
        // dotfile is by definition not noise, so suppressing it would lie
        // about the working-tree state the same way hiding gitignored adds
        // would. The pathFilter already bounds visible rows to the actual
        // changeset, so flipping this on does not expose unrelated
        // dotfiles — only the changed paths themselves and their
        // ancestor directories up to the repo root. NOTE: the main
        // general-purpose tree in Main.qml now ALSO sets `showHidden: true`
        // (an IDE project root is dotfile-dense and users expect those
        // visible) — do NOT reintroduce the old assumption that hiding
        // dotfiles is "right for the general-purpose tree."
        //
        // `compactScale: 0.75` makes rows tighter than the main tree
        // below — the changes pane benefits from packing more rows
        // into the vertical space it claims.
        FmUi.FileTreeView {
            id: changesTree
            Layout.fillWidth: true
            Layout.fillHeight: true
            // Hidden (and excluded from the layout) when the agent scope has
            // nothing to show — the empty-state line below takes its place.
            visible: !root.agentScopeEmpty

            rootPath: root.repoRoot
            initialExpandDepth: -1
            respectGitignore: false
            showHidden: true
            compactScale: 0.75
            statusProvider: root.statusProvider
            // Whole-repo changeset in "all"; the focused agent's touched ∩
            // dirty map in "agente". rootPath stays the displayed repo either
            // way, so the agent map only renders paths under it (foreign-repo
            // edits are a v1 no-show, by design).
            pathFilter: root.scope === "agent" ? root.agentPathFilter : root.pathFilter

            onFileActivated: (path) => root.fileActivated(path)
        }

        // Empty-state for the "este agente" scope. The panel itself stays
        // visible (there ARE repo changes, else `visible:` would hard-hide
        // it) so the toggle is reachable — this agent just owns none of them.
        Text {
            Layout.fillWidth: true
            Layout.topMargin: Theme.spacing.xs
            visible: root.agentScopeEmpty
            // Word it honestly: no agent at all vs a focused agent with nothing
            // uncommitted. `scope === "agent"` can outlive the pool emptying.
            text: root.focusedAgentSlot === 0
                ? "No focused agent"
                : "No changes from this agent"
            wrapMode: Text.WordWrap
            color: Theme.color.text.dim
            font.family: Theme.font.family
            font.pixelSize: Theme.font.size.xs
        }
    }
}
