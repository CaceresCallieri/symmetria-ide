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
-- Preload state + triggers eagerly at init time. If we waited until
-- the keymap callback fired, `require("orchestrator.whichkey.state")`
-- could fail: user config (lazy.nvim in particular) can mutate `rtp`
-- AFTER init.lua runs but BEFORE the trigger fires, so Lua's nvim-rtp
-- searcher may no longer find our runtime/lua/ path. Loading here
-- while our rtp entry is still fresh caches these modules in
-- `package.loaded`, so the later require() hits the cache regardless
-- of what happened to `rtp` in between.
local State = require("orchestrator.whichkey.state")
local Triggers = require("orchestrator.whichkey.triggers")

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
  -- are stable. Install our triggers here (not on require) so we see
  -- the post-config map set.
  vim.api.nvim_create_autocmd("VimEnter", {
    group = grp,
    callback = function()
      rebuild("n")
      Triggers.install("n")
    end,
  })

  -- Testing hooks. Kept around after the state machine lands — useful
  -- for headless smoke tests that want to force a menu open without
  -- simulating keystrokes.
  _G.symmetria_whichkey_show = M.show
  _G.symmetria_whichkey_hide = M.emit_hide
  _G.symmetria_whichkey_start = function(keys)
    State.start({ keys = keys or " " })
  end

  -- Silence unused-local linter noise — the locals exist for side-
  -- effect module loading, not for direct reference.
  local _ = Triggers and State
end

return M
