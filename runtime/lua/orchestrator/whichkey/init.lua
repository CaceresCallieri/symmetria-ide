-- Native which-key overlay for Symmetria IDE — Lua emitter.
--
-- Fires `vim.rpcnotify(0, "whichkey", payload)` notifications that the
-- Python backend (nvim_backend.py `_h_whichkey`) routes into
-- `WhichKeyModel` + `WhichKeyState` (app.py), rendered by
-- `qml/WhichKeyOverlay.qml`. Zero dependency on which-key.nvim's
-- rendering code — we own the data source (nvim_get_keymap) and the
-- UI.
--
-- C2 (this commit) replaces the C1 mock with real tree-driven
-- emissions. The trie is rebuilt on BufEnter / LspAttach / LspDetach
-- so buffer-local maps are always in sync. A single `<leader>`
-- emission still fires on VimEnter for visual verification; C3
-- replaces that demo firing with proper trigger keymaps + a state
-- machine that drives emissions from actual key presses.
--
-- ### Kill switch
--
-- Gated by `vim.g.symmetria_whichkey_native = 1`. Clear the flag to
-- disable all emissions and let stock which-key.nvim run as normal.

local Tree = require("orchestrator.whichkey.tree")
local Icons = require("orchestrator.whichkey.icons")
-- Preload state + triggers + presets eagerly at init time. If we
-- waited until the keymap callback fired, `require(...)` could fail:
-- user config (lazy.nvim in particular) can mutate `rtp` AFTER
-- init.lua runs but BEFORE the trigger fires, so Lua's nvim-rtp
-- searcher may no longer find our runtime/lua/ path. Loading here
-- while our rtp entry is still fresh caches these modules in
-- `package.loaded`, so the later require() hits the cache regardless
-- of what happened to `rtp` in between.
local State = require("orchestrator.whichkey.state")
local Triggers = require("orchestrator.whichkey.triggers")
local Presets = require("orchestrator.whichkey.presets")

local M = {}

-- Cached trie, rebuilt on autocmd. Guarded against concurrent reads
-- only by Lua's single-threaded execution within nvim.
---@type table?
local current_tree = nil

-- Build fresh trie for a given mode (always "n" in v1).
---@param mode string
local function rebuild(mode)
  current_tree = Tree.rebuild(mode or "n")
end

---@return table root
function M.tree()
  if not current_tree then
    rebuild("n")
  end
  return current_tree
end

-- Emit a fully-formed "show" payload, given a prefix node. Helper
-- for the demo and (in C3) the state machine.
---@param prefix table  trie node to expand as the menu
local function emit_show_for_node(prefix)
  local items = Tree.items_for(prefix, Icons.for_node)
  -- `can_go_back` is true only when there's a *real* ancestor to pop
  -- to. Root's direct children (e.g. <leader> itself) have root as
  -- their parent, but root has no desc / no render, so <BS> at that
  -- depth should close rather than navigate. Root is identified by
  -- `parent == nil` on the trie, so a grandparent check works.
  local can_go_back = prefix.parent ~= nil and prefix.parent.parent ~= nil
  pcall(vim.rpcnotify, 0, "whichkey", {
    op = "show",
    mode = "n",
    trail = prefix.keys or "",
    can_go_back = can_go_back,
    items = items,
  })
end

-- Public: emit menu for the given keystroke-path. `path_keys` is the
-- already-normalized lhs (e.g. `" "` for `<leader>`, `" b"` for the
-- buffer submenu). No-op if the path has no children.
---@param path_keys string
function M.show(path_keys)
  local root = M.tree()
  local node = Tree.find(root, Tree.split_keys(path_keys or ""))
  if not node or not Tree.is_group(node) then
    return
  end
  emit_show_for_node(node)
end

function M.emit_hide()
  pcall(vim.rpcnotify, 0, "whichkey", { op = "hide" })
end

-- If which-key.nvim is installed alongside our native overlay, neuter
-- its trigger installation so the two don't duel for the same prefix
-- keymaps. We let which-key remain loadable (user config may still
-- `require` it for unrelated reasons) but clear its `triggers` config
-- so its state machine never attempts to bind `<leader>` etc.
--
-- Also warn loudly if the user has registered specs via
-- `require('which-key').add(...)`. Our overlay sources all its data
-- from `nvim_get_keymap()`, so any which-key-only metadata (icons,
-- group names that aren't attached to real keymaps) would be silently
-- ignored. The user's current config (13 lines, no `add()` calls)
-- doesn't hit this, but future config drift might.
local function neutralize_whichkey_nvim()
  local ok, wk = pcall(require, "which-key")
  if not ok then
    return
  end

  -- Disable which-key's own trigger installation. `triggers = {}` is
  -- a sentinel v3 understands as "install zero triggers regardless of
  -- preset". Merged non-destructively with whatever the user passed
  -- to its earlier setup() call.
  pcall(wk.setup, { triggers = {} })

  -- Best-effort tear down of any triggers already installed. In v3
  -- this module exposes `_triggers` + `del` but not a bulk uninstall;
  -- iterating is safe because `del` no-ops on already-removed keys.
  local ok_tr, wk_triggers = pcall(require, "which-key.triggers")
  if ok_tr and type(wk_triggers) == "table" and type(wk_triggers._triggers) == "table" then
    for _, trigger in pairs(wk_triggers._triggers) do
      pcall(wk_triggers.del, trigger)
    end
  end

  -- Warn the user if they've queued which-key specs. Those would have
  -- carried icons / group overrides that we don't ingest. Not a hard
  -- failure — the overlay still works from `nvim_get_keymap()` data.
  if type(wk._queue) == "table" and #wk._queue > 0 then
    vim.notify(
      "[symmetria-whichkey] " .. #wk._queue
        .. " which-key.add() spec(s) detected — metadata (icons, group names)"
        .. " is not read by the native overlay. Attach via `desc` on"
        .. " `vim.keymap.set` instead.",
      vim.log.levels.WARN
    )
  end
end

function M.setup()
  if vim.g.symmetria_whichkey_native ~= 1 then
    return
  end

  rebuild("n")

  local grp = vim.api.nvim_create_augroup("SymWhichKey", { clear = true })

  -- Keep the trie + triggers fresh. BufEnter catches buffer-local
  -- maps from plugins attaching per-buffer; LspAttach/LspDetach cover
  -- LSP-installed maps that arrive async after BufEnter. Each event
  -- rebuilds the tree and reconciles the installed trigger keymaps
  -- against the new top-level prefix set.
  vim.api.nvim_create_autocmd({ "BufEnter", "LspAttach", "LspDetach" }, {
    group = grp,
    callback = function()
      rebuild("n")
      Triggers.install("n")
    end,
  })

  -- VimEnter is when user config has finished loading, so keymaps
  -- are stable. Neutralize which-key.nvim (if installed), then
  -- rebuild the trie and install our triggers. Running these in
  -- order inside a single autocmd avoids a race: which-key's own
  -- ModeChanged-driven installer otherwise competes with ours for
  -- the same prefix keymaps, and the loser is undefined.
  vim.api.nvim_create_autocmd("VimEnter", {
    group = grp,
    callback = function()
      neutralize_whichkey_nvim()
      rebuild("n")
      Triggers.install("n")
    end,
  })

  -- Testing hooks. Kept around after the state machine lands — useful
  -- for headless smoke tests that want to force a menu open without
  -- simulating keystrokes. `_G` is the deliberate IPC boundary that
  -- Python hits via `nvim.exec_lua(...)`; see CLAUDE.md gotcha #2.
  -- selene: allow(global_usage)  -- IPC boundary; gotcha #2
  _G.symmetria_whichkey_show = M.show
  -- selene: allow(global_usage)  -- IPC boundary; gotcha #2
  _G.symmetria_whichkey_hide = M.emit_hide
  -- selene: allow(global_usage)  -- IPC boundary; gotcha #2
  _G.symmetria_whichkey_start = function(keys)
    State.start({ keys = keys or " " })
  end

  -- Silence unused-local linter noise — the locals exist for side-
  -- effect module loading, not for direct reference.
  local _ = Triggers and State and Presets
end

return M
