-- Presets bridge: populate the trie with NeoVim's built-in motion /
-- operator / window / z / g / nav chains, using which-key.nvim's
-- hand-curated description catalog as a data source.
--
-- ### Why this exists
--
-- `nvim_get_keymap()` only returns USER-DEFINED keymaps. Built-in
-- motions like `gg`, `gu`, `gf`, `z<CR>`, `zz`, `[s`, `]s`, `<C-w>h`,
-- etc. are hardcoded in nvim core — they're not keymaps at all — so
-- our trie would show an empty submenu under `g`, `z`, `[`, `]`, and
-- `<C-w>` with only the user's own chord extensions visible.
-- Which-key fills this gap by shipping a preset catalog; we load the
-- same catalog to achieve parity without reimplementing ~200 lines of
-- curated descriptions.
--
-- ### What we filter
--
-- We only import MULTI-TOKEN entries (e.g. `gg`, `g~`, `[s`, `<C-w>h`)
-- because single-char motions like `h/j/k/l/w/b/e` aren't overlay
-- content — they're direct motions the user already knows. Pulling
-- them in would pollute the trie root with a large flat list of
-- non-menu keys.
--
-- ### Layering (trie insert order matters)
--
-- `init.lua` calls us BEFORE inserting real user keymaps. That way
-- `Tree.insert`'s desc-guarded upsert (only overrides desc if new is
-- non-empty) preserves user desc while letting user rhs/callback win.
-- A user's `gf` for a custom action overrides our preset `gf` here.
--
-- ### Graceful degradation
--
-- If which-key.nvim isn't installed, we quietly return zero entries
-- — the overlay just won't have preset descriptions. No crash, no
-- warning noise.

local Tree = require("orchestrator.whichkey.tree")

local M = {}

-- Flatten one preset table into a list of keymap-shaped records.
---@param preset table  which-key preset (contains numbered entries)
---@return table[]      list of { lhs, desc, rhs, buffer, callback }
local function flatten(preset)
  local out = {}
  if type(preset) ~= "table" then
    return out
  end
  for _, entry in ipairs(preset) do
    -- v3 spec: { "lhs", desc = "...", group = "...", ... }
    if type(entry) == "table" and type(entry[1]) == "string" then
      local lhs = entry[1]
      local desc = entry.desc or entry.group
      if desc and desc ~= "" then
        -- Only keep multi-token entries — single-char motions like
        -- `h/j/k/l/w/b/e` would pollute the trie root.
        local tokens = Tree.split_keys(lhs)
        if #tokens > 1 then
          table.insert(out, {
            lhs = lhs,
            desc = desc,
            rhs = "",
            buffer = 0,
          })
        end
      end
    end
  end
  return out
end

-- Memoize the flattened preset list. Which-key.nvim is lazy-loaded
-- (event = "VeryLazy") so a `require` can fail at early startup and
-- succeed later. We also observed it being unloadable again after
-- certain buffer transitions (e.g. `:edit` after the plugin's config
-- ran) — the `package.loaded` entry becomes unreliable. Caching the
-- first SUCCESSFUL load lets us keep the preset catalog even when
-- the module is momentarily unreachable.
---@type table[]?
M._cache = nil

-- Try to load the preset catalog from which-key.nvim and flatten it
-- into our keymap-shaped records. Returns the cached list if a
-- previous call already succeeded. No-op returns {} if which-key isn't
-- available.
---@return table[]  list of { lhs, desc, rhs, buffer } ready for Tree.insert
function M.load()
  if M._cache then
    return M._cache
  end
  local ok, presets = pcall(require, "which-key.plugins.presets")
  if not ok or type(presets) ~= "table" then
    return {}
  end

  -- These six preset tables all cover normal-mode-accessible chords.
  -- `text_objects` (mode = {"o","x"}) is intentionally omitted — our
  -- overlay is normal-mode-only in v1.
  local names = { "operators", "motions", "windows", "z", "nav", "g" }
  local out = {}
  for _, name in ipairs(names) do
    for _, km in ipairs(flatten(presets[name])) do
      table.insert(out, km)
    end
  end
  -- Only cache non-empty results; if which-key is still loading, let
  -- a later rebuild try again.
  if #out > 0 then
    M._cache = out
  end
  return out
end

return M
