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
  -- pcall intentional: rpcnotify fails if the Python client is not yet
  -- connected or has disconnected. Swallowing is correct here; dropped
  -- capsules are recovered by the explicit push_state() re-request
  -- Python makes after subscribing (CLAUDE.md gotcha #2).
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

-- Dedup cache for the mode capsule: track the last label we emitted
-- so redundant pushes become no-ops. Covers two cases:
--   * SafeState reconciliation firing on every idle tick when already
--     in sync (the hot path).
--   * Compound-state transitions that collapse to the same label
--     (`i` -> `niI` -> `i` all map to INSERT).
local last_emitted_mode = nil

local function push_mode(mode_str)
  local label = mode_label(mode_str)
  if label ~= last_emitted_mode then
    last_emitted_mode = label
    emit_capsule("mode", "", label)
  end
end

-- Dedup cache for the cwd capsule. Critical because `push_state` is wired
-- to BufEnter / BufWritePost (line 183), neither of which signal a real
-- cwd change — they fire on every buffer focus or save, while `getcwd()`
-- only changes on `:cd` (or autochdir traversal). Without this dedup,
-- every BufEnter re-emits nvim's CURRENT cwd as a "fresh" capsule, and on
-- the Python side `_route_capsule` accepts it as authoritative — which
-- clobbers any cwd previously set by the terminal pane's OSC 7 sync.
--
-- Concrete repro: user `cd`s in the terminal to repo B; OSC 7 fires and
-- `controller.cwd` becomes B. User then opens a file from the Active
-- Changes pane (which dispatches `:edit <path>` in nvim). BufEnter fires,
-- push_state emits `cwd = vim.fn.getcwd()` — still repo A because nvim's
-- own cwd never changed — and the file tree + git status snap back to A.
-- Deduping on the lua side keeps nvim a well-behaved emitter on the
-- shared cwd channel: it speaks ONLY when its own cwd actually changes
-- (DirChanged real edges, plus the initial VimEnter baseline).
local last_emitted_cwd = nil

local function push_cwd()
  local cwd = vim.fn.getcwd()
  if cwd ~= last_emitted_cwd then
    last_emitted_cwd = cwd
    emit_capsule("cwd", "", cwd)
  end
end

function M.push_state()
  push_mode(vim.api.nvim_get_mode().mode)
  emit_capsule("project", "", project_name())
  emit_capsule("branch", "", git_branch())
  emit_capsule("file", "", file_display())
  emit_capsule("pos", "", cursor_position())
  -- `cwd` is NOT a status-bar field — it's intercepted on the Python
  -- side by AppController._route_capsule before reaching StatusBarState
  -- and routed to controller.cwd, which the sidebar's FileTreeView
  -- binds against as rootPath. Deduped via `push_cwd` so BufEnter ticks
  -- don't re-assert nvim's cwd over a terminal-OSC-7-set value — see the
  -- comment block on `last_emitted_cwd` above for the failure mode.
  push_cwd()
end

function M.push_position()
  emit_capsule("pos", "", cursor_position())
end

-- Expose the push function as a global so the Python side can trigger
-- an on-demand re-push after it finishes subscribing (the initial push
-- below fires before Python subscribes, so we need this handshake).
-- This is the deliberate IPC boundary — see CLAUDE.md gotcha #2.
-- selene: allow(global_usage)
_G.symmetria_push_state = M.push_state

-- Wire autocmds. We push once on each buffer enter / cursor move /
-- mode change; NeoVim coalesces rapid changes so this is cheap.
local grp = vim.api.nvim_create_augroup("SymmetriaIdeCapsules", { clear = true })

vim.api.nvim_create_autocmd({ "BufEnter", "BufWritePost", "DirChanged" }, {
  group = grp,
  callback = function()
    M.push_state()
  end,
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

-- Convert a logical-line range to an approximate display-row count.
-- With `wrap = true` (or folds collapsing ranges of lines into one
-- display row), a WinScrolled delta measured in logical lines does not
-- match the actual grid-row shift that nvim performed. The Python-side
-- scrollback rotation and spring displacement operate on grid rows, so
-- feeding them the logical count produces under/oversized animations
-- and paints scrollback content from the wrong offset during decay.
-- Computing the display-row span directly here — using the same pieces
-- nvim itself uses to lay out wrapped lines (`strdisplaywidth` ÷ window
-- width, with folds and blank lines handled explicitly) — gives Python
-- the unit it was actually designed for.
--
-- Not perfect: ignores 'showbreak' / 'breakindent' / virtual text /
-- extmarks, and approximates Unicode east-asian widths via
-- `strdisplaywidth`. It is accurate to within ±1 row per wrapped line
-- in practice, which is well inside the spring's error tolerance.
local function display_rows_between(lnum_from, lnum_to, text_width)
  -- text_width is the actual wrappable column count: vim.fn.winwidth(0)
  -- minus non-text decorations (number column, sign column, fold column).
  -- WinScrolled only fires for visible, rendered windows so text_width < 1
  -- is a startup-race defense that should never trigger in practice.
  if lnum_from == lnum_to or text_width < 1 then
    return 0
  end
  -- `sign` tracks scroll direction; `rows` accumulates the absolute
  -- display-row count over [lo, hi). Direction is restored at return.
  local sign = 1
  local lo, hi = lnum_from, lnum_to
  if lnum_to < lnum_from then
    sign = -1
    lo, hi = lnum_to, lnum_from
  end
  -- Cap the scan at MAX_SCAN_LINES to bound worst-case cost for
  -- pathological gg/G jumps across thousands of lines. Beyond the cap
  -- we fall back to the logical delta (the pre-fix behavior), which
  -- slightly undershoots the animation but avoids blocking the event
  -- loop for hundreds of ms on huge buffers.
  local MAX_SCAN_LINES = 500
  if hi - lo > MAX_SCAN_LINES then
    return (hi - lo) * sign
  end
  local rows = 0
  local ln = lo
  while ln < hi do
    local fold_end = vim.fn.foldclosedend(ln)
    if fold_end > 0 then
      -- Closed fold: entire range from `ln..fold_end` collapses to one
      -- display row, regardless of how many logical lines are inside.
      rows = rows + 1
      ln = fold_end + 1
    else
      local line = vim.fn.getline(ln)
      local disp = vim.fn.strdisplaywidth(line)
      -- math.max(1, ...) ensures blank lines (disp == 0) count as 1 row,
      -- which is the same as `if disp == 0 then rows + 1` but avoids the
      -- special case: ceil(0 / text_width) = 0 and max(1, 0) = 1.
      rows = rows + math.max(1, math.ceil(disp / text_width))
      ln = ln + 1
    end
  end
  return rows * sign
end

-- Expose the helper as a global so tests (via `nvim.exec_lua`) can
-- exercise it against real nvim state: real `foldclosedend`, real
-- `strdisplaywidth`, real `getwininfo().textoff`. This is the same IPC
-- boundary pattern as `_G.symmetria_push_state` and the whichkey test
-- hooks — deliberate per CLAUDE.md gotcha #2. Not used by the Python
-- runtime; the WinScrolled handler calls the local name directly.
-- selene: allow(global_usage)  -- IPC boundary; gotcha #2
_G.symmetria_display_rows_between = display_rows_between

vim.api.nvim_create_autocmd("WinScrolled", {
  group = grp,
  callback = function()
    local buf = vim.api.nvim_get_current_buf()
    local topline = vim.fn.line("w0")
    if last_topline ~= nil and buf == last_buf then
      local delta_logical = topline - last_topline
      if delta_logical ~= 0 then
        -- With wrap on, logical-line delta ≠ display-row delta. Python's
        -- scrollback/spring expect display rows (that is what the grid
        -- actually shifts). Without wrap, the two are identical, and
        -- without folds, `display_rows_between` reduces to `|delta_logical|`.
        local delta_display
        if vim.wo.wrap then
          -- `getwininfo[1].textoff` is the total width of non-text columns
          -- (line numbers, sign column, fold column). Subtracting it gives
          -- the column count nvim actually wraps at — using raw winwidth(0)
          -- inflates the denominator and systematically underestimates the
          -- display-row count on buffers with number/relativenumber enabled.
          local info = vim.fn.getwininfo(vim.fn.win_getid())
          local text_width = vim.fn.winwidth(0) - (info[1] and info[1].textoff or 0)
          delta_display = display_rows_between(last_topline, topline, text_width)
        else
          -- wrap=off: every logical line is exactly one display row.
          -- delta_display == delta_logical, which is already ~= 0 per outer guard.
          delta_display = delta_logical
        end
        -- pcall intentional: rpcnotify fails if Python client is disconnected.
        pcall(vim.rpcnotify, 0, "scroll", { delta = delta_display })
      end
    end
    -- Unconditional update: keeps the baseline in sync for the next event.
    -- The BufEnter/WinEnter autocmd below primes last_buf/last_topline before
    -- the first WinScrolled fires; this path keeps them current thereafter.
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
  callback = function()
    M.push_position()
  end,
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
  -- All rpcnotify calls below use pcall intentionally: they fail if the
  -- Python client is not yet connected or has disconnected, and silently
  -- dropping a completion update is correct (the cmdline will re-emit on
  -- the next keystroke).
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

-- --- Native which-key overlay ---------------------------------------
--
-- Opt in to the native which-key replacement (see
-- `runtime/lua/orchestrator/whichkey/init.lua`). Kept behind a global
-- flag so it can be disabled quickly if our overlay breaks — unsetting
-- `g:symmetria_whichkey_native` lets stock which-key.nvim run as normal.
vim.g.symmetria_whichkey_native = 1
pcall(function()
  require("orchestrator.whichkey").setup()
end)

-- --- Agent-pane keymap hijacks: REMOVED ------------------------------
--
-- The `<leader>aN` / `<leader>an` / `<C-1>..<C-5>` / `<C-S-q>` slots
-- were previously claimed here to drive the IDE's native AgentPane via
-- `rpcnotify(0, "agent", ...)`. That coupling has been stripped so
-- orchestrator.nvim runs unmolested inside the embedded NeoVim.
--
-- The IDE-side chat infrastructure (AgentPane.qml + SessionHost +
-- sidecar) is preserved and remains driven by AppController's public
-- methods (`spawn_fresh_instance`, `focus_instance`, etc.); the
-- activation chord is intentionally absent until Track-2 picks a
-- non-colliding namespace. See `docs/orchestrator-replacement-prd.md`
-- (shelved) and the "agent backend" section of CLAUDE.md for context.

-- ============================================================================
-- File manager toggle-overlay keybind
-- ============================================================================
--
-- Promoted out of nvim's layer. The Ctrl+E chord is now an IDE-wide
-- ApplicationShortcut declared in qml/Main.qml — see `controller.toggle_fm`.
-- AppController owns visibility state and now derives the initial path from
-- the anchored project root (AppController.displayedRoot), so the FM opens
-- the same way regardless of which pane has focus.
--
-- The `"fm"` rpcnotify envelope (NvimBackend.fm_event → _on_fm_event) is
-- preserved as a stable contract for any future Lua-side opener (e.g. a
-- `:SymFm` user-command), but no auto-installed hijack lives here anymore.

-- ============================================================================
-- File-tree sidebar focus keybind: REMOVED
-- ============================================================================
--
-- The `<leader>tf` hijack was previously installed here to emit
-- `rpcnotify(0, "tree", {op="focus"})` and pull QML focus into the IDE
-- file-tree sidebar. It was redundant with the IDE-wide Ctrl+J/K/H/L
-- ApplicationShortcuts in qml/Main.qml (Ctrl+J focuses the tree
-- directly) and conflicted with nvim-tree / neo-tree / yazi.nvim
-- claims on `<leader>t*`. Removed alongside the agent-pane hijack to
-- keep nvim's keymap layer entirely under the user's plugins' control.

-- ---------------------------------------------------------------------------
-- Window navigation bridge: <C-h/j/k/l> spillover to IDE outer panes
-- ---------------------------------------------------------------------------
-- vim-tmux-navigator (in the user's plugin set) installs <C-h/j/k/l>
-- with edge detection that spills over to tmux when nvim has no window
-- in the requested direction. Inside the IDE host we substitute the
-- spillover destination: same edge-detection idiom
-- (`winnr() == winnr(dir)`), same in-nvim fallback to `wincmd <dir>`,
-- but the at-edge case fires `rpcnotify(0, "nav", ...)` to AppController
-- instead of shelling out to `tmux select-pane`.
--
-- Outside the IDE (terminal nvim) the plugin's keymaps stay in place —
-- runtime/init.lua is on the runtime path only when launched via the IDE
-- (per `set rtp^=...` in the embed spawn), so this file does not load
-- in a normal terminal-nvim session.
--
-- Edge detection (see :h winnr()): `winnr(dir)` returns the window
-- number you'd land on by moving in `dir`, OR the current window when
-- no neighbor exists in that direction. Equality means "I'm at the
-- edge in this direction." This is the canonical idiom used by
-- vim-tmux-navigator itself.
local SYMMETRIA_NAV_DIRS = { h = "left", j = "down", k = "up", l = "right" }

local function nav_handler(key)
  if vim.fn.winnr() ~= vim.fn.winnr(key) then
    vim.cmd("wincmd " .. key)
    return
  end
  -- WORKAROUND: pcall swallows the rpcnotify error when the Python client
  -- has disconnected mid-session (e.g. IDE shutting down while nvim is still
  -- alive). Clean fix would require a session-alive check API from pynvim,
  -- which doesn't exist. Remove this guard once pynvim exposes a synchronous
  -- `is_connected()` predicate or rpcnotify becomes a no-op on dead channels.
  local ok, err = pcall(vim.rpcnotify, 0, "nav", { op = "move", dir = SYMMETRIA_NAV_DIRS[key] })
  if not ok then
    vim.notify("[symmetria] nav rpcnotify failed: " .. tostring(err), vim.log.levels.DEBUG)
  end
end

local function install_nav_keymaps(reason)
  for k, _ in pairs(SYMMETRIA_NAV_DIRS) do
    vim.keymap.set("n", "<C-" .. k .. ">", function()
      nav_handler(k)
    end, { desc = "Symmetria pane nav " .. SYMMETRIA_NAV_DIRS[k], silent = true })
  end
  -- WORKAROUND: pcall swallows the rpcnotify error when Python client is
  -- disconnected; same root cause as the pcall in nav_handler above.
  local dbg_ok, dbg_err = pcall(vim.rpcnotify, 0, "nav", {
    op = "debug",
    event = "keymap_install",
    reason = reason,
  })
  if not dbg_ok then
    vim.notify(
      "[symmetria] nav debug rpcnotify failed: " .. tostring(dbg_err),
      vim.log.levels.DEBUG
    )
  end
end

-- VimEnter timing matches the fm hijack pattern — installs AFTER
-- lazy-loaded plugins (including vim-tmux-navigator) wire their own
-- <C-h/j/k/l>. vim.schedule defers past the autocmd phase per gotcha #21
-- (scheduled callbacks may stall during prefix-wait, but VimEnter isn't
-- prefix-wait, so we're safe here).
local nav_grp = vim.api.nvim_create_augroup("SymmetriaIdeNav", { clear = true })
vim.api.nvim_create_autocmd("VimEnter", {
  group = nav_grp,
  callback = function()
    vim.schedule(function()
      install_nav_keymaps("VimEnter")
    end)
  end,
})
-- LazyLoad self-heal — if vim-tmux-navigator lazy-loads after VimEnter
-- has fired (the plugin is eager-loaded in the current config, but a
-- future `event = "VeryLazy"` would change that), its setup re-installs
-- <C-h/j/k/l> and clobbers our maps. Re-install on the LazyLoad signal.
vim.api.nvim_create_autocmd("User", {
  group = nav_grp,
  pattern = "LazyLoad",
  callback = function(ev)
    local name = ev.data or ""
    if type(name) == "string" and name:lower():find("tmux-navigator", 1, true) then
      install_nav_keymaps("LazyLoad:" .. name)
    end
  end,
})

-- ============================================================================
-- Project-anchor user commands
-- ============================================================================
--
-- `:SymmetriaAnchor [path]` and `:SymmetriaUnanchor` are the programmatic
-- (scripted / macro) surface for the IDE's project-anchor concept.
-- Anchoring pins the file-tree sidebar to a specific project root so the
-- user can navigate freely (via :cd, or — once Phase 2.5 ships — via cd in
-- the embedded terminal pane) without dragging the tree along.
--
-- The PRIMARY trigger for humans is a Qt application-scope shortcut
-- (Ctrl+Shift+A in Main.qml) that works from any pane regardless of
-- focus — anchor is an IDE-level concern, NOT a nvim concept, which is
-- why these commands deliberately do NOT register a `<leader>` keybind.
-- Giving anchor a `<leader>` slot would mis-locate it conceptually
-- (binding it to the editor surface) and burn a slot that should stay
-- nvim's. The user commands stay useful for scripted workflows
-- (autocmds, ad-hoc macros, debugger sessions).
--
-- Routing: emits rpcnotify(0, "anchor", {op = "set"|"clear", path = ...}).
-- AppController._on_anchor_event dispatches op="set" → anchor_to_path
-- and op="clear" → release_anchor. The path arg is optional on "set":
-- when omitted, AppController falls back to anchor_to_current_cwd
-- (which reads `_cwd`, NOT vim's cwd directly, so it stays consistent
-- with what the file tree was displaying at the moment of the command).
--
-- pcall is intentional (same pattern as every other emitter in this file):
-- rpcnotify fails when the Python client is disconnected, and a dropped
-- command is correct behaviour during shutdown/startup races.

vim.api.nvim_create_user_command("SymmetriaAnchor", function(args)
  -- nargs = "?" → args.args is "" when no path given, the path string
  -- when one is provided. Empty string short-circuits to "set" without
  -- path, leaving the choice of root to the Python side (which uses
  -- `_cwd`). `complete = "dir"` gives Tab-completion against directory
  -- names — same UX as `:cd <Tab>`.
  local path = args.args or ""
  local payload = { op = "set" }
  if path ~= "" then
    payload.path = vim.fn.fnamemodify(path, ":p") -- absolute path
  end
  pcall(vim.rpcnotify, 0, "anchor", payload)
end, {
  nargs = "?",
  complete = "dir",
  desc = "Anchor the IDE to a project root (defaults to current cwd)",
})

vim.api.nvim_create_user_command("SymmetriaUnanchor", function()
  pcall(vim.rpcnotify, 0, "anchor", { op = "clear" })
end, {
  desc = "Release the IDE's project anchor; file tree resumes tracking cwd",
})

return M
