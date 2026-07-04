# Memory index

Auto-memory for symmetria-ide. See `.claude/rules/memory_doctrine.md` for the layout rules and promotion lifecycle. Each memory file gets exactly one bullet entry here (≤150 chars).

## Feedback — `feedback/` (user preferences, validated approaches)

- [Dev launches target workspace 6](feedback/dev_workspace.md) — open Symmetria IDE on Hyprland workspace 6 during iteration
- [New UI surfaces start as placeholders](feedback/ui_surface_discipline.md) — minimal delegate, Theme tokens, defer aesthetic decisions until real data exists
- [Popups use the FM scale-pop entrance](feedback/popup_animation.md) — bind scale/opacity off visible + Theme.anim tokens; never hand-roll durations
- [Clay chrome surfaces](feedback/clay_chrome_surfaces.md) — new chrome uses PillSurface/PillCard + Theme.depth, gate depth via `elevated`; no flat Rectangles
- [MCP enablement is per-project](feedback/mcp_enablement_per_project.md) — costly agent MCP gates per PROJECT (default off, IDE-owned), never per agent
- [Prefer peer over shell coordination](feedback/prefer_peer_over_shell_coordination.md) — cross-IDE state via shared file/socket, NOT the shell bridge; survives shell swap/outage

## Project — meta — `project/meta/` (identity, governance, key decisions)

- [Project governance layer](project/meta/project_governance.md) — read .claude/project-standards.md, CONTRIBUTING.md, and Theme.qml before planning non-trivial work
- [Agent dashboard dual-feed commitment](project/meta/agent_dashboard_commitment.md) — IDE keeps hooks AND adds SDK feed; phased execution plan in docs/orchestrator-replacement-prd.md
- [IDE owns the keybind layer](project/meta/ide_owns_keybind_layer.md) — Symmetria IDE is canonical UX surface; nvim/terminal are bare engines; IDE chords always win
- [Multi-instance topology](project/meta/multi_instance_topology.md) — many concurrent IDE instances (one/project across Hyprland workspaces) is deliberate; never consolidate to single-process

## Project — active — `project/active/` (operational state being maintained)

- [Framework pivot REVERSED (2026-06-07)](project/active/framework_pivot.md) — Tauri pivot abandoned; staying native QML to reuse FM's Symmetria.FileManager.UI module. Plan QML, not web.
- [Phase 2 SDK pane (parked+env-gated 2026-06-10)](project/active/phase2_current_state.md) — terminal-agent runtime superseded it; mounts only via SYMMETRIA_IDE_SDK_PANE=1
- [GitView history viewer](project/active/gitview_history_viewer.md) — comprehension-first "git" surface; pull/push (transport) now in scope via GitOpsController, no authoring mutations
- [Markdown preview in IDE (idea)](project/active/markdown_preview_in_ide.md) — future: themed HTML .md preview in the embedded browser, key-toggled
- [Startup optimization outcomes](project/active/startup_optimization_followups.md) — gc.collect drop SHIPPED (51bf26c, ~20ms); WebEngine import deferral spiked+works but HELD (Qt-deprecated late init)
- [Agent ownership inversion (P1-4 SHIPPED; P5 IDE-decoupled)](project/active/agent_ownership_inversion.md) — claude agents IDE-owned; STT now pure-direct shell→IDE (bridge STT/inject removed); orchestrator.nvim KEPT (IDE just decoupled). Live dictation verify owed. See docs/agent-ownership-inversion.md

## Project — shipped — `project/shipped/` (past-tense systems; one consolidated bullet)

- [Phase 2.5 — terminal pane + project anchor](project/shipped/phase25_current_state.md) — anchor (D1), PTY terminal (D2), OSC 7 cwd sync (D3) — all shipped 2026-05-18
- [GitController cold-start recovery](project/shipped/gitcontroller_cold_start_recovery.md) — dir opened pre-`git init` froze status "clean"; sentinel watch + backed-off re-resolve, no nvim

## Reference — host — `reference/host/` (Hyprland, QuickShell, OS-level)

- [Notification system is Symmetria Shell, not swaync](reference/host/notification_system.md) — do not invoke swaync-client or makoctl
- [fff.nvim watches nvim's cwd](reference/host/fff_watcher_roots_at_cwd.md) — never root embedded nvim at $HOME; recursive Rust watch → thermal runaway. Fix spans IDE + dotfiles
- [STT chip stuck = stale hub broadcast](reference/host/stt_chip_hub_broadcast.md) — IDE reads hub stt field, shell reads local AgentService; fix is hub self-heal in agent-bridge.py

## Reference — nvim-rpc — `reference/nvim-rpc/` (NeoVim `--listen` socket, pynvim RPC-only, msgpack)

- [Lazy-plugin keymap hijack pattern](reference/nvim-rpc/lazy_keymap_hijack_pattern.md) — User LazyLoad autocmd is the race-winning install point for `keys = {...}` lazy-loaded plugins

## Reference — qt-pyside — `reference/qt-pyside/` (Qt 6, PySide6, QML, shiboken)

- [QML overlay focus discipline](reference/qt-pyside/qml_overlay_focus.md) — wrapper Item's `Component.onCompleted: forceActiveFocus()` steals focus from inner self-focusing widgets
- [RowLayout center-drift](reference/qt-pyside/rowlayout_center_drift.md) — hidden fillWidth+bare AlignVCenter→center drift; fix: AlignLeft+spacer
- [QML strict-property on QObject](reference/qt-pyside/qml_strict_property_qobject.md) — C++ QObject dynamic-prop assign silently no-ops; use JS map
- [QML typed param + default value](reference/qt-pyside/qml_typed_param_no_default.md) — Qt 6.11 rejects `x: T = default`; cascades to "type unavailable" at engine load
- [QML Array.isArray rejects QVariantList](reference/qt-pyside/qml_qvariantlist_array_check.md) — PySide6 list props fail `Array.isArray()` in Qt 6.11; use `x != null && x.length > 0`
- [grabWindow GIL deadlock](reference/qt-pyside/grabwindow_gil_deadlock.md) — sync grab + Python QQuickPaintedItem + threaded loop = ABBA hang; force basic loop
- [Fork changes need makepkg](reference/qt-pyside/fork_changes_need_makepkg.md) — launchers load the pacman qmltermwidget pkg; commit + makepkg -sif after fork edits
- [ApplicationShortcut masks terminal keys](reference/qt-pyside/applicationshortcut_masks_terminal_keys.md) — chrome Shortcut eats keys before QMLTermWidget; suspect it when a key fails in-IDE but works in Ghostty
- [QtWebEngine CDP drives chrome-devtools-mcp](reference/qt-pyside/qtwebengine_cdp_devtools_mcp.md) — QTWEBENGINE_REMOTE_DEBUGGING + --browserUrl: all 29 tools work; Puppeteer attaches, Playwright fails #36961
- [Startup performance](reference/qt-pyside/startup_perf.md) — SYMMETRIA_IDE_TRACE waterfall + interleaved A/B; fixed browser-MCP GUI-thread import (~1s) + eager WebEngine (~430ms)
- [processEvents() shared-app SEGV](reference/qt-pyside/processevents_shared_app_segv.md) — never pump the session app in tests; runs prior tests' deleteLater → gotcha #10 crash; hand-deliver queued slots

## Reference — agent-sdk — `reference/agent-sdk/` (claude-agent-sdk + sidecar protocol)

- [No hook on Esc-interrupt](reference/agent-sdk/no_hook_on_esc_interrupt.md) — Claude fires NO hook on cancel (2.1.170); sparkle sticks; EscapeWatcher fallback
- [opencode browser MCP wiring](reference/agent-sdk/opencode_remote_mcp_sse_only.md) — SSE-only transport; inject via OPENCODE_CONFIG_CONTENT (merges); headers+attribution work (spiked)
- [Daemon freezes agent env](reference/agent-sdk/daemon_freezes_agent_env.md) — CC 2.1.x spare-pool freezes agent env id across projects; attribute by session_id+cwd

_(sidecar protocol contract lives in CLAUDE.md "The agent backend" section + `sidecar/src/protocol.ts`.)_
