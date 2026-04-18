-- Auto-installs nvim keymaps that fire `state.start()` whenever the
-- user presses a top-level prefix (e.g. `<leader>`). Once inside the
-- state machine, keystrokes are read via `vim.fn.getcharstr()` rather
-- than keymap dispatch, so we only need triggers at the ROOT of each
-- prefix chain — not at every intermediate node.
--
-- ### Coexistence with user mappings
--
-- If the user has a real keymap at a prefix (non-empty rhs or a
-- callback), we skip installing there to avoid stealing their
-- intentional binding. Empty-rhs group labels (the `<leader>b`
-- pattern — rhs = "", desc = "Buffer navigation") count as
-- "suppressible" — those are just which-key-style hints and do no
-- real work, so we safely replace them with our trigger.
--
-- ### Reconciliation on trie rebuild
--
-- After each trie rebuild (BufEnter, LspAttach, LspDetach), we diff
-- the wanted set of triggers against the installed set and
-- add/remove accordingly. Triggers are identified by a "mode:keys"
-- id and tagged with `desc = TRIGGER_DESC` so we can recognize our
-- own work.

local Tree = require("orchestrator.whichkey.tree")

local M = {}

local TRIGGER_DESC = "symmetria-whichkey-trigger"

---@type table<string, { mode: string, keys: string }>
M._installed = {}

---@param mode string
---@param keys string
---@return boolean  true if a non-suppressible user mapping already exists
local function user_has_real_mapping(mode, keys)
  local km = vim.fn.maparg(keys, mode, false, true)
  if not km or vim.tbl_isempty(km) then
    return false
  end
  if km.desc and km.desc:find(TRIGGER_DESC, 1, true) then
    -- Our own previous trigger.
    return false
  end
  if type(km.callback) == "function" then
    return true
  end
  local rhs = km.rhs or ""
  -- Empty rhs is the user's "group label" idiom — safe to shadow.
  if rhs == "" or rhs == "<Nop>" then
    return false
  end
  return true
end

---@param mode string
---@param keys string
local function install_one(mode, keys)
  -- Wrap in vim.schedule so the keymap handler returns IMMEDIATELY.
  -- `state.start` runs its getcharstr loop in the next main-loop tick;
  -- this ensures the initial rpcnotify flushes to the UI channel before
  -- nvim blocks on input, and it keeps Python's Qt event loop from
  -- stalling during the transition from "no menu" to "menu open".
  vim.keymap.set(mode, keys, function()
    local ok, err = pcall(require("orchestrator.whichkey.state").start,
      { keys = keys })
    if not ok then
      vim.notify(
        "[symmetria-whichkey] state.start failed: " .. tostring(err),
        vim.log.levels.ERROR
      )
    end
  end, {
    -- Deliberately `nowait = false` so nvim waits for longer
    -- matches. When execute_leaf later feedkeys a full user sequence
    -- like `<leader>bn` with the `m` flag, nvim matches the USER's
    -- `<leader>bn` keymap (longer) instead of firing our shorter
    -- `<leader>` trigger. Net effect: we pay a `timeoutlen`-wait on
    -- the first key (500ms default) before the menu opens, but in
    -- exchange we don't need to uninstall/reinstall the trigger
    -- around every leaf execution (which was impossible to do
    -- reliably — see state.lua::execute_leaf).
    silent = true,
    desc = TRIGGER_DESC,
  })
  M._installed[mode .. ":" .. keys] = { mode = mode, keys = keys }
end

---@param mode string
---@param keys string
local function uninstall_one(mode, keys)
  pcall(vim.keymap.del, mode, keys)
  M._installed[mode .. ":" .. keys] = nil
end

-- Reconcile installed triggers with the current trie's top-level
-- prefixes. Called after every rebuild.
---@param mode string
function M.install(mode)
  mode = mode or "n"
  local root = require("orchestrator.whichkey").tree()

  ---@type table<string, { mode: string, keys: string }>
  local wanted = {}
  for key, child in pairs(root.children) do
    if Tree.is_group(child) then
      wanted[mode .. ":" .. key] = { mode = mode, keys = key }
    end
  end

  -- Remove triggers no longer wanted (prefix disappeared from the trie).
  for id, t in pairs(M._installed) do
    if not wanted[id] then
      uninstall_one(t.mode, t.keys)
    end
  end

  -- Add new triggers where we don't clash with real user mappings.
  for id, t in pairs(wanted) do
    if not M._installed[id] and not user_has_real_mapping(t.mode, t.keys) then
      install_one(t.mode, t.keys)
    end
  end
end

-- Tear down everything. Used by C4 (kill-switch path) and on tests.
function M.uninstall_all()
  for _, t in pairs(M._installed) do
    uninstall_one(t.mode, t.keys)
  end
  M._installed = {}
end

return M
