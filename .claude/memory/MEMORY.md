# Memory index

Auto-memory for symmetria-ide. See `.claude/rules/memory_doctrine.md` for the layout rules and promotion lifecycle. Each memory file gets exactly one bullet entry here (≤150 chars).

## Feedback — `feedback/` (user preferences, validated approaches)

- [Dev launches target workspace 6](feedback/dev_workspace.md) — open Symmetria IDE on Hyprland workspace 6 during iteration
- [New UI surfaces start as placeholders](feedback/ui_surface_discipline.md) — minimal delegate, Theme tokens, defer aesthetic decisions until real data exists

## Project — meta — `project/meta/` (identity, governance, key decisions)

- [Project governance layer](project/meta/project_governance.md) — read .claude/project-standards.md, CONTRIBUTING.md, and Theme.qml before planning non-trivial work
- [Agent dashboard dual-feed commitment](project/meta/agent_dashboard_commitment.md) — IDE keeps hooks AND adds SDK feed; phased execution plan in docs/orchestrator-replacement-prd.md
- [IDE owns the keybind layer](project/meta/ide_owns_keybind_layer.md) — Symmetria IDE is canonical UX surface; nvim/terminal are bare engines; IDE chords always win

## Project — active — `project/active/` (operational state being maintained)

- [Phase 2 current state (2026-04-25)](project/active/phase2_current_state.md) — Node SDK sidecar pivot landed, permission UI shipped; deferred: persistence, stop, turn grouping
- [Post-TIOCSCTTY fzf lag — RESOLVED (2026-05-19)](project/active/post_tiocsctty_lag_investigation.md) — pyte DSR/DA1 reply via `_AnswerbackHistoryScreen`; fzf Ctrl+E now ~100ms

## Project — shipped — `project/shipped/` (past-tense systems; one consolidated bullet)

- [Phase 2.5 — terminal pane + project anchor](project/shipped/phase25_current_state.md) — anchor (D1), PTY terminal (D2), OSC 7 cwd sync (D3) — all shipped 2026-05-18

## Reference — host — `reference/host/` (Hyprland, QuickShell, OS-level)

- [Notification system is Symmetria Shell, not swaync](reference/host/notification_system.md) — do not invoke swaync-client or makoctl

## Reference — nvim-rpc — `reference/nvim-rpc/` (NeoVim --embed, pynvim, msgpack-RPC)

- [Lazy-plugin keymap hijack pattern](reference/nvim-rpc/lazy_keymap_hijack_pattern.md) — User LazyLoad autocmd is the race-winning install point for `keys = {...}` lazy-loaded plugins

## Reference — qt-pyside — `reference/qt-pyside/` (Qt 6, PySide6, QML, shiboken)

_(empty — gotchas live in CLAUDE.md until a session-relevant pitfall earns a dedicated file.)_

## Reference — agent-sdk — `reference/agent-sdk/` (claude-agent-sdk + sidecar protocol)

_(empty — protocol contract lives in CLAUDE.md "The agent backend" section + `sidecar/src/protocol.ts`.)_
