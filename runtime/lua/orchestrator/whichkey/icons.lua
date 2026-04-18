-- Handcrafted icon map for the native which-key overlay.
--
-- Looks up a node's full `keys` path first (exact prefix match), then
-- its leading token, then its top-level prefix. This lets us attach
-- a common git glyph to everything under `<leader>g*` while still
-- overriding individual entries (like `<leader>G` for GitHub).
--
-- Static for v1 per the user's request ("handcraft for now"). v2 will
-- probe `require("mini.icons")` / nvim-web-devicons and fall back to
-- this map. Keep entries aligned with the Symmetria palette used
-- throughout the overlay (amber for accents, soft blue for browse/nav,
-- neutral gray for utility).

local M = {}

-- Leader-prefixed mappings. `<leader>` resolves to a space character
-- in the user's config (mapleader=" "), so prefix matches use " ".
---@type table<string, { glyph: string, color: string }>
M.by_path = {
  [" g"] = { glyph = "", color = "#e8ab6f" }, -- git / lazygit
  [" G"] = { glyph = "", color = "#e8ab6f" }, -- GitHub
  [" b"] = { glyph = "", color = "#b4b4b4" }, -- buffers
  [" e"] = { glyph = "", color = "#7fb3d5" }, -- file explorer
  [" c"] = { glyph = "", color = "#b4b4b4" }, -- copy
  [" S"] = { glyph = "", color = "#b4b4b4" }, -- sessions
  [" L"] = { glyph = "", color = "#7fb3d5" }, -- lint
  [" f"] = { glyph = "", color = "#c8a37a" }, -- find / telescope
  [" p"] = { glyph = "", color = "#c8a37a" }, -- plugins
  [" s"] = { glyph = "", color = "#7fb3d5" }, -- search
  [" t"] = { glyph = "", color = "#c8a37a" }, -- toggle
  [" o"] = { glyph = "", color = "#7a7a7a" }, -- obsidian / notes
  [" x"] = { glyph = "", color = "#d16969" }, -- trouble / diag
  [" m"] = { glyph = "", color = "#b4b4b4" }, -- misc
  [" n"] = { glyph = "", color = "#b4b4b4" }, -- misc
  [" a"] = { glyph = "", color = "#b4b4b4" }, -- misc
}

local EMPTY = { glyph = "", color = "" }

-- Resolve the icon for a trie node. Tries exact path first (so
-- ` ga` can override ` g`), then successively shorter prefixes.
---@param node table  node with .keys (accumulated path)
---@return { glyph: string, color: string }
function M.for_node(node)
  local keys = node.keys or ""
  if keys == "" then
    return EMPTY
  end
  -- Exact-match wins.
  if M.by_path[keys] then
    return M.by_path[keys]
  end
  -- Walk back shortening the path until we hit a registered entry.
  -- Covers the common case where a leaf (` bn`) inherits the icon
  -- of its group (` b`).
  local probe = keys
  while #probe > 0 do
    if M.by_path[probe] then
      return M.by_path[probe]
    end
    probe = probe:sub(1, -2)
  end
  return EMPTY
end

return M
