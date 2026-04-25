# Memory index

Auto-memory for symmetria-ide. See `.claude/rules/memory_doctrine.md` for the layout rules and promotion lifecycle. Each bullet ≤150 chars, one per file.

## Feedback (user preferences, validated approaches)

- [Dev launches target workspace 6](feedback/dev_workspace.md) — open Symmetria IDE on Hyprland workspace 6 during iteration
- [New UI surfaces start as placeholders](feedback/ui_surface_discipline.md) — minimal delegate, Theme tokens, defer aesthetic decisions until real data exists

## Project — meta (identity, governance, key decisions)

- [Project governance layer](project/meta/project_governance.md) — read .claude/project-standards.md, CONTRIBUTING.md, and Theme.qml before planning non-trivial work

## Project — active (operational state being maintained)

- [Phase 2 current state (2026-04-25)](project/active/phase2_current_state.md) — Node SDK sidecar pivot landed, permission UI shipped; deferred: persistence, stop, turn grouping

## Project — shipped (past-tense systems; one consolidated bullet)

_(none yet — when a system ships and earns its way to a one-line pointer, file lives under `project/shipped/`.)_

## Reference — host (Hyprland, QuickShell, OS-level)

- [Notification system is Symmetria Shell, not swaync](reference/host/notification_system.md) — do not invoke swaync-client or makoctl

## Reference — nvim-rpc (NeoVim --embed, pynvim, msgpack-RPC)

- [Lazy-plugin keymap hijack pattern](reference/nvim-rpc/lazy_keymap_hijack_pattern.md) — User LazyLoad autocmd is the race-winning install point for `keys = {...}` lazy-loaded plugins

## Reference — qt-pyside (Qt 6, PySide6, QML, shiboken)

_(empty — gotchas live in CLAUDE.md until a session-relevant pitfall earns a dedicated file.)_

## Reference — agent-sdk (claude-agent-sdk + sidecar protocol)

_(empty — protocol contract lives in CLAUDE.md "The agent backend" section + `sidecar/src/protocol.ts`.)_
