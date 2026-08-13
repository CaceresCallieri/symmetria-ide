// Active Changes panel — path-filtered FileTreeView(s) of pending git
// changes.
//
// Sits ABOVE the main FileTreeView in the side panel column. Auto-hidden
// when the working tree is clean or we're not in a git repo — UNLESS the
// focused agent has changes in a foreign repo while the displayed repo is
// clean (see the `visible:` guard). The body is a Flickable holding a
// DEDICATED displayed-repo `FmUi.FileTreeView` (`changesTree`, always
// instantiated) plus, in "agent" scope, a Repeater of FOREIGN-repo sections —
// each an `FmUi.FileTreeView` whose `pathFilter` restricts visible rows to that
// repo's changed paths (plus ancestors up to its root). In "all" scope only the
// displayed tree shows (whole repo, no header); in "agent" scope the displayed
// tree is the focused agent's slice, headed when foreign sections sit beside it,
// followed by one section per FOREIGN repo the agent also changed. The displayed
// tree is deliberately NOT a Repeater delegate (delegate churn on every git
// scan — see the body comment). Each tree is always-expanded by default —
// `initialExpandDepth: -1` with the FM's caps (`maxExpandDepth: 8`,
// `_autoExpandModelCeiling: 100`, `_autoExpandFanoutCap: 200`) bounding it.
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
// (`Theme.color.bg.chrome`, hairline border) — the PANEL rung of the
// surface ladder, one step out from the content and one step in from the
// bars, so this panel and the file tree below it read as one column. (It
// used to say "continuous with the status bar"; the bars moved to
// `bg.bar` on 2026-08-13, so that reason is no longer the right one even
// though the token still is.) Per-row visuals come from the
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
//   2. This panel's `focusInternal()` proxy — forwards to the FIRST
//      section's tree (the displayed repo) so consumers don't reach
//      inside. Foreign sections below it are mouse-focusable (a click
//      focuses their own ListView; the FocusScope bubbles it); keyboard
//      descent INTO a foreign section is a v1 follow-up.
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
import QtQuick.Controls
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

    // --- Per-agent change scope -----------------------------------------
    // The FOCUSED agent's uncommitted changes. `agentPathFilter`/`agentCount`
    // are the DISPLAYED repo's slice (its write-tool + Bash provenance ∩ the
    // displayed repo's dirty set), from `AppController.focusedAgentChangesPathSet`
    // / `focusedAgentChangesCount`. `scope` toggles between the whole-repo
    // changeset ("all") and this per-agent view ("agent"); the "a" Shortcut
    // below flips it while the panel sub-pane holds focus.
    property var agentPathFilter: ({})
    property int agentCount: 0
    // Multi-root (v2): the same agent's changes in git repos OTHER than the
    // displayed one, as section descriptors `[{root, label, pathFilter, count}]`
    // (`AppController.focusedAgentForeignChanges`), the count of sections the
    // display cap hid (`focusedAgentForeignOverflow`), and the cross-repo TOTAL
    // file count (`focusedAgentChangesTotalCount`) that heads the panel and
    // decides the empty state. Foreign sections use `foreignStatusProvider`, a
    // ROOT-PARAMETERISED `statusForPath(root, abs)` provider (a per-section shim
    // closes over each section's root — see the Repeater below).
    property int agentTotalCount: 0
    property var foreignChanges: []
    property int foreignOverflow: 0
    property var foreignStatusProvider: null
    // The focused agent's 1-based slot (0 = none). Used only to word the
    // empty-state honestly — "no agent focused" vs "no changes from this agent"
    // — so `scope === "agent"` with an empty pool doesn't imply an agent exists.
    property int focusedAgentSlot: 0
    property string scope: "all"
    // True only in agent scope with nothing to show ANYWHERE (displayed + every
    // foreign repo) — drives the empty-state line. Keyed on the cross-repo TOTAL
    // so an agent working ENTIRELY in a foreign repo (displayed repo clean for
    // it) is NOT treated as empty. Kept distinct from the whole-repo clean case
    // (which hard-hides the panel via `visible:`) so the toggle stays reachable.
    readonly property bool agentScopeEmpty: scope === "agent" && agentTotalCount === 0

    // The panel body's per-repo SECTIONS. Unifies both scopes into one Repeater
    // (DRY — one FileTreeView delegate config, one sizing model):
    //   • "all" scope  → a single UNLABELLED section = the whole displayed repo
    //     (exactly the pre-multi-root body).
    //   • "agent" scope → the displayed-repo slice (if it has changes) plus one
    //     section per FOREIGN repo. Section headers appear only when there is
    //     more than one section (`labelled`), so a single-repo agent view still
    //     reads as a bare tree.
    // Multi-root body composition. The displayed-repo tree is a DEDICATED,
    // always-instantiated FileTreeView (see the body) so it never churns on a
    // git scan — only the FOREIGN sections live in a Repeater, whose model
    // (`foreignChanges`) changes on foreign edges, not on every keystroke.
    //
    // Does the displayed-repo tree have rows to show in the current scope?
    readonly property bool _displayedHasRows:
        scope === "agent" ? agentCount > 0 : (model ? model.count > 0 : false)
    // Foreign sections present (agent scope only). `foreignChanges` is a
    // QVariantList: use `.length`, NOT `Array.isArray` (fails on QVariantList in
    // Qt 6.11 — see the memory note).
    readonly property bool _hasForeign:
        scope === "agent" && foreignChanges && foreignChanges.length > 0
    // Label the displayed-repo tree with a section header ONLY when foreign
    // sections sit beside it — a lone displayed tree reads as a bare list, as
    // before multi-root.
    readonly property bool _showDisplayedHeader: _hasForeign && agentCount > 0
    // Collapse state for the displayed-repo section (foreign sections keep their
    // own per-delegate state). Starts expanded.
    property bool displayedSectionCollapsed: false

    // Last path segment, for section header labels. Trailing slashes trimmed so
    // "/a/b/" and "/a/b" both read "b".
    function _basename(path: string): string {
        if (!path)
            return "";
        var p = path.replace(/\/+$/, "");
        var i = p.lastIndexOf("/");
        return i >= 0 ? p.substring(i + 1) : p;
    }

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
    // Focus the displayed-repo tree when it has rows (the common case), else the
    // first FOREIGN section's tree (an agent working entirely in a foreign repo,
    // displayed slice empty). Foreign sections are otherwise mouse-focusable
    // (clicking focuses their own ListView; FocusScope bubbles it) — keyboard
    // descent INTO a foreign section is a v1 follow-up.
    function focusInternal(): void {
        if (changesTree.visible) {
            changesTree.focusInternal();
            return;
        }
        if (foreignRepeater.count > 0) {
            var first = foreignRepeater.itemAt(0);
            if (first && first.focusInternal)
                first.focusInternal();
        }
    }

    // Auto-hide when there are no changes. Hidden state collapses the
    // vertical real estate so the main file tree below claims it back.
    // The `|| agent-scope total` clause keeps the panel ALIVE when the focused
    // agent has changes only in a FOREIGN repo while the displayed repo is clean
    // (model.count == 0) — the multi-root motivating case; without it the panel
    // (and the foreign sections + the scope toggle) would hard-hide.
    visible: (model && model.count > 0)
             || (scope === "agent" && agentTotalCount > 0)
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
                    // "all", the focused agent's CROSS-REPO total (displayed +
                    // every foreign repo) in "agent" — each section carries its
                    // own per-repo count in its header below.
                    text: {
                        const n = root.scope === "agent"
                            ? root.agentTotalCount
                            : (root.model ? root.model.count : 0);
                        return "Changes · " + n;
                    }
                    color: Theme.color.text.dim
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.xs
                }

                // Scope switcher. Keyboard is primary — Ctrl+Shift+D (global)
                // or `a` while the panel is focused; the clicks are parity.
                //
                // Drawn ONLY when it can mean something (2026-08-13): with no
                // focused agent it offers to filter the repo down to nobody,
                // so it was a permanent two-segment control in the narrowest
                // column in the IDE that was inert most of the time. The
                // `scope === "agent"` clause is the way BACK — the same
                // defensive shape the location toggle uses at the top bar:
                // the pool can empty while the scope is still "agent" (a
                // closed agent, an emptied pool), and a control that vanishes
                // while its non-default state is active would strand the
                // panel showing an empty agent view with no visible way out.
                //
                // Both chords stay unconditional on purpose. Flipping to
                // agent scope with no agent is not an error state to be
                // blocked; it lands on the panel's own "No focused agent"
                // empty text, which ANSWERS the question the user just asked.
                //
                // No icons here, unlike the surface switcher: "all" and "this
                // agent" have no mark a reader would guess (see the note in
                // SegmentedControl's header).
                SegmentedControl {
                    Layout.alignment: Qt.AlignVCenter
                    Layout.rightMargin: Theme.spacing.xs
                    visible: root.focusedAgentSlot > 0 || root.scope === "agent"
                    // Tighter than the component's default on both axes: this
                    // sits in the narrow side panel, where the top bar's
                    // roomier rhythm pushes "This agent" against the edge.
                    horizontalPadding: Theme.spacing.sm
                    spacing: Theme.spacing.xxs

                    segments: [
                        { key: "all", label: "All" },
                        { key: "agent", label: "This agent" },
                    ]
                    current: root.scope
                    onActivated: key => root.scope = key
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

        // The per-repo change body. A DEDICATED displayed-repo tree (always
        // instantiated — whole repo in "all" scope, the agent's displayed-repo
        // slice in "agent" scope) plus, in agent scope, a Repeater of FOREIGN
        // sections, all stacked in one Flickable so the set scrolls together
        // when it exceeds the panel's maxHeight cap.
        //
        // Why the displayed tree is NOT a Repeater delegate: a Repeater with a
        // JS-array model recreates ALL delegates whenever the array identity
        // changes, and the "all"-scope filter changes every 200ms git scan — so
        // a unified Repeater tore down and rebuilt the tree (losing scroll +
        // focus, forcing an FM model rebuild) on every keystroke. Keeping it a
        // stable instance restores the pre-multi-root behaviour; only the
        // foreign sections churn, and their model changes only on foreign edges.
        //
        // Sizing: each FileTreeView is CONTENT-sized (`Layout.preferredHeight:
        // implicitHeight` = contentHeight + padding — NO per-tree fillHeight),
        // and the Flickable's own `implicitHeight` is bound to the section
        // column's, so the panel's content-fit implicitHeight binding upstream
        // still works. The Flickable takes `Layout.fillHeight`, so when the panel
        // clamps to maxHeight it shrinks below the column's height and scrolls
        // (contentHeight > height); under the cap it fits exactly and the
        // ScrollBar stays hidden.
        //
        // `respectGitignore: false` — users genuinely want force-added gitignored
        // files (`git add -f`) visible; hiding them would lie about the working
        // tree. `showHidden: true` — a CHANGED dotfile is not noise (the
        // pathFilter already bounds rows to the actual changeset).
        // `compactScale: 0.75` packs rows tighter than the main tree below.
        Flickable {
            id: bodyFlick
            Layout.fillWidth: true
            Layout.fillHeight: true
            // Bind implicitHeight to the section column so the panel's
            // content-fit implicitHeight (which sums `content`'s children)
            // accounts for the real content, not a Flickable's default 0.
            implicitHeight: bodyCol.implicitHeight
            contentWidth: width
            contentHeight: bodyCol.implicitHeight
            clip: true
            boundsBehavior: Flickable.StopAtBounds
            interactive: contentHeight > height + 0.5
            // Hidden (and excluded from the layout) when the agent scope has
            // nothing to show — the empty-state line below takes its place.
            visible: !root.agentScopeEmpty
            ScrollBar.vertical: ScrollBar {
                policy: bodyFlick.interactive ? ScrollBar.AsNeeded : ScrollBar.AlwaysOff
            }

            ColumnLayout {
                id: bodyCol
                width: bodyFlick.width
                spacing: Theme.spacing.xs

                // --- Displayed repo section --------------------------------
                // Header shown only when foreign sections sit beside it (a lone
                // displayed tree reads as a bare list, as before multi-root).
                GitChangeSectionHeader {
                    visible: root._showDisplayedHeader
                    collapsed: root.displayedSectionCollapsed
                    foreign: false
                    label: root._basename(root.repoRoot)
                    count: root.agentCount
                    onToggled: root.displayedSectionCollapsed =
                        !root.displayedSectionCollapsed
                }

                FmUi.FileTreeView {
                    id: changesTree
                    Layout.fillWidth: true
                    // Content-sized (see the Flickable sizing note).
                    Layout.preferredHeight: implicitHeight
                    // Hidden when the current scope leaves the displayed repo
                    // nothing to show (agent scope + no displayed changes) or the
                    // displayed section is collapsed.
                    visible: root._displayedHasRows && !root.displayedSectionCollapsed

                    rootPath: root.repoRoot
                    initialExpandDepth: -1
                    respectGitignore: false
                    showHidden: true
                    compactScale: 0.75
                    statusProvider: root.statusProvider
                    // Whole-repo changeset in "all"; the agent's displayed-repo
                    // slice in "agent".
                    pathFilter: root.scope === "agent"
                        ? root.agentPathFilter
                        : root.pathFilter

                    onFileActivated: (path) => root.fileActivated(path)
                }

                // --- Foreign repo sections (agent scope only) --------------
                // These DO live in a Repeater, but its model (`foreignChanges`)
                // changes only on foreign-list edges (a probe landing / a root
                // entering-leaving), NOT on every git scan — so delegate churn is
                // bounded to genuine changes. This is the inert git-status kind of
                // Repeater, NOT the "live terminal/webview pane" kind the
                // agent/browser surfaces forbid.
                Repeater {
                    id: foreignRepeater
                    model: root._hasForeign ? root.foreignChanges : []

                    delegate: ColumnLayout {
                        id: sectionItem
                        required property var modelData
                        Layout.fillWidth: true
                        spacing: Theme.spacing.xxs
                        // Sections start EXPANDED; clicking the header folds a
                        // repo away when a busy agent has touched many.
                        property bool sectionCollapsed: false

                        // Lets the panel's focusInternal() reach a foreign tree
                        // when the displayed slice is empty.
                        function focusInternal(): void {
                            sectionTree.focusInternal();
                        }

                        GitChangeSectionHeader {
                            foreign: true
                            collapsed: sectionItem.sectionCollapsed
                            label: sectionItem.modelData.label
                            count: sectionItem.modelData.count
                            onToggled: sectionItem.sectionCollapsed =
                                !sectionItem.sectionCollapsed
                        }

                        FmUi.FileTreeView {
                            id: sectionTree
                            Layout.fillWidth: true
                            Layout.preferredHeight: implicitHeight
                            visible: !sectionItem.sectionCollapsed

                            rootPath: sectionItem.modelData.root
                            initialExpandDepth: -1
                            respectGitignore: false
                            showHidden: true
                            compactScale: 0.75
                            // A per-section shim closes over this section's root
                            // (the FM seam is one-arg `statusForPath(abs)`, the
                            // foreign provider two-arg `statusForPath(root, abs)`).
                            statusProvider: foreignShim
                            pathFilter: sectionItem.modelData.pathFilter

                            onFileActivated: (path) => root.fileActivated(path)
                        }

                        QtObject {
                            id: foreignShim
                            signal statusChanged
                            function statusForPath(abs) {
                                return root.foreignStatusProvider
                                    ? root.foreignStatusProvider.statusForPath(
                                        sectionItem.modelData.root, abs)
                                    : null;
                            }
                        }
                        Connections {
                            target: root.foreignStatusProvider
                            function onStatusChanged(): void {
                                foreignShim.statusChanged();
                            }
                        }
                    }
                }

                // Foreign repos beyond the display cap. Their file counts still
                // land in the header total — only the sections are hidden (the
                // "no silent caps" rule).
                Text {
                    Layout.fillWidth: true
                    Layout.topMargin: Theme.spacing.xxs
                    visible: root.scope === "agent" && root.foreignOverflow > 0
                    text: "+" + root.foreignOverflow
                        + (root.foreignOverflow === 1 ? " more repo" : " more repos")
                    color: Theme.color.text.dim
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.xs
                }
            }
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
