-- Auto-installs buffer-local nvim keymaps that fire `state.start()`
-- whenever the user presses a top-level prefix (e.g. `<leader>`).
-- Triggers are buffer-local so that `nowait = true` is authoritative
-- (see gotcha #20 in CLAUDE.md — global `nowait` is silently ignored
-- when a longer mapping exists, causing 500–800 ms chord latency).
-- We only need triggers at the ROOT of each prefix chain; the state
-- machine drives all subsequent transitions via its own per-child
-- ephemeral keymaps (see state.lua — no `getcharstr` loop is used).
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
-- ### Per-buffer reconciliation
--
-- After each trie rebuild (BufEnter, LspAttach, LspDetach), we diff
-- the wanted set of triggers against the installed set FOR THE CURRENT
-- BUFFER and add/remove accordingly. Triggers are identified by a
-- `"bufnr:mode:keys"` cache key and tagged with `desc = TRIGGER_DESC`
-- so we can recognize our own work. Stale entries for unloaded buffers
-- age out naturally — buffer-local keymaps die with their buffer.

local Tree = require("orchestrator.whichkey.tree")
local Constants = require("orchestrator.whichkey.constants")

local M = {}

local TRIGGER_DESC = Constants.TRIGGER_DESC

---@type table<string, { bufnr: integer, mode: string, keys: string }>
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
  local bufnr = vim.api.nvim_get_current_buf()
  -- `buffer = bufnr` + `nowait = true` is the only combination that
  -- actually suppresses the `timeoutlen` wait. A global trigger with
  -- `nowait = true` is silently ignored when a longer mapping exists
  -- — LSP's buffer-local `gd`/`gi`/`gr`/`gy`/`gD` and plugin maps
  -- like `gcc` all trigger that case — causing the menu to appear
  -- ~500–800ms late OR a fast `gg` to bypass the menu entirely
  -- (gotcha #20). Triggers are reinstalled per-buffer on
  -- BufEnter/LspAttach/LspDetach by the setup() autocmd in init.lua,
  -- which is already how this reconciler is driven.
  vim.keymap.set(mode, keys, function()
    local ok, err = pcall(require("orchestrator.whichkey.state").start, { keys = keys })
    if not ok then
      vim.notify("[symmetria-whichkey] state.start failed: " .. tostring(err), vim.log.levels.ERROR)
    end
  end, {
    buffer = bufnr,
    nowait = true,
    silent = true,
    desc = TRIGGER_DESC,
  })
  M._installed[bufnr .. ":" .. mode .. ":" .. keys] = { bufnr = bufnr, mode = mode, keys = keys }
end

---@param entry { bufnr: integer, mode: string, keys: string }
local function uninstall_one(entry)
  pcall(vim.keymap.del, entry.mode, entry.keys, { buffer = entry.bufnr })
  M._installed[entry.bufnr .. ":" .. entry.mode .. ":" .. entry.keys] = nil
end

-- Reconcile installed triggers with the current trie's top-level
-- prefixes FOR THE CURRENT BUFFER. Called on BufEnter / LspAttach /
-- LspDetach / VimEnter, and after each menu close — the menu's
-- `_install_for` calls `vim.keymap.set` on keys that are also
-- triggers (e.g. `g` for the `gg → First line` preset leaf), which
-- overwrites the trigger keymap for that buffer. `M._installed` still
-- remembers we "installed" them, so the diff below would skip
-- re-adding. To recover from menu-overwrite, we verify each wanted
-- trigger's CURRENT keymap via `maparg` and reinstall if our
-- TRIGGER_DESC isn't present.
--
-- Per-buffer: triggers are buffer-local (gotcha #20), so the cache
-- key is `"bufnr:mode:keys"` and reconciliation runs against the
-- current buffer only. Stale entries in other buffers persist until
-- those buffers are unloaded; that's harmless — buffer-local keymaps
-- die with their buffer.
---@param mode string
function M.install(mode)
  mode = mode or "n"
  local root = require("orchestrator.whichkey").tree()
  local bufnr = vim.api.nvim_get_current_buf()

  ---@type table<string, true>
  local wanted = {}
  for key, child in pairs(root.children) do
    if Tree.is_group(child) then
      wanted[key] = true
    end
  end

  -- Remove triggers for THIS buffer no longer wanted (prefix
  -- disappeared from the trie). Other buffers' cached entries are
  -- left alone — they'll age out with their buffer.
  for _, entry in pairs(M._installed) do
    if entry.bufnr == bufnr and entry.mode == mode and not wanted[entry.keys] then
      uninstall_one(entry)
    end
  end

  -- Add or restore triggers for this buffer.
  for key in pairs(wanted) do
    local current = vim.fn.maparg(key, mode, false, true)
    local ours_present = type(current) == "table"
      and current.desc
      and current.desc:find(TRIGGER_DESC, 1, true) ~= nil
    if not ours_present then
      -- The slot either was never ours or got clobbered. Clear any
      -- non-user-intended stale map first (respecting its own scope),
      -- then install fresh as buffer-local.
      if
        type(current) == "table"
        and not vim.tbl_isempty(current)
        and not user_has_real_mapping(mode, key)
      then
        local del_opts = (type(current.buffer) == "number" and current.buffer > 0)
            and { buffer = current.buffer }
          or nil
        pcall(vim.keymap.del, mode, key, del_opts)
      end
      if not user_has_real_mapping(mode, key) then
        install_one(mode, key)
      end
    end
  end
end

-- Tear down everything. Used by C4 (kill-switch path) and on tests.
function M.uninstall_all()
  for _, entry in pairs(M._installed) do
    uninstall_one(entry)
  end
  M._installed = {}
end

return M
