-- Event-driven state machine for the native which-key overlay.
--
-- ### Why event-driven, not `getcharstr`
--
-- Which-key.nvim uses a synchronous `vim.fn.getcharstr()` loop inside
-- its state machine (`state.lua:242-275`). That pattern works in a TTY
-- nvim where nvim owns the whole event loop, but deadlocks in our
-- embedded setup: the Lua loop holds nvim's main thread, preventing
-- nvim from flushing RPC notifications to our Python UI client.
-- Symptom: the overlay shows up (first rpcnotify leaks through on the
-- initial show) but subsequent keypress → descent → re-emit never
-- reaches Python, and even the Qt event loop on the Python side
-- eventually stalls.
--
-- The fix is to let nvim's own keymap dispatcher drive transitions.
-- When the menu opens at a node, we install one keymap per direct
-- child (plus `<Esc>` and `<BS>`), and tear them down when the menu
-- closes. Each press dispatches through the normal keymap path so the
-- main thread returns to the event loop between transitions — RPC
-- flushes cleanly.
--
-- This is the SAME invariant the existing cmdline/completion pipeline
-- relies on: all dispatch happens through autocmds + keymaps, never
-- through a blocking Lua loop.
--
-- ### Execution + trigger suspension
--
-- On leaf activation we feed the full key sequence back through nvim
-- so the user's bound action runs. But our top-level trigger keymap
-- (installed by `triggers.lua`) is still live — `feedkeys(" bn", 'm')`
-- would cause `<Space>` to re-trigger the menu before `bn` gets
-- consumed. So we uninstall triggers before feedkeys and re-install
-- them on the next tick. This mirrors which-key's `Triggers.suspend`
-- pattern (`triggers.lua:166-172`).
--
-- ### Re-entry guard
--
-- `M._active` prevents overlapping starts (e.g. a feedkeys race firing
-- the trigger again before the menu closes).

local Tree = require("orchestrator.whichkey.tree")

local M = {}

M._active = false
---@type table?   current trie node the menu is anchored at
M._node = nil
---@type { mode: string, key: string }[]
M._installed_menu_keymaps = {}

local MENU_DESC = "sym-whichkey-menu-key"

---@param mode string
---@param key string
local function install_menu_key(mode, key, handler)
  vim.keymap.set(mode, key, handler, {
    nowait = true,
    silent = true,
    desc = MENU_DESC,
  })
  table.insert(M._installed_menu_keymaps, { mode = mode, key = key })
end

local function clear_menu_keymaps()
  for _, km in ipairs(M._installed_menu_keymaps) do
    pcall(vim.keymap.del, km.mode, km.key)
  end
  M._installed_menu_keymaps = {}
end

-- Tear down the menu and emit hide. Idempotent.
--
-- ### Important: trigger restoration
--
-- When `_install_for` installs a menu keymap at a key that is ALSO a
-- top-level trigger (e.g. `g` as both a prefix AND a child of the `g`
-- menu for the preset "gg → First line"), `vim.keymap.set` REPLACES
-- the trigger keymap. Deleting the menu keymap on close therefore
-- leaves that slot EMPTY — the next press of `g` dispatches nothing,
-- which manifested as "after pressing gg, subsequent g does nothing."
-- Restoring triggers on close ensures the slot is always either our
-- trigger OR a menu keymap — never empty.
function M.close()
  if not M._active then
    return
  end
  clear_menu_keymaps()
  M._active = false
  M._node = nil
  require("orchestrator.whichkey").emit_hide()
  -- Re-install any triggers that got clobbered by the menu keymap
  -- set. `install()` is idempotent and fast (~O(trie-top-level)).
  require("orchestrator.whichkey.triggers").install("n")
end

-- Navigate up one level, closing the menu if we'd hit root.
function M.pop()
  if not M._active or not M._node then
    return
  end
  local parent = M._node.parent
  -- `parent.parent == nil` means parent is the unrenderable root.
  if parent and parent.parent then
    M._install_for(parent)
  else
    M.close()
  end
end

-- Execute a leaf's action by feeding the full key sequence back
-- through nvim. Triggers are suspended across the feedkeys so our
-- top-level trigger doesn't re-fire on the leading <leader>.
---@param node table
-- Execute a leaf by running its key sequence through nvim's `:normal`
-- command.
--
-- ### Why `:normal` instead of feedkeys
--
-- `feedkeys` queues keys into typeahead and returns immediately — the
-- actual processing happens later on the event loop. That created two
-- problems in our embedded setup:
--
--   1. If we uninstalled the trigger and used feedkeys with `m`
--      (remap), any cleanup scheduled via `vim.schedule` /
--      `vim.defer_fn` / `CursorMoved` autocmd wouldn't fire reliably
--      — nvim ends up in prefix-wait state with trailing chars in
--      typeahead, blocking those callbacks indefinitely. Result:
--      "press gg → native runs, next g does nothing" bug.
--   2. If we kept the trigger installed (`nowait = false`) and used
--      feedkeys with "m", there was a 500ms `timeoutlen` wait before
--      the FIRST `g` fired the trigger and opened the menu.
--
-- `vim.cmd.normal` runs the keys SYNCHRONOUSLY — control returns
-- after nvim has fully processed them. No deferred cleanup required.
-- We use `bang = true` (equivalent to `:normal!`) for preset
-- built-ins like `gg`, `gf`, `gU` so they run as native motions even
-- when our trigger at `g` is installed (`:normal!` bypasses user
-- keymaps). For user keymap leaves we use `bang = false` (no !) so
-- the user's action resolves through their mapping — the trigger
-- keymap has `nowait = true` but my own keymap is SHORTER than the
-- user's (`g` vs `gcc`), and `:normal` resolves the longest map, so
-- we don't recurse.
--
-- Known edge: this is a normal-mode-only synchronous execution. If a
-- leaf's action changes modes (e.g. `i` to enter insert), `:normal`
-- will enter insert then exit when done. For C4 that's acceptable;
-- full mode preservation would need a different strategy.
local function execute_leaf(node)
  local has_real_action = (type(node.rhs) == "string" and node.rhs ~= "")
    or type(node.callback) == "function"
  local ok, err = pcall(vim.cmd.normal, {
    args = { node.keys },
    bang = not has_real_action,
  })
  if not ok then
    vim.notify(
      "[symmetria-whichkey] failed to execute `" .. tostring(node.keys)
        .. "`: " .. tostring(err),
      vim.log.levels.WARN
    )
  end
end

-- Handler invoked when the user presses a known child key in the menu.
---@param key string
local function on_child_pressed(key)
  if not M._active or not M._node then
    return
  end
  local child = M._node.children[key]
  if not child then
    M.close()
    return
  end
  if Tree.is_group(child) then
    M._install_for(child)
  else
    M.close()
    execute_leaf(child)
  end
end

-- (Re)install the menu keymap set for a given node, then emit show.
---@param node table
function M._install_for(node)
  clear_menu_keymaps()
  M._node = node

  for key, _child in pairs(node.children) do
    install_menu_key("n", key, function()
      on_child_pressed(key)
    end)
  end

  install_menu_key("n", "<Esc>", function() M.close() end)
  -- <BS> pops; at the depth-1 layer (direct <leader> menu) it closes
  -- instead, matching the can_go_back semantics used in init.lua.
  install_menu_key("n", "<BS>", function() M.pop() end)

  require("orchestrator.whichkey").show(node.keys)
end

---@param opts { keys: string }
function M.start(opts)
  if M._active then
    return
  end
  opts = opts or {}
  local emitter = require("orchestrator.whichkey")
  local root = emitter.tree()
  local path = Tree.split_keys(opts.keys or "")
  local node = Tree.find(root, path)
  if not node or not Tree.is_group(node) then
    return
  end
  M._active = true
  M._install_for(node)
end

return M
