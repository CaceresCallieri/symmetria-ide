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

-- Last mode label we emitted to the UI. Used as a dedup cache so the
-- SafeState reconciliation hook below only fires rpcnotify when the
-- label actually drifted.
local last_emitted_mode = nil

-- Emit the mode capsule only if the label changed since our last
-- emission. Keeps SafeState reconciliation cheap (no-op when already
-- in sync) and keeps ModeChanged correct even when nvim reports
-- compound states (`niI`, `nt`, `no`...) that collapse to the same
-- label as their parent mode.
local function push_mode(mode_str)
  local label = mode_label(mode_str)
  if label ~= last_emitted_mode then
    last_emitted_mode = label
    emit_capsule("mode", "", label)
  end
end

function M.push_state()
  push_mode(vim.api.nvim_get_mode().mode)
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

-- Mode capsule hygiene. Two-layer defense against drift between what
-- the UI shows and what NeoVim is actually doing:
--
--   1. ModeChanged: use `v:event.new_mode` (the authoritative transition
--      payload), NOT a live `nvim_get_mode()` re-query. The re-query
--      can return compound states (`niI`, `no`, `nt`) captured at a
--      different instant than the transition fired, which previously
--      caused the UI to occasionally display INSERT after the user
--      had already returned to NORMAL (e.g. after a plugin-initiated
--      buffer switch via `gf` into a preview buffer).
--   2. SafeState: fires whenever NeoVim enters the input-wait state
--      (i.e. between keystrokes). Re-emits the mode capsule if it
--      differs from last-emitted — this reconciles any drift from
--      paths we didn't hook (plugin `nvim_feedkeys` with `n` flag,
--      compound state transitions, etc.). `push_mode` dedups, so this
--      is a no-op cost on every settled tick after the first correct
--      emit.
vim.api.nvim_create_autocmd("ModeChanged", {
  group = grp,
  callback = function()
    push_mode(vim.v.event.new_mode or vim.api.nvim_get_mode().mode)
  end,
})

vim.api.nvim_create_autocmd("SafeState", {
  group = grp,
  callback = function()
    push_mode(vim.api.nvim_get_mode().mode)
  end,
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
      -- `enabled = false` is NOT read per-cmdline by nvim-cmp — only the
      -- global `enabled` flag is checked. Setting sources = {} is the
      -- correct way to suppress cmp's cmdline popup for these modes.
      pcall(cmp.setup.cmdline, ":", { sources = {} })
      pcall(cmp.setup.cmdline, "/", { sources = {} })
      pcall(cmp.setup.cmdline, "?", { sources = {} })
    end

    M.push_state()
  end,
})

-- --- Viewport scroll tracking ---------------------------------------
--
-- `grid_scroll` events (the nvim redraw-protocol optimization) only
-- fire when nvim decides that shifting existing cells is cheaper than
-- re-drawing them. For scroll-down (Ctrl-d) this is the common path,
-- but for scroll-up (Ctrl-u) nvim often just re-transmits the whole
-- viewport via grid_line events without emitting any grid_scroll. That
-- leaves a UI-side scroll animation that relies on grid_scroll blind
-- to Ctrl-u.
--
-- WinScrolled is an nvim *semantic* event — it fires whenever the
-- visible viewport (topline) changes, regardless of how nvim chose to
-- redraw. We compute the line delta from the topline (`line('w0')`)
-- and push it over rpcnotify; Python uses this as the authoritative
-- scroll signal for animation.
--
-- `ext_multigrid` + `win_viewport.scroll_delta` would be the
-- canonical path (what Neovide does), but turning on multigrid means
-- each window becomes a separate grid — a larger architectural shift.
-- This autocmd-based approach is a single-grid equivalent.
local last_topline = nil
local last_buf = nil

vim.api.nvim_create_autocmd("WinScrolled", {
  group = grp,
  callback = function()
    local buf = vim.api.nvim_get_current_buf()
    local topline = vim.fn.line("w0")
    if last_topline ~= nil and buf == last_buf then
      local delta = topline - last_topline
      if delta ~= 0 then
        pcall(vim.rpcnotify, 0, "scroll", { delta = delta })
      end
    end
    last_buf = buf
    last_topline = topline
  end,
})

-- Reset the tracking baseline when the active buffer or window
-- changes — otherwise the first WinScrolled in a new buffer would
-- treat the switch as a huge delta and animate a long flourish.
vim.api.nvim_create_autocmd({ "BufEnter", "WinEnter" }, {
  group = grp,
  callback = function()
    last_buf = vim.api.nvim_get_current_buf()
    last_topline = vim.fn.line("w0")
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

  -- Skip completions when the cmdline is empty (e.g. just opened with `:`)
  -- to avoid flooding the model with the full command set (~300 items)
  -- before the user has typed anything.
  if line == "" then
    last_items = {}
    pcall(vim.rpcnotify, 0, "completions", { items = {}, line = "", selected = -1 })
    return
  end

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
