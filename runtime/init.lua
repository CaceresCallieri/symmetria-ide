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

-- Well-known detection flag for the user's config to branch on.
-- Plugins that also claim `ext_cmdline`/`ext_popupmenu` (noice.nvim,
-- etc.) should disable their cmdline/popupmenu modules when this is
-- set so their output doesn't pollute the grid. Follows the convention
-- established by goneovim (`g:goneovim`) and fvim.
--
-- Example in user config:
--   if vim.g.symmetria_ide == 1 then
--     require("noice").setup({
--       cmdline = { enabled = false },
--       popupmenu = { enabled = false },
--     })
--   end
vim.g.symmetria_ide = 1

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

    -- Force-disable nvim-cmp's cmp-cmdline source. That plugin draws
    -- its own floating window anchored to the default cmdline position
    -- (bottom row) which becomes an orphaned ghost popup once we've
    -- extracted the cmdline into a centered overlay. It also binds
    -- arrow keys to cycle its completions, which fights our pipeline.
    -- Best-effort; silent no-op if the user doesn't have cmp installed.
    local ok, cmp = pcall(require, "cmp")
    if ok and cmp.setup and cmp.setup.cmdline then
      pcall(cmp.setup.cmdline, ":", { enabled = false })
      pcall(cmp.setup.cmdline, "/", { enabled = false })
      pcall(cmp.setup.cmdline, "?", { enabled = false })
    end

    M.push_state()
  end,
})

-- Cursor position changes are high-frequency; emit only the cheap
-- position capsule on motion, not the full payload.
vim.api.nvim_create_autocmd({ "CursorMoved", "CursorMovedI" }, {
  group = grp,
  callback = function() M.push_position() end,
})

-- --- Cmdline completion pipeline ------------------------------------
--
-- We emit our own completion list via `getcompletion()` on every
-- cmdline keystroke. This is independent of whatever plugin the user
-- has for cmdline completion (nvim-cmp, wilder, noice) — so the
-- overlay behaves the same for any user configuration.
--
-- Plugins that draw their own cmdline completion window (nvim-cmp's
-- `cmp-cmdline`, wilder.nvim) should be disabled when `g:symmetria_ide
-- == 1` or their floating windows will render in the grid anchored to
-- the wrong place (the default cmdline position, bottom row).

-- Cached items from the last fresh `getcompletion`. Kept stable across
-- cycle steps so Tab navigation doesn't collapse the popup to a single
-- row each time the cmdline text changes to the chosen completion.
--
-- The cycle/typing distinction is made by text matching: if the new
-- cmdline content equals one of our cached items, a cycle just
-- happened (either via our cycle_completion or external wildmenu),
-- so reuse the list and emit the matching row index as selected.
-- Otherwise the user typed — recompute. This is robust against
-- whether CmdlineChanged fires synchronously during setcmdline or
-- deferred to the next event loop tick.
--
-- We deliberately do NOT keep a global selected_idx. cycle_completion
-- derives position from cmdline text each call, so the source of
-- truth stays the cmdline itself.
local last_items = {}

local function emit_completions()
  local line = vim.fn.getcmdline()

  -- Cycle detection by equality — if the cmdline content matches one
  -- of the cached items, keep the list stable and report the index.
  if #last_items > 0 then
    for i, item in ipairs(last_items) do
      if item == line then
        pcall(vim.rpcnotify, 0, "completions", {
          items = last_items,
          line = line,
          selected = i - 1, -- 0-indexed for Qt model consumption
        })
        return
      end
    end
  end

  -- Fresh typing — recompute completions.
  local ok, fresh = pcall(vim.fn.getcompletion, line, "cmdline")
  if not ok then
    fresh = {}
  end
  last_items = fresh
  pcall(vim.rpcnotify, 0, "completions", {
    items = fresh,
    line = line,
    selected = -1,
  })
end

local function cycle_completion(direction)
  local n = #last_items
  if n == 0 then
    return
  end

  -- Locate our current position by matching the live cmdline against
  -- the cached list. -1 means "not in the list" (user was typing, not
  -- cycling yet) — first Tab jumps to row 0, first S-Tab to the last.
  local current = vim.fn.getcmdline()
  local current_idx = -1
  for i, item in ipairs(last_items) do
    if item == current then
      current_idx = i - 1
      break
    end
  end

  local new_idx
  if direction > 0 then
    new_idx = current_idx < 0 and 0 or ((current_idx + 1) % n)
  else
    new_idx = current_idx < 0 and (n - 1) or ((current_idx - 1 + n) % n)
  end

  -- The subsequent CmdlineChanged fires emit_completions, which will
  -- match the new cmdline against last_items[new_idx+1] and emit the
  -- stable list + correct selected index.
  vim.fn.setcmdline(last_items[new_idx + 1])
end

vim.api.nvim_create_autocmd({ "CmdlineEnter", "CmdlineChanged" }, {
  group = grp,
  callback = emit_completions,
})

vim.api.nvim_create_autocmd("CmdlineLeave", {
  group = grp,
  callback = function()
    last_items = {}
    pcall(vim.rpcnotify, 0, "completions", {
      items = {},
      line = "",
      selected = -1,
    })
  end,
})

-- Install our Tab/S-Tab cmdline keymaps when cmdline opens. Scheduled
-- (vim.schedule) so we run *after* any plugin CmdlineEnter autocmds in
-- the same event loop tick — that means our mapping wins over whatever
-- nvim-cmp / noice.nvim installed this cmdline session.
--
-- `expr = true` lets our handler run side-effects (setcmdline) and then
-- return "" so the original Tab keystroke does nothing further.
vim.api.nvim_create_autocmd("CmdlineEnter", {
  group = grp,
  callback = function()
    vim.schedule(function()
      vim.keymap.set("c", "<Tab>", function()
        cycle_completion(1)
        return ""
      end, { silent = true, expr = true, desc = "Symmetria: cycle completion forward" })
      vim.keymap.set("c", "<S-Tab>", function()
        cycle_completion(-1)
        return ""
      end, { silent = true, expr = true, desc = "Symmetria: cycle completion backward" })
    end)
  end,
})

-- Also emit immediately, since by the time VimEnter fires the UI may
-- already be attached and the capsule panel waiting for its first
-- payload.
M.push_state()

return M
