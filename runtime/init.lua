-- Symmetria IDE runtime stub for Phase 0.
--
-- This file is loaded by the embedded NeoVim (via the `--cmd luafile`
-- arg the Python side passes). It imitates the capsule-emission side of
-- orchestrator.nvim: any time the buffer or mode changes, we send an
-- rpcnotify "capsule" message up to the wrapper so the native status
-- bar can render it.
--
-- Real orchestrator.nvim will replace this file once the plugin is
-- installed. The protocol here is the contract the plugin must match:
--
--   rpcnotify(0, "capsule", { id = "...", label = "...", value = "..." })

local M = {}

local function emit_capsule(id, label, value)
  pcall(vim.rpcnotify, 0, "capsule", {
    id = id,
    label = label,
    value = value,
  })
end

local function current_buffer_name()
  local name = vim.api.nvim_buf_get_name(0)
  if name == nil or name == "" then
    return "[no name]"
  end
  -- Show only the last path segment — status bars are narrow.
  return name:match("([^/]+)$") or name
end

local function mode_label(mode)
  local t = {
    n = "NORMAL",
    i = "INSERT",
    v = "VISUAL",
    V = "V-LINE",
    ["\22"] = "V-BLOCK", -- Ctrl-V
    s = "SELECT",
    R = "REPLACE",
    c = "COMMAND",
    t = "TERMINAL",
  }
  return t[mode:sub(1, 1)] or mode
end

function M.push_state()
  emit_capsule("buf", "buf", current_buffer_name())
  emit_capsule("mode", "mode", mode_label(vim.api.nvim_get_mode().mode))
end

-- Expose the push function as a global so the Python side can trigger
-- an on-demand re-push after it finishes subscribing (the initial push
-- below fires before Python subscribes, so we need this handshake).
_G.symmetria_push_state = M.push_state

-- Wire autocmds. We push once on each buffer enter / cursor move /
-- mode change; NeoVim coalesces rapid changes so this is cheap.
local grp = vim.api.nvim_create_augroup("SymmetriaIdeCapsules", { clear = true })

vim.api.nvim_create_autocmd({ "BufEnter", "BufWritePost" }, {
  group = grp,
  callback = function() M.push_state() end,
})

vim.api.nvim_create_autocmd("ModeChanged", {
  group = grp,
  callback = function() M.push_state() end,
})

vim.api.nvim_create_autocmd("VimEnter", {
  group = grp,
  callback = function()
    M.push_state()
  end,
})

-- Also emit immediately, since by the time VimEnter fires the UI may
-- already be attached and the capsule panel waiting for its first
-- payload.
M.push_state()

return M
