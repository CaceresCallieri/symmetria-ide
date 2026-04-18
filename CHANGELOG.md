# Changelog

All notable changes to Symmetria IDE are documented here. Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning will follow [SemVer](https://semver.org/) once a release is cut.

## [Unreleased]

### Added
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

Starting next. Claude Code (or OpenCode/custom harness) as a sibling pane via pty/pyte bridge. Reference patterns: `nvim_backend.py` worker-thread shape, Warp's block model.

[Unreleased]: https://github.com/CaceresCallieri/symmetria-ide/compare/main...HEAD
