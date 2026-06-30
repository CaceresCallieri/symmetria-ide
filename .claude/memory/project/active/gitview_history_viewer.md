---
name: gitview-history-viewer
description: "GitView — read-only git history viewer (\"git\" central surface); comprehension not mutation; v0 spine shipped 2026-06-15"
metadata: 
  node_type: memory
  type: project
  originSessionId: 3ef6783a-0332-48f6-ba9e-150a4a3e5ddc
---

The IDE's git frontend is a **comprehension-first** surface. Thesis: when agents
author the commits, the developer loses the understanding that comes free from
writing code — the **cognitive gap**. GitView exists to shrink it: browse/read/
navigate history. **Boundary refined 2026-06-29:** the line is no longer
"read-only, never mutate" but **no AUTHORING mutations** — stage/commit/rebase
(content creation) stay in nvim/shell; **TRANSPORT/sync ops are now in scope**.
First mutations shipped: `git pull` (`p`) / `git push` (`P`) from the history
list, via `src/symmetria_ide/git_ops_controller.py` (`GitOpsController` — its own
worker-thread lane, NOT the read-only log/status controllers) + a status toast
(qml/Toast.qml gained severity states: running/success/error, error persists).
Rationale the boundary holds: sync moves *existing* commits between local/remote
(navigation), it doesn't author content, so it doesn't undercut the thesis.

**v0 (shipped 2026-06-15) — the comprehension spine:**
- New `"git"` central surface (sibling of editor/terminal/agent); `Ctrl+Shift+G`
  toggles it; "history" chip in the AgentTopBar switcher.
- Backend `src/symmetria_ide/git_log_controller.py` — `GitLogController` +
  `GitLogListModel` + pure `parse_git_log`. Mirrors `git_controller.py`'s
  read-only worker/subprocess/emit discipline but request-driven (queue.Queue,
  no watcher/debounce); `git log -z` page load + `load_more` pagination +
  `git show` per-commit diff. Wired to the same anchored root via
  `_sync_git_repo_root`.
- UI `qml/githistory/{GitHistoryView,CommitListView,CommitDetailView}.qml` —
  master/detail; j/k walks the log and the diff streams live; diff colored via
  `Theme.color.diff.*`. Binds to INJECTED `gitLogController`/`gitLogModel`
  (not globals) so it lifts cleanly into a future standalone module.

**Decisions locked with the user:** native QML (not lazygit-in-a-pane), git CLI
backend, read-only. lazygit is reference-only (it shells to git, has no
embeddable library / headless query mode) — mine it for the hard algorithms.

**North stars (scaffolded, NOT built):** the `CommitRow.agent_id` field already
parses a `Symmetria-Agent-Id` commit trailer (agent-session correlation seam),
and a `seen` field exists (review-frontier seam) — both unpopulated in v0.
**Deferred phases:** native commit-graph ribbon (port lazygit's
`pkg/gui/presentation/graph`); standalone `symmetria-gitview` repo exposing
`Symmetria.Git.UI` + a C++ backend (FM pattern, for Shell/FM reuse).

Full plan: `/home/jc/.claude/plans/tidy-mapping-ullman.md`.

**Why:** the comprehension-not-mutation framing is the load-bearing product
decision and is NOT derivable from the code (the code is "a git viewer"; the
read-only-on-purpose rationale and the roadmap are not). Relates to
[[multi_instance_topology]] (many agent-driven repos open at once) and
[[ide_owns_keybind_layer]] (IDE owns the chord/surface layer).

**How to apply:** when extending GitView, prefer read/comprehension features
(blame, file history, catch-up range diff, agent grouping, review frontier). For
new ACTIONS, allow transport/sync (pull/push shipped; fetch/prune are natural
next) but still do NOT add AUTHORING mutations (stage/commit/rebase) — those stay
in nvim/shell. New mutating/network actions belong in `GitOpsController` (its own
worker lane + operationStarted/Finished → toast), NOT the read-only controllers;
new keys live in `CommitListView` as pure intents routed by `GitHistoryView`.
Keep the QML bound to injected providers for extraction-readiness.
