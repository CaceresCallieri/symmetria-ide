-- Symmetria IDE runtime.
--
-- This file is loaded by the embedded NeoVim (via the `--cmd luafile`
-- arg Python passes). It emits the status-bar data ("capsules") the
-- wrapper UI renders — mode, buffer, branch, project, cursor position.
-- It also hides NeoVim's native status line (lualine etc.) from the
-- viewport since we render the same info natively.
--
-- Protocol (what orchestrator.nvim must match when it replaces this):
--
--   rpcnotify(0, "capsule", { id = "...", label = "...", value = "..." })

local M = {}

-- Hide the native statusline and mode echo — the wrapper's QML status
-- bar owns that real estate. Setting these both here AND in a VimEnter
-- autocmd below is intentional: the early set lets the screen start
-- out clean, and the VimEnter re-set re-asserts our choice after the
-- user's plugins (lualine, etc.) run and clobber `laststatus`.
vim.opt.laststatus = 0
vim.opt.showmode = false

local function emit_capsule(id, label, value)
  pcall(vim.rpcnotify, 0, "capsule", {
    id = id,
    label = label,
    value = value,
  })
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

local function project_name()
  local cwd = vim.fn.getcwd()
  return vim.fn.fnamemodify(cwd, ":t")
end

local function git_branch()
  -- Read .git/HEAD directly — avoids spawning a git subprocess on
  -- every BufEnter. Handles both normal refs (`ref: refs/heads/main`)
  -- and detached HEAD (just a 40-char sha).
  local cwd = vim.fn.getcwd()
  local head_path = cwd .. "/.git/HEAD"
  if vim.fn.filereadable(head_path) == 0 then
    return ""
  end
  local lines = vim.fn.readfile(head_path)
  if #lines == 0 then
    return ""
  end
  local ref = lines[1]:match("^ref:%s*refs/heads/(.+)$")
  if ref then
    return ref
  end
  return lines[1]:sub(1, 7) -- detached HEAD short sha
end

local function file_display()
  local full = vim.api.nvim_buf_get_name(0)
  if full == nil or full == "" then
    return "[no name]"
  end
  local cwd = vim.fn.getcwd()
  -- Guard against cwd == "/" where cwd .. "/" would be "//" and the
  -- prefix check would never match any real path.
  if cwd ~= "/" and full:sub(1, #cwd + 1) == cwd .. "/" then
    return full:sub(#cwd + 2)
  end
  return vim.fn.fnamemodify(full, ":~")
end

local function cursor_position()
  local pos = vim.api.nvim_win_get_cursor(0)
  local total = math.max(1, vim.api.nvim_buf_line_count(0))
  local percent = math.floor((pos[1] / total) * 100)
  return string.format("%d:%d/%d %d%%", pos[1], pos[2] + 1, total, percent)
end

function M.push_state()
  emit_capsule("mode", "", mode_label(vim.api.nvim_get_mode().mode))
  emit_capsule("project", "", project_name())
  emit_capsule("branch", "", git_branch())
  emit_capsule("file", "", file_display())
  emit_capsule("pos", "", cursor_position())
end

function M.push_position()
  emit_capsule("pos", "", cursor_position())
end

-- Expose the push function as a global so the Python side can trigger
-- an on-demand re-push after it finishes subscribing (the initial push
-- below fires before Python subscribes, so we need this handshake).
_G.symmetria_push_state = M.push_state

-- Wire autocmds. We push once on each buffer enter / cursor move /
-- mode change; NeoVim coalesces rapid changes so this is cheap.
local grp = vim.api.nvim_create_augroup("SymmetriaIdeCapsules", { clear = true })

vim.api.nvim_create_autocmd({ "BufEnter", "BufWritePost", "DirChanged" }, {
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
    -- Lualine and similar plugins set laststatus during their own
    -- setup, which runs before VimEnter. Re-assert our settings here
    -- so their globals stay but the native status line stays hidden.
    vim.opt.laststatus = 0
    vim.opt.showmode = false
    M.push_state()
  end,
})

-- Cursor position changes are high-frequency; emit only the cheap
-- position capsule on motion, not the full payload.
vim.api.nvim_create_autocmd({ "CursorMoved", "CursorMovedI" }, {
  group = grp,
  callback = function() M.push_position() end,
})

-- Also emit immediately, since by the time VimEnter fires the UI may
-- already be attached and the capsule panel waiting for its first
-- payload.
M.push_state()

return M
