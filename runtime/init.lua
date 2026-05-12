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

function M.push_state()
  push_mode(vim.api.nvim_get_mode().mode)
  emit_capsule("project", "", project_name())
  emit_capsule("branch", "", git_branch())
  emit_capsule("file", "", file_display())
  emit_capsule("pos", "", cursor_position())
  -- `cwd` is NOT a status-bar field — it's intercepted on the Python
  -- side by AppController._route_capsule before reaching StatusBarState
  -- and routed to controller.cwd, which the sidebar's FileTreeView
  -- binds against as rootPath. Emitted alongside the rest of push_state
  -- so the DirChanged/BufEnter/VimEnter triggers retarget the sidebar
  -- without a second autocmd plumbing.
  emit_capsule("cwd", "", vim.fn.getcwd())
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

-- --- Agent-pane triggers ---------------------------------------------
--
-- Hijack the `<leader>aN` / `<leader>an` slots (orchestrator.nvim's
-- "New Claude" / "New Claude skip perms") so they open the native
-- agent pane instead of orchestrator's terminal launcher.
--
-- Hijack discipline (gotchas #17, #21):
--   1. orchestrator.nvim is commonly lazy-loaded via `lazy.nvim`'s
--      `keys = {"<leader>a*", ...}` spec, which means the plugin
--      registers its keymaps ON FIRST KEYPRESS — strictly AFTER our
--      `VimEnter+schedule` install completes. A plain install loses
--      the race on that first-tap reload.
--   2. Countermeasure: verify ownership via `vim.fn.maparg` on every
--      `BufEnter` and re-install if our desc isn't there. Trusting
--      an internal install cache lies after the lazy reload; only
--      `maparg` tells the truth (same reasoning as gotcha #17's
--      whichkey-menu / trigger self-heal).
--   3. Observability: the handler `vim.notify`s on fire so the
--      operator can confirm it ran (orchestrator's terminal flow
--      runs silently, so absent our notify = our handler didn't run).
--
-- Continue / Resume Claude routing (`<leader>aC`, `<leader>aR`) is
-- deferred until `SessionHost` supports `claude -c` / `claude -r`
-- flags — orchestrator's terminal flow handles those slots today.

local SYMMETRIA_AGENT_DESC = "New Claude (Symmetria agent pane)"
-- HACK: <leader>an is wired to the same handler as <leader>aN; "skip perms"
-- semantics (orchestrator's --dangerously-skip-permissions equivalent) are
-- deferred until SessionHost supports passing that flag through to `claude`.
-- Keeping the slot separate (with its own desc) so the eventual divergence
-- is a code change, not a keymap-table surgery.
local SYMMETRIA_AGENT_DESC_SKIP = "New Claude skip perms (Symmetria agent pane)"
-- Phase B keybinds — focus an instance by slot, close the focused instance.
-- Distinct desc strings so `slot_owned_by_us` can recognize ANY of our
-- keymaps without the descriptor-table growing into a special-case forest.
local SYMMETRIA_AGENT_DESC_FOCUS = "Focus Claude instance (Symmetria agent pane)"
local SYMMETRIA_AGENT_DESC_CLOSE = "Close focused Claude instance (Symmetria agent pane)"

-- Set keyed by descriptor string so a future divergence (e.g. focus
-- handlers needing a different self-heal cadence) can branch on the
-- desc without pattern-matching strings.
local SYMMETRIA_AGENT_DESCS = {
  [SYMMETRIA_AGENT_DESC] = true,
  [SYMMETRIA_AGENT_DESC_SKIP] = true,
  [SYMMETRIA_AGENT_DESC_FOCUS] = true,
  [SYMMETRIA_AGENT_DESC_CLOSE] = true,
}

local function open_agent_new()
  pcall(vim.rpcnotify, 0, "agent", { op = "show", action = "new" })
end

-- Build a focus handler that emits `{op="focus", index=N}` for the
-- given slot. Closure over `slot` keeps the install loop one-liner.
local function make_focus_handler(slot)
  return function()
    pcall(vim.rpcnotify, 0, "agent", { op = "focus", index = slot })
  end
end

local function close_focused_agent()
  -- No `index` field — AppController interprets a missing index as
  -- "the focused instance", per PRD §5.1's dispatch table.
  pcall(vim.rpcnotify, 0, "agent", { op = "close" })
end

local function slot_owned_by_us(keys)
  local mapping = vim.fn.maparg(keys, "n", false, true)
  if type(mapping) ~= "table" or vim.tbl_isempty(mapping) then
    return false
  end
  local desc = mapping.desc or ""
  return SYMMETRIA_AGENT_DESCS[desc] == true
end

local function install_agent_keymaps(reason)
  local wanted = {
    { keys = "<leader>aN", desc = SYMMETRIA_AGENT_DESC, handler = open_agent_new },
    { keys = "<leader>an", desc = SYMMETRIA_AGENT_DESC_SKIP, handler = open_agent_new },
    -- <C-S-q> closes the focused instance. Standalone CTRL+SHIFT chord —
    -- no longer-prefix ambiguity, but we still install buffer-local +
    -- nowait for parity with the rest of the agent keymaps. Kept as a
    -- single binding to match orchestrator.nvim's mental model (one
    -- close binding regardless of which instance is focused).
    { keys = "<C-S-q>", desc = SYMMETRIA_AGENT_DESC_CLOSE, handler = close_focused_agent },
  }
  -- <C-1>..<C-5> → focus the corresponding pool slot. CTRL+digit is
  -- one of the few chord forms Qt reliably surfaces to embedded nvim
  -- without terminal-layer ambiguity (terminals typically swallow it,
  -- but we render via direct Qt key events through `keys.py`). Each
  -- handler captures its slot via `make_focus_handler` so the install
  -- loop stays uniform.
  for slot = 1, 5 do
    table.insert(wanted, {
      keys = string.format("<C-%d>", slot),
      desc = SYMMETRIA_AGENT_DESC_FOCUS,
      handler = make_focus_handler(slot),
    })
  end
  for _, spec in ipairs(wanted) do
    if not slot_owned_by_us(spec.keys) then
      -- `buffer = 0` + `nowait = true` per gotcha #20: `nowait` is only
      -- honored on buffer-local maps when a competing longer map exists
      -- globally (which is exactly orchestrator.nvim's situation for
      -- <leader>a*). The BufEnter autocmd above re-installs on every
      -- buffer transition, which is the right lifecycle for buffer-local
      -- keymaps (they die with their buffer).
      vim.keymap.set("n", spec.keys, spec.handler, {
        buffer = 0,
        silent = true,
        nowait = true,
        desc = spec.desc,
      })
      -- Diagnostic trail — visible in Python's app log via the
      -- existing `agent` rpcnotify channel.
      -- pcall intentional: rpcnotify fails if Python client is disconnected
      -- (same pattern as capsule/scroll/completions emitters — see emit_capsule).
      pcall(vim.rpcnotify, 0, "agent", {
        op = "debug",
        event = "keymap_install",
        keys = spec.keys,
        reason = reason,
      })
    end
  end
end

local agent_grp = vim.api.nvim_create_augroup("symmetria_agent_keys", { clear = true })

-- First install: VimEnter + vim.schedule (defers one tick past
-- plugin-setup order so we land last among non-lazy plugins).
vim.api.nvim_create_autocmd("VimEnter", {
  group = agent_grp,
  callback = function()
    vim.schedule(function()
      install_agent_keymaps("VimEnter")
    end)
  end,
})

-- Self-heal on every buffer transition. Cheap reconciliation point
-- for the general case where some plugin re-registers at an
-- unpredictable lifecycle event.
vim.api.nvim_create_autocmd("BufEnter", {
  group = agent_grp,
  callback = function()
    install_agent_keymaps("BufEnter")
  end,
})

-- Self-heal whenever lazy.nvim finishes loading orchestrator. The plugin
-- is commonly lazy-loaded via `keys = {"<leader>a*", ...}`, which means
-- its keymaps land WHEN THE USER FIRST PRESSES <leader>a — a window
-- BufEnter cannot cover. `User LazyLoad` fires synchronously inside
-- lazy.nvim's load path, BEFORE the keypress is re-dispatched for
-- lookup, so installing here wins the slot back for the trailing `N`.
--
-- Narrowed to orchestrator-matching plugin names (lazy.nvim passes the
-- plugin spec name as `ev.data`) so the broad-hook install cost stays
-- zero for the 30–80 other plugins that LazyLoad per session. BufEnter
-- self-heal remains the fallback if the plugin is ever renamed.
vim.api.nvim_create_autocmd("User", {
  group = agent_grp,
  pattern = "LazyLoad",
  callback = function(ev)
    local name = ev.data or ""
    if type(name) == "string" and name:lower():find("orchestrator", 1, true) then
      install_agent_keymaps("LazyLoad:" .. name)
    end
  end,
})

-- ============================================================================
-- File manager toggle-overlay keybind
-- ============================================================================
--
-- <leader>e (mnemonic: "explorer") toggles the embedded Symmetria File
-- Manager overlay. The overlay floats above NvimView with a dim scrim
-- behind it and dismisses on Escape. AppController owns the visibility
-- state; this keybind only emits the rpcnotify event.
--
-- Initial path is the current buffer's directory if it exists on disk,
-- otherwise nvim's cwd. Sending the path explicitly lets the overlay
-- boot into the right directory without an extra RPC roundtrip.
--
-- Hijack discipline (mirrors <leader>aN at line 533+):
--   1. The user's nvim config commonly registers <leader>e as a
--      which-key group declaration ("File explorers") in core/keymaps.lua,
--      and lazy-loaded plugins like yazi.nvim add <leader>ey / <leader>eY
--      via their `keys = {...}` spec. Both land AFTER our --cmd dofile of
--      this file, so a plain file-scope `vim.keymap.set` is overwritten.
--   2. Countermeasure: install on `VimEnter` (deferred via vim.schedule
--      so we land last among non-lazy plugins) and self-heal on
--      `BufEnter` (cheap reconciliation point + buffer-local keymaps die
--      with their buffer, so we need to re-install per-buffer anyway).
--   3. `buffer = 0` + `nowait = true` per gotcha #20: nowait only honors
--      buffer-local maps, and yazi's <leader>ey/eY are longer global maps
--      that would otherwise force a timeoutlen wait at <leader>e.

local SYMMETRIA_FM_DESC = "Toggle Symmetria File Manager overlay"

local function open_file_manager()
  local buf_path = vim.api.nvim_buf_get_name(0)
  local initial_path = ""
  if buf_path ~= "" then
    -- Prefer the buffer's parent directory so users navigating from
    -- a file see siblings, not the project root.
    local parent = vim.fn.fnamemodify(buf_path, ":p:h")
    if parent ~= "" and vim.fn.isdirectory(parent) == 1 then
      initial_path = parent
    end
  end
  if initial_path == "" then
    initial_path = vim.fn.getcwd()
  end
  pcall(vim.rpcnotify, 0, "fm", {
    op = "toggle",
    initialPath = initial_path,
  })
end

-- HACK: <C-u> is a temporary escape-hatch test binding while the <leader>e
-- hijack fight against yazi.nvim's lazy-loaded children is still being worked
-- out. <C-u> shadows nvim's built-in half-page scroll-up — accepted as a
-- temporary testing tradeoff. Remove <C-u> from this list (and from the
-- CLAUDE.md comment block above) once <leader>e wins reliably.
local SYMMETRIA_FM_KEYS = { "<leader>e", "<C-u>" }

local function fm_slot_owned_by_us(keys)
  local mapping = vim.fn.maparg(keys, "n", false, true)
  if type(mapping) ~= "table" or vim.tbl_isempty(mapping) then
    return false
  end
  return (mapping.desc or "") == SYMMETRIA_FM_DESC
end

local function install_fm_keymap(reason)
  reason = reason or "unknown"
  for _, keys in ipairs(SYMMETRIA_FM_KEYS) do
    if not fm_slot_owned_by_us(keys) then
      vim.keymap.set("n", keys, open_file_manager, {
        buffer = 0,
        silent = true,
        nowait = true,
        desc = SYMMETRIA_FM_DESC,
      })
      -- Diagnostic trail — mirrors install_agent_keymaps pattern so the
      -- operator can confirm the FM hijack ran. Especially useful for
      -- confirming <C-u> is active while the fight with yazi is unresolved.
      -- pcall intentional: rpcnotify fails if Python client is disconnected.
      pcall(vim.rpcnotify, 0, "fm", {
        op = "debug",
        event = "keymap_install",
        keys = keys,
        reason = reason,
      })
    end
  end
end

local fm_grp = vim.api.nvim_create_augroup("symmetria_fm_keys", { clear = true })

vim.api.nvim_create_autocmd("VimEnter", {
  group = fm_grp,
  callback = function()
    vim.schedule(function()
      install_fm_keymap("VimEnter")
    end)
  end,
})

vim.api.nvim_create_autocmd("BufEnter", {
  group = fm_grp,
  callback = function()
    install_fm_keymap("BufEnter")
  end,
})

-- Self-heal when yazi.nvim (or any other FM plugin) lazy-loads via its
-- `keys = {"<leader>ey", ...}` spec. lazy.nvim fires `User LazyLoad`
-- synchronously inside its load path, BEFORE the triggering keypress is
-- re-dispatched — so we win back the <leader>e slot for the trailing stroke.
-- Narrowed to FM-plugin names so the install cost is zero for other lazy loads.
-- BufEnter self-heal remains the fallback if a plugin uses a different name.
vim.api.nvim_create_autocmd("User", {
  group = fm_grp,
  pattern = "LazyLoad",
  callback = function(ev)
    local name = ev.data or ""
    if type(name) == "string" then
      local lower = name:lower()
      if
        lower:find("yazi", 1, true)
        or lower:find("neo-tree", 1, true)
        or lower:find("nvim-tree", 1, true)
        or lower:find("oil", 1, true)
      then
        install_fm_keymap("LazyLoad:" .. name)
      end
    end
  end,
})

-- ============================================================================
-- File-tree sidebar focus keybind
-- ============================================================================
--
-- <leader>tf (mnemonic: "tree focus") moves active focus into the
-- always-on file-tree sidebar. Sidebar visibility itself is managed by
-- AppController.treeVisible (defaults to True, no v1 toggle keybind per
-- the "visualization-first" scope decision).
--
-- This is a SEPARATE hijack surface from <leader>e / install_fm_keymap.
-- The two are conceptually independent — sidebar focus vs FM overlay
-- toggle compete with different plugin sets (nvim-tree / neo-tree
-- contend for <leader>t* prefixes; yazi.nvim contends for <leader>e).
-- Consolidating into one install function would couple lifecycle
-- decisions that should evolve separately (see CLAUDE.md's
-- `slot_owned_by_us` vs `fm_slot_owned_by_us` reasoning for the same
-- pattern applied to agent and FM hijacks).

local SYMMETRIA_TREE_DESC = "Focus Symmetria file-tree sidebar"

local SYMMETRIA_TREE_KEYS = { "<leader>tf" }

local function focus_file_tree()
  pcall(vim.rpcnotify, 0, "tree", { op = "focus" })
end

local function tree_slot_owned_by_us(keys)
  local mapping = vim.fn.maparg(keys, "n", false, true)
  if type(mapping) ~= "table" or vim.tbl_isempty(mapping) then
    return false
  end
  return (mapping.desc or "") == SYMMETRIA_TREE_DESC
end

local function install_tree_keymap(reason)
  reason = reason or "unknown"
  for _, keys in ipairs(SYMMETRIA_TREE_KEYS) do
    if not tree_slot_owned_by_us(keys) then
      vim.keymap.set("n", keys, focus_file_tree, {
        buffer = 0,
        silent = true,
        nowait = true,
        desc = SYMMETRIA_TREE_DESC,
      })
      -- Diagnostic trail — mirrors install_fm_keymap so the operator
      -- can confirm the tree hijack ran via the app log.
      -- pcall intentional: rpcnotify fails if Python client is disconnected.
      pcall(vim.rpcnotify, 0, "tree", {
        op = "debug",
        event = "keymap_install",
        keys = keys,
        reason = reason,
      })
    end
  end
end

local tree_grp = vim.api.nvim_create_augroup("symmetria_tree_keys", { clear = true })

vim.api.nvim_create_autocmd("VimEnter", {
  group = tree_grp,
  callback = function()
    vim.schedule(function()
      install_tree_keymap("VimEnter")
    end)
  end,
})

vim.api.nvim_create_autocmd("BufEnter", {
  group = tree_grp,
  callback = function()
    install_tree_keymap("BufEnter")
  end,
})

-- Self-heal when nvim-tree / neo-tree / other <leader>t*-claiming plugins
-- lazy-load. Same pattern as install_fm_keymap's LazyLoad self-heal —
-- lazy.nvim's `User LazyLoad` fires synchronously inside its load path
-- BEFORE the triggering keypress is re-dispatched, so we win the slot
-- back for the trailing key in the chord.
vim.api.nvim_create_autocmd("User", {
  group = tree_grp,
  pattern = "LazyLoad",
  callback = function(ev)
    local name = ev.data or ""
    if type(name) == "string" then
      local lower = name:lower()
      if
        lower:find("nvim-tree", 1, true)
        or lower:find("neo-tree", 1, true)
        or lower:find("oil", 1, true)
      then
        install_tree_keymap("LazyLoad:" .. name)
      end
    end
  end,
})

return M
