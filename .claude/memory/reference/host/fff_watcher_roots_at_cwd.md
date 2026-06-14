---
name: fff-watcher-roots-at-cwd
description: "Embedded nvim's cwd must never be $HOME — fff.nvim (user-global) recursively watches cwd → CPU/thermal runaway"
metadata: 
  node_type: memory
  type: reference
  originSessionId: b7bb8514-963e-460f-bdd8-c64b8a28c040
---

The embedded editor nvim loads the user's real `~/.config/nvim`, which includes
**fff.nvim** (`dmtrKovalenko/fff.nvim`, the "fff Rust engine"). fff's Rust
`notify`-crate backend recursively watches `base_path`, which defaults to
`vim.fn.getcwd()` (`conf.lua`) and has **no ignore-glob config** (only
`max_results`; it honors `.gitignore` only when rooted inside a repo).

The IDE feeds nvim its cwd through ONE chokepoint: `_apply_project_arg`
(`os.chdir`) → `os.getcwd()` → `AppController._cwd` → `displayedRoot` →
`initialWorkingDirectory` on the QMLTermSessions. So if the IDE is launched from
`$HOME` with no project arg, nvim's cwd is `$HOME`, and the first fff picker use
(`<leader>ff`) makes its Rust backend recursively index/watch the entire home
tree (`.cache`/`.steam`/browser profiles) → inotify event storm → ~6 pinned
cores, 88 °C (relay 20260614-04*). It is NOT a regression in `runtime/` (that
tree is byte-identical dev↔stable) and NOT the Python `WorktreeWatcher` (that is
git-gated and declines `$HOME` since it is not a repo).

**Why:** a future agent debugging IDE thermal/CPU will grep this repo for
"watcher" and find only `WorktreeWatcher` — the real culprit lives in the
**dotfiles** repo (`~/.dotfiles/.config/nvim/lua/jc/plugins/fff.lua`), not here.

**How to apply:** the fix is two-repo. IDE side: `_resolve_launch_dir`
(`app.py`) redirects a `$HOME`/`/` launch cwd to an inert
`$XDG_STATE_HOME/symmetria-ide/scratch`. Dotfiles side: `fff.lua` sets
`base_path` via an upward `.git` search with a non-`$HOME` scratch fallback
(also protects standalone nvim). Do NOT revert either to plain `getcwd()`. fff's
`DirChanged` autocmd re-roots to the RAW new cwd on `:cd`, so never `:cd` the
embedded nvim to `$HOME` either. See also
[notification system](./notification_system.md) for other host-config facts the
IDE inherits.
