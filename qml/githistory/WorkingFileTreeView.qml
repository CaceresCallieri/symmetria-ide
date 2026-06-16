// Working-file TREE — the "master" pane of the uncommitted-files sub-view.
//
// Replaces the earlier flat WorkingFileListView. Instead of a flat list of
// changed paths, this renders the changeset as a FILE TREE so the user can
// see WHERE each change lives in the project's structure — the spatial
// context (which module, which directory) is itself a comprehension aid,
// shrinking the cognitive gap agent-authored changes open up.
//
// It is the SAME FM primitive the side panel's Active Changes panel
// (GitStatusPanel) already uses — an `FmUi.FileTreeView` whose `pathFilter`
// restricts visible rows to the current changeset (plus ancestors up to the
// repo root) — just composed for a different role: the central master pane,
// so it gets the FM's DEFAULT (roomier) row height rather than the side
// panel's compact 0.75 scale. That re-composition (not re-implementation) is
// the DRY win: the tree, the git-status badges, the always-expand caps, the
// keyboard nav all come from the shared module; we only supply the
// selection→diff seam and the Tab toggle.
//
// Selection drives the detail pane: as the highlighted row moves (j/k), the
// `currentFile` object recomputes and the host (GitHistoryView) re-requests
// that file's working-tree diff. `currentFile` is shaped to match what
// WorkingFileDetailView expects, so the detail pane is unchanged from the
// flat-list era.
//
// Bound to INJECTED inputs (repoRoot / statusProvider / pathFilter), never
// globals — the same discipline GitStatusPanel follows — so this subtree
// lifts cleanly into a future standalone Symmetria.Git.UI module. In
// particular, `currentFile` is derived ENTIRELY from the injected
// `statusProvider` (no `gitController` reach-through): the provider already
// resolves the badge colour and tooltip, and `untracked` falls out of the
// git porcelain char (`?`).

import QtQuick
import Symmetria.FileManager.UI as FmUi
import "../design"

FocusScope {
    id: root

    // Absolute path to the git repo root — drives the FM tree's rootPath and
    // the abs→repo-relative conversion the diff request needs.
    property string repoRoot: ""

    // FM duck-typed status seam — `statusForPath(absPath) -> {char, color,
    // tooltip, adds, dels}` or null. Same instance the side panel + main tree
    // use (`gitProviderAdapter` in Main.qml).
    property var statusProvider: null

    // Absolute-path membership map driving the FM's `pathFilter` (rootPath +
    // every changed leaf + every ancestor). Default `{}` keeps the embedded
    // tree empty-but-valid on first paint.
    property var pathFilter: ({})

    // The live GitStatusListModel — consumed ONLY for the clean-tree empty
    // state (`count === 0`) and as the recompute trigger for `currentFile`
    // (see `_statusRevision`). The tree itself renders from the FM's own
    // FileSystemModel + pathFilter gate, not from this model.
    property QtObject model: null

    // Emitted on Tab/Backtab — the host toggles between this view and history.
    signal toggleRequested()

    // Emitted when the user activates a row (Enter / double-click) on a FILE.
    // Carries the absolute path; the host routes it to open-in-nvim.
    signal fileActivated(string absolutePath)

    // Bumped on every worktree scan so `currentFile` recomputes even when the
    // SELECTION hasn't moved — otherwise the detail header's ±counts / tooltip
    // would go stale on an in-place edit of the already-selected file (the
    // flat list got this for free because its `currentFile` read role data off
    // `currentItem`, which re-binds on model reset; a tree row's status comes
    // from a `statusProvider` FUNCTION call, which has no such auto-dependency,
    // so we tap an explicit revision counter). Mirrors the host's
    // diff-re-request-on-reset wiring; together they keep the whole detail pane
    // live as the tree changes under the cursor.
    property int _statusRevision: 0
    Connections {
        target: root.model
        function onModelReset(): void { root._statusRevision++; }
    }

    // The highlighted file's data, surfaced for the detail pane. Null for a
    // DIRECTORY row (ancestors of changed files carry no diff) and for a clean
    // tree (no current row). Shape matches WorkingFileListView.currentFile so
    // WorkingFileDetailView needs no change.
    readonly property var currentFile: {
        // Dependency tap — read the revision so this binding re-evaluates on
        // each scan (see `_statusRevision`). The read itself is the point; the
        // value is unused.
        root._statusRevision; // eslint-disable-line no-unused-expressions

        var row = tree.currentRow;
        if (!row || row.isDir)
            return null;
        var st = root.statusProvider ? root.statusProvider.statusForPath(row.path) : null;
        if (!st)
            return null;
        return ({
            path: row.path,
            displayName: root._repoRelative(row.path),
            tooltip: st.tooltip,
            additions: st.adds || 0,
            deletions: st.dels || 0,
            // git porcelain marks untracked files `?` (git_controller.py).
            // The diff request uses this to pick `git diff HEAD` (tracked) vs
            // `--no-index /dev/null` (untracked).
            untracked: st.char === "?",
            // Already the resolved FmTheme git colour — the detail pane reuses
            // it directly rather than re-deriving a state→colour map.
            stateColor: st.color,
        });
    }

    // Hand keyboard focus to the FM tree's inner ListView (the item that owns
    // j/k/h/l/gg/G). The host calls this when the changes view becomes active.
    function focusList(): void {
        tree.focusInternal();
    }

    // Strip the repo root so the diff request gets the same repo-relative path
    // the flat GitStatusListModel exposed as `displayName` (the git_log
    // controller keys its published diff by this string).
    function _repoRelative(abs: string): string {
        if (root.repoRoot !== "" && abs.indexOf(root.repoRoot + "/") === 0)
            return abs.substring(root.repoRoot.length + 1);
        return abs;
    }

    // Tab / Backtab → ask the host to switch sub-views. The FM FileTreeView's
    // inner ListView handles j/k/h/l/etc. but NOT Tab, so an unhandled Tab
    // bubbles up the focus chain to THIS FocusScope's handler — where we
    // repurpose it and accept the event so Qt's focus traversal doesn't also
    // fire. (WorkingFileListView intercepted Tab inside its own ListView
    // handler; here the handler lives one level up because we don't own the FM
    // ListView's Keys block.)
    Keys.onPressed: function (event) {
        if (event.key === Qt.Key_Tab || event.key === Qt.Key_Backtab) {
            root.toggleRequested();
            event.accepted = true;
        }
    }

    FmUi.FileTreeView {
        id: tree
        anchors.fill: parent
        visible: root.model && root.model.count > 0

        rootPath: root.repoRoot
        // Always-expanded so the whole changeset is visible at a glance; the
        // FM's existing caps (maxExpandDepth 8, model/fanout ceilings) bound
        // the worst case.
        initialExpandDepth: -1
        // Show force-added gitignored files and changed dotfiles — hiding
        // either would lie about the working-tree state. The pathFilter
        // already bounds visible rows to the actual changeset, so neither flag
        // leaks unrelated files. (Same rationale as GitStatusPanel.)
        respectGitignore: false
        showHidden: true
        statusProvider: root.statusProvider
        pathFilter: root.pathFilter

        // Activation routes up to the host (→ open-in-nvim). Directory rows
        // toggle expand/collapse via the FM's own built-in handler, so this
        // only ever fires for files.
        onFileActivated: (path) => root.fileActivated(path)
    }

    // Empty state — the working tree is clean (nothing uncommitted). Calm, not
    // an error: this is the "all committed" resting state. Tab still switches
    // to history from here (the FocusScope keeps the key handler live).
    Text {
        anchors.centerIn: parent
        visible: !root.model || root.model.count === 0
        text: "working tree clean"
        color: Theme.color.text.dim
        font.family: Theme.font.family
        font.pixelSize: Theme.font.size.sm
        renderType: Text.NativeRendering
    }
}
