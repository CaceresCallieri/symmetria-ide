-- Minimap content channel — Lua emitter side.
--
-- Phase 1 of the editor minimap (docs/minimap-prd.md). Pushes the full
-- buffer's line contents from nvim to Python over a new `"minimap"`
-- rpcnotify channel. Python's `MinimapModel` ingests the stream and
-- exposes line count + line accessors that Phase 2's block painter and
-- Phase 5's glyph painter consume.
--
-- Wire shape (mirrored on the Python side as `MinimapModel.apply` —
-- keep in sync if you change either):
--
--   vim.rpcnotify(0, "minimap", {
--     op = "snapshot",
--     bufnr = <number>,
--     line_count = <number>,
--     lines = { "line1", "line2", ... },
--   })
--
--   vim.rpcnotify(0, "minimap", {
--     op = "patch",
--     bufnr = <number>,
--     line_count = <number>,        -- post-patch count
--     first = <number>,             -- 0-indexed start row (inclusive)
--     last = <number>,              -- 0-indexed end row (exclusive)
--     lines = { "new1", "new2" },   -- replacement content for [first, last)
--   })
--
-- Cadence:
--   - BufEnter / BufWritePost                 → immediate snapshot
--   - TextChanged / TextChangedI              → debounced snapshot
--                                               (one per main-loop tick
--                                                via a pending flag —
--                                                rapid keystrokes
--                                                coalesce to one emit
--                                                per ms, sufficient for
--                                                Phase 2's visible
--                                                indent silhouette)
--
-- Patches are NOT emitted in Phase 1 — the wire schema includes the
-- `op = "patch"` envelope so Phase 1.5 / Phase 2 can add diff-style
-- emits without changing the Python contract, but the initial
-- implementation pushes full snapshots on every change. The model
-- side handles both ops from day one.
--
-- Gotchas in play:
--
-- #2  Subscribe race. If `init.lua` fires the first snapshot before
--     Python has subscribed to `"minimap"`, the payload is dropped on
--     the floor. Python force-requests a re-push via
--     `_G.symmetria_minimap_push_snapshot()` immediately after it
--     subscribes — same mitigation as the capsule channel.
--
-- #16 No scheduled callbacks during prefix-wait. `vim.schedule` may
--     not fire while nvim is in a typeahead-wait state mid-mapping.
--     We never call `vim.schedule` from a keymap handler — only from
--     autocmd handlers, which run on safe-state ticks. The pending
--     flag means even if `schedule` is briefly stalled, we don't queue
--     up redundant work.
--
-- Kill switch:
--   `vim.g.symmetria_minimap_emit = 0` disables emissions (the autocmd
--   group is still registered but the dispatchers short-circuit). Leave
--   unset / nonzero to enable.

local M = {}

local AUGROUP = "SymmetriaMinimap"

-- Single coalesce flag. When an event fires, if no emit is currently
-- scheduled, we set `pending = true` and `vim.schedule(emit)`. The
-- scheduled callback clears the flag at the top of its body. Rapid
-- successive events during the same main-loop tick collapse into one
-- emit. This is the "lightweight debounce" mentioned in PRD §5.1 — a
-- real timer-based debounce is a Phase 1.5 follow-up if jank shows up
-- on large buffers.
local pending = false

-- Tracks the last buffer we snapshotted so a fresh BufEnter into the
-- same buffer (e.g. focus regained without a buffer switch) does not
-- redundantly fire — the Python model already has the right state.
-- Cleared on TextChanged so a real edit to the same buffer still emits.
local last_snapshot_bufnr = -1

---Read the buffer's lines + count. Returns nil if the buffer is no
---longer valid (BufEnter fires for buffers that may have been wiped
---by the time the scheduled callback runs).
---@param bufnr integer
---@return string[]?, integer?
local function read_buffer(bufnr)
  if not vim.api.nvim_buf_is_valid(bufnr) then
    return nil, nil
  end
  local lines = vim.api.nvim_buf_get_lines(bufnr, 0, -1, false)
  local line_count = vim.api.nvim_buf_line_count(bufnr)
  return lines, line_count
end

---Send a snapshot envelope. pcall'd so a disconnected Python client
---doesn't cascade into autocmd errors (same pattern the capsule /
---completions / scroll channels use).
---@param bufnr integer
local function emit_snapshot(bufnr)
  local lines, line_count = read_buffer(bufnr)
  if lines == nil then
    return
  end
  pcall(vim.rpcnotify, 0, "minimap", {
    op = "snapshot",
    bufnr = bufnr,
    line_count = line_count,
    lines = lines,
  })
  last_snapshot_bufnr = bufnr
end

---Schedule a coalesced snapshot for the given buffer. Multiple calls
---in the same main-loop tick collapse into one emit on the next tick.
---@param bufnr integer
local function schedule_snapshot(bufnr)
  if vim.g.symmetria_minimap_emit == 0 then
    return
  end
  if pending then
    return
  end
  pending = true
  vim.schedule(function()
    pending = false
    emit_snapshot(bufnr)
  end)
end

---Emit a snapshot for the current buffer immediately, bypassing the
---coalesce flag. Used for BufEnter / BufWritePost where the user is
---visibly switching context and wants the minimap to track instantly.
---Still goes through `vim.schedule` (not direct call) so we don't
---fire mid-autocmd dispatch — nvim's autocmd ordering can otherwise
---leave LSP / treesitter handlers running concurrently with our emit.
---@param bufnr integer
local function schedule_snapshot_immediate(bufnr)
  if vim.g.symmetria_minimap_emit == 0 then
    return
  end
  vim.schedule(function()
    emit_snapshot(bufnr)
  end)
end

---Public re-push hook. Python calls this via `nvim.exec_lua` after it
---subscribes to the "minimap" channel, to plug the subscribe race
---(gotcha #2). Re-emits the current buffer's snapshot unconditionally,
---bypassing both the pending flag and the BufEnter dedup.
-- selene: allow(global_usage)  -- IPC boundary; gotcha #2
function _G.symmetria_minimap_push_snapshot()
  local bufnr = vim.api.nvim_get_current_buf()
  emit_snapshot(bufnr)
end

---Install autocmds. Idempotent — re-running setup() clears the prior
---group and re-installs, so a hot-reload during dev doesn't stack
---duplicate handlers.
function M.setup()
  local grp = vim.api.nvim_create_augroup(AUGROUP, { clear = true })

  -- BufEnter: user switched to a different buffer (or focus regained
  -- on the same one). Emit immediately so the minimap tracks the new
  -- context without waiting for an edit.
  vim.api.nvim_create_autocmd("BufEnter", {
    group = grp,
    callback = function(args)
      if args.buf == last_snapshot_bufnr then
        -- Re-entering the buffer we already pushed — skip the
        -- redundant emit. TextChanged below will reset the dedup
        -- if a real edit happens before the next BufEnter.
        return
      end
      schedule_snapshot_immediate(args.buf)
    end,
    desc = "Symmetria minimap: snapshot on buffer enter",
  })

  -- BufWritePost: user saved. Always emit — saving may have run a
  -- formatter or other autocmd that mutated lines between the last
  -- TextChanged and disk.
  vim.api.nvim_create_autocmd("BufWritePost", {
    group = grp,
    callback = function(args)
      schedule_snapshot_immediate(args.buf)
    end,
    desc = "Symmetria minimap: snapshot on buffer write",
  })

  -- TextChanged / TextChangedI: any edit. Goes through the coalesce
  -- flag so rapid keystrokes don't fire one snapshot per stroke.
  vim.api.nvim_create_autocmd({ "TextChanged", "TextChangedI" }, {
    group = grp,
    callback = function(args)
      -- Reset dedup so a subsequent BufEnter for the same buffer
      -- will still emit (the buffer's content changed since the
      -- last push).
      last_snapshot_bufnr = -1
      schedule_snapshot(args.buf)
    end,
    desc = "Symmetria minimap: snapshot on text change",
  })
end

return M
