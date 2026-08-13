// Git viewer — root of the "git" central surface.
//
// Three keyboard-first master/detail comprehension sub-views, cycled with Tab
// (and the clay tab header at the top-left):
//
//   - "changes"  — the UNCOMMITTED working tree. WorkingFileTreeView (left)
//                  drives WorkingFileDetailView (right): j/k walk the changed
//                  files — laid out as a FILE TREE so their place in the
//                  project structure is visible — and the file's working-tree
//                  diff (vs HEAD) streams in live. This is the DEFAULT view on
//                  entry — "what haven't I committed yet" is the most
//                  immediately actionable question.
//   - "history"  — the committed log. The master column stacks BranchListView
//                  (top band, ≤6 rows then scrolls; entered with `b`, filters
//                  the log by branch) above CommitListView; together they
//                  drive CommitDetailView (right): j/k walk commits, each
//                  commit's diff streams in. The long view of "how did we
//                  get here".
//   - "prs"      — GitHub pull requests (via the gh CLI). PrListView (left)
//                  lists open PRs (o toggles closed/merged, r refreshes —
//                  manual freshness only, never polled); Enter loads a PR's
//                  header + flattened conversation into PrDetailView (right).
//                  Enter-driven, NOT selection-driven: each detail load is
//                  two network gh calls, so j/k stays free. c checks out the
//                  PR's branch (confirm dialog at Main scope, like push).
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
    // Branches panel (history sub-view's leftmost column).
    property var branchController: null
    property var branchModel: null
    property var statusModel: null
    // Mutating/network git ops (pull/push) — the surface's first write actions.
    // Injected like the read controllers so the subtree stays extraction-ready.
    property var opsController: null
    // GitHub PR lane (gh CLI) — backs the "prs" sub-view.
    property var prController: null
    property var prModel: null
    property var prTimelineModel: null

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

    // Push needs a confirm dialog (a Main-level modal) and the branch/ahead
    // context (statusState/gitController globals Main owns), so push intent
    // bubbles to Main rather than being handled here. Pull has no such needs —
    // it's dispatched directly to opsController below. `dismissStatusRequested`
    // (Esc on the history list) bubbles to Main to hide the git-ops toast.
    signal pushRequested()
    signal dismissStatusRequested()
    // PR checkout needs the same Main-level confirm treatment as push (it
    // moves HEAD + the working tree), so the intent bubbles up with the
    // number + branch for the dialog message.
    signal prCheckoutRequested(int number, string branch)

    // Which sub-view is active. Initial value "changes" (working tree first)
    // per the surface's "what's uncommitted" framing; Tab toggles between the
    // two while the surface stays open. NOTE: "changes" is only the pre-first-
    // show value — `enterSurface()` re-picks the default on EVERY entry from the
    // live worktree state, so a sub-view the user toggled to in one visit is NOT
    // persisted across a leave/re-enter; the per-entry auto-pick wins instead.
    property string mode: "changes"

    // Is there uncommitted work to review? Single source of truth for "the tree
    // is dirty", bound by both `enterSurface()` (entry default) and the tab
    // header's file-count badge. statusModel is `property var … : null`, so the
    // null guard is load-bearing (it can be unset before the host injects it).
    readonly property bool hasPendingChanges: root.statusModel != null
                                              && root.statusModel.count > 0

    // PR-detail navigation state, per surface visit. `prDetailNumber` is the
    // PR the user opened with Enter (0 = none; drives PrDetailView's
    // requestedNumber + ready guard); `prDetailFocused` tracks which PR pane
    // owns the keys. Both reset in enterSurface() so re-entering the surface
    // always lands on the list.
    property int prDetailNumber: 0
    property bool prDetailFocused: false

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
        } else if (root.mode === "prs") {
            if (root.prDetailFocused)
                prDetail.focusList();
            else
                prList.focusList();
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
        // Freshness poke on entering the history side: the branches panel has
        // no watcher of its own, and a commit made to ANOTHER branch from a
        // different worktree escapes the `.git` watcher set (only the current
        // branch's ref is watched). Entering the panel is the natural "is
        // this fresh?" moment; reload() is coalesced, so this is cheap.
        if (next === "history" && root.branchController)
            root.branchController.reload();
        // Lazy first fetch on entering the PR tab: ensure_loaded() hits the
        // network once per (repo, filter) and no-ops after — manual `r` is
        // the only refetch (no polling, ever, by user decision).
        if (next === "prs" && root.prController)
            root.prController.ensure_loaded();
        focusContent();
    }

    // The cycle order for Tab. Adding a future sub-view (issues) = one entry
    // here + a tab-header entry + a visible-gated RowLayout below.
    readonly property var _modeCycle: ["changes", "history", "prs"]

    function toggleMode(): void {
        const i = root._modeCycle.indexOf(root.mode);
        setMode(root._modeCycle[(i + 1) % root._modeCycle.length]);
    }

    // Called by the host every time the surface becomes visible. Picks the most
    // useful default sub-view for THIS entry: "changes" when there's
    // uncommitted work to review, else "history". A clean working tree has
    // nothing to show in the changes view, so landing there would strand the
    // user on an empty pane — the committed log is the actionable thing to read
    // instead. Tab still toggles freely once inside. Re-evaluated on every entry
    // (not just first open) so the default tracks the live worktree state, and
    // routes through setMode so focus lands on the chosen list. Distinct from
    // the Ctrl+H focus-restore path, which calls focusContent() directly so it
    // re-homes focus WITHOUT re-picking the mode the user may have toggled to.
    function enterSurface(): void {
        // Re-entering always lands the PR tab on its list, not a stale
        // detail from the previous visit.
        root.prDetailNumber = 0;
        root.prDetailFocused = false;
        setMode(root.hasPendingChanges ? "changes" : "history");
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

        // --- Tab header ---------------------------------------------
        // Tab cycles the sub-views and is the primary path; the clicks are
        // parity. Each label carries a live count or ref so the scope of each
        // sub-view reads without entering it.
        //
        // The counts are composed into `label` HERE rather than inside the
        // control: a formatting function passed as a property would be a
        // function call in a binding, which QML does not re-evaluate (gotcha
        // #3), so the counts would freeze at their first value. Rebuilding
        // this array on a count change re-creates three Text delegates, which
        // costs nothing.
        SegmentedControl {
            id: tabHeader
            Layout.preferredHeight: Theme.size.modeBadgeHeight

            segments: [
                {
                    key: "changes",
                    label: "changes" + (root.hasPendingChanges
                                        ? " · " + root.statusModel.count : "")
                },
                {
                    key: "history",
                    label: "history" + (root.logController
                                        && root.logController.currentRef.length > 0
                                        ? " · " + root.logController.currentRef : "")
                },
                {
                    key: "prs",
                    label: "PRs" + (root.prModel && root.prModel.count > 0
                                    ? " · " + root.prModel.count : "")
                },
            ]
            current: root.mode
            onActivated: key => root.setMode(key)
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
                    // Esc here also dismisses a stuck git-ops error toast, so
                    // it's keyboard-dismissible from either sub-view.
                    onDismissStatusRequested: root.dismissStatusRequested()
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

            // Master column — branches panel stacked ABOVE the commit list
            // (the lazygit left-column rhythm), sharing the master width so
            // the detail pane keeps its room. The branches frame hugs its
            // content up to a cap of 6 rows, then scrolls internally — a
            // long branch list must not squeeze the log out of the column.
            ColumnLayout {
                // fillWidth must be EXPLICITLY false: a layout nested inside
                // another layout defaults Layout.fillWidth to TRUE (unlike a
                // plain Item/Rectangle), so without this the master column
                // grabs the row's spare width and crushes the detail pane to
                // zero. Regression observed live when the plain Rectangle
                // master became this ColumnLayout (2026-07-03).
                Layout.fillWidth: false
                Layout.preferredWidth: Math.round(root.width * 0.42)
                Layout.minimumWidth: 320
                Layout.fillHeight: true
                spacing: Theme.spacing.sm

                // Branches panel — entered with `b` from the commit list;
                // Enter on a row filters the log to that branch (Enter on
                // the checked-out branch clears back to HEAD).
                Rectangle {
                    Layout.fillWidth: true
                    // Content-hugging height: N rows (row pitch is the
                    // list's rowHeight) + the ListView's top/bottom content
                    // insets, capped at 6 rows before internal scrolling.
                    // Floor of 1 row keeps the "no branches" empty state a
                    // visible slim band rather than a collapsed sliver.
                    Layout.preferredHeight: Math.max(1, Math.min(
                            root.branchModel ? root.branchModel.count : 0, 6))
                        * branchList.rowHeight + Theme.spacing.xs * 2
                    color: Theme.color.bg.chrome
                    radius: Theme.radius.md
                    border.width: 1
                    border.color: Theme.color.border.hairline

                    BranchListView {
                        id: branchList
                        anchors.fill: parent
                        model: root.branchModel
                        activeFilter: root.logController ? root.logController.currentRef : ""

                        // Enter on the checked-out branch means "show me where I
                        // am" — identical to the unfiltered HEAD view, so it
                        // clears the filter instead of pinning a redundant one.
                        onBranchSelected: function (name, isHead) {
                            if (root.logController)
                                root.logController.set_ref(isHead ? "" : name);
                            commitList.focusList();
                        }
                        onClearFilterRequested: {
                            if (root.logController)
                                root.logController.set_ref("");
                        }
                        onFocusCommitListRequested: commitList.focusList()
                        onToggleRequested: root.toggleMode()
                    }
                }

                // Master: commit list — takes whatever height the branches
                // band leaves.
                Rectangle {
                    Layout.fillWidth: true
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
                        onFocusBranchesRequested: branchList.focusList()
                        // p → pull directly (no confirm); P → bubble push intent up
                        // to Main for the confirm dialog; Esc → bubble toast dismiss.
                        onPullRequested: if (root.opsController) root.opsController.pull()
                        onPushRequested: root.pushRequested()
                        onDismissStatusRequested: root.dismissStatusRequested()
                    }
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

        // --- "prs": GitHub pull requests ---------------------------------
        // NOT a side-by-side master/detail like the other two modes: the PR
        // list and the PR conversation are FULL-SURFACE alternates (a
        // drill-in, user decision 2026-07-12). Enter swaps the list for the
        // detail; h/Esc swaps back. Rationale: a half-width detail pane sat
        // mostly empty next to a short list, and the conversation deserves
        // the whole width (bodies render untruncated — see PrDetailView).
        Item {
            Layout.fillWidth: true
            Layout.fillHeight: true
            visible: root.mode === "prs"

            // The list, full width. Hidden (not unloaded) while the detail
            // is open so its selection survives the round-trip.
            Rectangle {
                anchors.fill: parent
                visible: !root.prDetailFocused
                color: Theme.color.bg.chrome
                radius: Theme.radius.md
                border.width: 1
                border.color: Theme.color.border.hairline

                PrListView {
                    id: prList
                    anchors.fill: parent
                    model: root.prModel
                    controller: root.prController
                    focus: root.mode === "prs" && !root.prDetailFocused

                    onToggleRequested: root.toggleMode()
                    // Enter → load the detail (network) and swap to it.
                    onOpenDetailRequested: function (number) {
                        if (root.prController)
                            root.prController.request_detail(number);
                        root.prDetailNumber = number;
                        root.prDetailFocused = true;
                        prDetail.focusList();
                    }
                    onCheckoutRequested: (number, branch) =>
                        root.prCheckoutRequested(number, branch)
                    onDismissStatusRequested: root.dismissStatusRequested()
                }
            }

            // The conversation, full width, shown in the list's place.
            PrDetailView {
                id: prDetail
                anchors.fill: parent
                visible: root.prDetailFocused
                radius: Theme.radius.md
                border.width: 1
                border.color: Theme.color.border.hairline
                controller: root.prController
                timelineModel: root.prTimelineModel
                requestedNumber: root.prDetailNumber

                onBackRequested: {
                    root.prDetailFocused = false;
                    prList.focusList();
                }
                onToggleRequested: root.toggleMode()
                onCheckoutRequested: (number, branch) =>
                    root.prCheckoutRequested(number, branch)
            }
        }
    }
}
