-- Trie of normal-mode keymaps, built from `vim.api.nvim_get_keymap()`
-- and `vim.api.nvim_buf_get_keymap()`. Pure data module — no side
-- effects on require, no rendering, no RPC. Consumers: init.lua (emits
-- RPC payloads from tree data) and state.lua (C3: walks the tree on
-- key presses).
--
-- ### Key format gotcha
--
-- `nvim_get_keymap()` returns each keymap's `lhs` AS-PARSED by nvim:
-- `<leader>` expands to the literal mapleader character (a space in
-- the user's config), and angle-bracket tokens like `<CR>`, `<Tab>`,
-- `<C-x>` are preserved as text. So `<leader>bn` becomes `" bn"` (a
-- space followed by "b" then "n"). The splitter below treats
-- angle-bracket tokens as single keystrokes and everything else as
-- per-character keystrokes.
--
-- ### Trie node shape
--
--   {
--     key      = "b",            -- this level's keystroke token
--     keys     = " b",           -- full accumulated path from root
--     desc     = "Buffer nav",   -- leaf or group label (may be nil)
--     rhs      = "<cmd>...",     -- leaf action (may be nil or empty)
--     buffer   = 0,              -- 0 = global, >0 = buffer-local
--     children = { [key] = Node, ... },
--     parent   = Node?,
--   }
--
-- Whether a node is a "group" is derived (`is_group(node)`), not
-- stored — the same node can be BOTH a leaf (with `rhs`) AND a group
-- (with children). That's how nvim's `timeoutlen` semantics express
-- themselves: press the key + wait → execute; press + continue → descend.

local M = {}

-- Split a keymap `lhs` into an ordered list of keystroke tokens.
-- `<CR>` stays intact; `bn` becomes { "b", "n" }. Unterminated `<`
-- is treated as a literal character.
---@param lhs string
---@return string[]
function M.split_keys(lhs)
  local tokens = {}
  local i = 1
  local n = #lhs
  while i <= n do
    local c = lhs:sub(i, i)
    if c == "<" then
      local close = lhs:find(">", i + 1, true)
      if close then
        table.insert(tokens, lhs:sub(i, close))
        i = close + 1
      else
        table.insert(tokens, c)
        i = i + 1
      end
    else
      table.insert(tokens, c)
      i = i + 1
    end
  end
  return tokens
end

---@return table  a new empty root node
function M.new_root()
  return { key = "", keys = "", children = {}, parent = nil }
end

-- Insert one keymap record (the shape nvim_get_keymap returns) into
-- the trie. Later inserts override earlier ones for the same `lhs`,
-- which is correct for buffer-local maps shadowing global maps since
-- callers should insert globals first, then buffer-local.
---@param root table
---@param km table  keymap record from nvim_get_keymap / buf_get_keymap
function M.insert(root, km)
  local lhs = km.lhs or ""
  if lhs == "" then
    return
  end
  local tokens = M.split_keys(lhs)
  local node = root
  for idx, tok in ipairs(tokens) do
    local child = node.children[tok]
    if not child then
      child = {
        key = tok,
        keys = node.keys .. tok,
        children = {},
        parent = node,
      }
      node.children[tok] = child
    end
    if idx == #tokens then
      child.desc = (km.desc ~= "" and km.desc) or child.desc
      child.rhs = km.rhs
      child.buffer = km.buffer or 0
      child.callback = km.callback  -- function rhs
    end
    node = child
  end
end

-- Build a fresh trie from the current nvim keymap tables.
--
-- Insert order matters for the desc-guarded upsert in `insert()`:
--   1. Presets (which-key's catalog of built-in nvim chords) go in
--      FIRST. They have empty rhs / no callback — pure metadata.
--   2. User global keymaps go in SECOND. Their rhs/callback overwrite
--      preset's empty values; their desc replaces preset's only when
--      non-empty (so users without desc still keep the preset label).
--   3. User buffer-local keymaps go in LAST, shadowing globals.
--
-- Presets come from `orchestrator.whichkey.presets` which loads the
-- catalog from which-key.nvim if available (graceful no-op if not).
---@param mode string  e.g. "n"
---@return table root
function M.rebuild(mode)
  local root = M.new_root()
  local ok, Presets = pcall(require, "orchestrator.whichkey.presets")
  if ok then
    for _, km in ipairs(Presets.load()) do
      M.insert(root, km)
    end
  end
  for _, km in ipairs(vim.api.nvim_get_keymap(mode) or {}) do
    M.insert(root, km)
  end
  for _, km in ipairs(vim.api.nvim_buf_get_keymap(0, mode) or {}) do
    M.insert(root, km)
  end
  return root
end

---@param node table
---@return boolean
function M.is_group(node)
  return next(node.children) ~= nil
end

---@param node table
---@return integer
function M.child_count(node)
  local n = 0
  for _ in pairs(node.children) do
    n = n + 1
  end
  return n
end

-- Walk a root by a token path and return the landing node, or nil.
---@param root table
---@param path string[]
---@return table?
function M.find(root, path)
  local node = root
  for _, tok in ipairs(path) do
    node = node.children[tok]
    if not node then
      return nil
    end
  end
  return node
end

-- Natural-ish sort matching which-key's default: lowercase before
-- uppercase for the same letter, digits sorted numerically when they
-- embed in the key name, specials and angle-bracket tokens at the end.
---@param a table
---@param b table
local function sort_children(a, b)
  local ak, bk = a.key, b.key
  local a_is_tok = ak:sub(1, 1) == "<"
  local b_is_tok = bk:sub(1, 1) == "<"
  if a_is_tok ~= b_is_tok then
    return not a_is_tok  -- plain chars before <...> tokens
  end
  local a_lower = ak:lower()
  local b_lower = bk:lower()
  if a_lower == b_lower then
    return ak < bk  -- lowercase sorts before uppercase at equal-letter
  end
  return a_lower < b_lower
end

-- Return an ordered list of direct child nodes under `node`.
---@param node table
---@return table[]
function M.children(node)
  local list = {}
  for _, child in pairs(node.children) do
    table.insert(list, child)
  end
  table.sort(list, sort_children)
  return list
end

-- Build the items[] payload that init.lua emits over RPC, given a
-- parent node. Each child becomes one entry with key/desc/is_group.
-- Icons are looked up via the icons module (separate concern).
---@param parent table
---@param icons_lookup fun(node: table): { glyph: string, color: string }
---@return table items
function M.items_for(parent, icons_lookup)
  local items = {}
  for _, child in ipairs(M.children(parent)) do
    local is_group = M.is_group(child)
    local desc = child.desc
    if not desc or desc == "" then
      if is_group then
        local count = M.child_count(child)
        desc = count .. " keymap" .. (count == 1 and "" or "s")
      elseif type(child.rhs) == "string" and child.rhs ~= "" then
        desc = child.rhs
      else
        desc = ""
      end
    end
    local icon = icons_lookup and icons_lookup(child) or { glyph = "", color = "" }
    table.insert(items, {
      key = child.key,
      desc = desc,
      is_group = is_group,
      icon = icon.glyph or "",
      icon_color = icon.color or "",
    })
  end
  return items
end

return M
