# Changelog

All notable changes to Symmetria IDE are documented here. Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning will follow [SemVer](https://semver.org/) once a release is cut.

## [Unreleased]

### Added
- Agent pane placeholder spike (Phase 2). `claude -p --output-format stream-json` spawns as a subprocess; `SessionHost` pumps JSONL events onto the GUI thread through a queued cross-thread connection; `SessionModel` renders one row per event with partial-text coalescing; `AgentPane.qml` displays the flat event list bound entirely against `Theme.color.agent.*` / `Theme.font.*` / `Theme.spacing.*`. Opt in via the `SYMMETRIA_IDE_AGENT_PROMPT` env var; editor-first workflows see no behaviour change when unset. 36 new tests.
- `Theme.color.agent.{user,assistant}` rung for the agent pane — `user` shares tone with `accent.primary`; `assistant` uses `wine_theme.term_cyan` (dim sibling of `mode.terminal`).
- Native which-key overlay (Lua emitter + QML panel) with trie built from `nvim_get_keymap` plus which-key.nvim's preset catalog.
- Native QML command-line overlay with independent completion pipeline (`getcompletion()`-driven, bypasses `nvim-cmp`/`wilder.nvim` popups).
- Smooth-scroll animation (critically-damped spring over 2× scrollback buffer, Neovide-parity).
- Cursor animation (remaining-delta spring with short-jump speedup) and wall-clock blink (gotchas #12, #13).
- Native QML status bar with well-known capsule protocol (`mode`, `file`, `branch`, `project`, `pos`) plus a generic extension slot.
- Headless smoke-test harness driven by `SYMMETRIA_IDE_SCREENSHOT` / `SYMMETRIA_IDE_TEST_KEYS` env vars.

### Fixed
- Render-thread SEGV under Python 3.14 caused by cyclic GC racing shiboken wrappers — mitigated by `gc.freeze()` + `gc.disable()` around `_dispatch_redraw` + `QColor` memoization (gotcha #10).
- Scroll geometry invariants (max_delta, scrollback multiplier, clip bounds, residual-gated trailing row) — gotcha #11.
- Which-key menu keymaps clobbering triggers and third-party plugin keymaps — self-healing reconciler + `maparg`/`mapset` save-restore (gotchas #17, #19).

### Infrastructure
- `.claude/project-standards.md` authoritative style ruleset.
- `selene.toml` + `neovim.yml` for Neovim-aware Lua linting.
- Tech-debt audit pass — 24 issues filed as GitHub issues with severity/module/effort/benefit labels.

## Phase 0 — Spine (complete)

Baseline PySide6 window embeds NeoVim via `--embed`. User's real nvim config loads by default. Capsule status bar, cmdline overlay, and which-key overlay land. See `docs/phases.md` for the full plan and `CLAUDE.md` for architectural context.

## Phase 1 — File Manager integration

Deferred.

## Phase 2 — Agent pane

**Placeholder spike landed.** Claude Code drives the pane via `claude -p --output-format stream-json` — a typed-event JSONL protocol instead of the originally planned `pty + pyte` bridge. Every Claude Code behaviour (hooks, skills, MCP, permissions, sessions) stays intact while the frontend consumes structured events directly. See `docs/phases.md` for the full pivot rationale and remaining deliverables (composer, permission UI, turn grouping, image + diagram rendering).

[Unreleased]: https://github.com/CaceresCallieri/symmetria-ide/compare/main...HEAD
