-- Native which-key overlay for Symmetria IDE.
--
-- This is the Lua emitter for the native which-key replacement. It
-- fires `vim.rpcnotify(0, "whichkey", payload)` notifications that the
-- Python backend (nvim_backend.py `_h_whichkey`) routes into
-- `WhichKeyModel` + `WhichKeyState` (app.py), rendered by
-- `qml/WhichKeyOverlay.qml`. End-to-end pipe, no dependency on
-- which-key.nvim's rendering code.
--
-- Commit C1 (scaffolding): emits a single mocked `show` payload on
-- VimEnter so the Lua → RPC → Python model → QML render path can be
-- visually verified. The mock items mirror the user's real <leader>
-- menu so the overlay ships looking approximately like the reference
-- screenshot from day one.
--
-- Subsequent commits replace the mock:
--   C2 — tree.lua builds the trie from vim.api.nvim_get_keymap().
--   C3 — state.lua + triggers.lua drive emissions from prefix presses.
--   C4 — neutralize which-key.nvim so the two don't duel.
--
-- Kill switch: `vim.g.symmetria_whichkey_native = 1` gates the whole
-- module. Clear it (or don't set it) to fall back to stock which-key.

local M = {}

-- Emit a fully-formed whichkey "show" payload to the Python side.
---@param items table   list of { key, desc, is_group, icon, icon_color }
---@param trail string  breadcrumb text (e.g. "<leader>"); reserved for C2+
---@param can_go_back boolean  true if <BS> should navigate up
function M.emit_show(items, trail, can_go_back)
  pcall(vim.rpcnotify, 0, "whichkey", {
    op = "show",
    mode = "n",
    trail = trail or "",
    can_go_back = can_go_back and true or false,
    items = items or {},
  })
end

function M.emit_hide()
  pcall(vim.rpcnotify, 0, "whichkey", { op = "hide" })
end

-- Scaffolding mock: matches the user's real <leader> menu from the
-- reference screenshot. Removed in C2 once the real tree-driven payload
-- lands.
local function mock_items()
  return {
    { key = "g", desc = "Open lazy git",                    is_group = false, icon = "",  icon_color = "#e8ab6f" },
    { key = "L", desc = "Trigger linting for current file", is_group = false, icon = "",  icon_color = "#7fb3d5" },
    { key = "+", desc = "Increment number",                 is_group = false, icon = "",   icon_color = ""        },
    { key = "-", desc = "Decrement number",                 is_group = false, icon = "",   icon_color = ""        },
    { key = "a", desc = "12 keymaps",                       is_group = true,  icon = "",   icon_color = ""        },
    { key = "b", desc = "Buffer navigation",                is_group = true,  icon = "",  icon_color = "#b4b4b4" },
    { key = "c", desc = "Copy path",                        is_group = true,  icon = "",  icon_color = "#b4b4b4" },
    { key = "e", desc = "File explorers",                   is_group = true,  icon = "",  icon_color = "#7fb3d5" },
    { key = "f", desc = "11 keymaps",                       is_group = true,  icon = "",   icon_color = ""        },
    { key = "G", desc = "GitHub",                           is_group = true,  icon = "",  icon_color = "#e8ab6f" },
    { key = "m", desc = "4 keymaps",                        is_group = true,  icon = "",   icon_color = ""        },
    { key = "n", desc = "1 keymap",                         is_group = true,  icon = "",   icon_color = ""        },
    { key = "o", desc = "Obsidian macros",                  is_group = true,  icon = "",   icon_color = ""        },
    { key = "p", desc = "Plugins keymaps",                  is_group = true,  icon = "",   icon_color = ""        },
    { key = "s", desc = "5 keymaps",                        is_group = true,  icon = "",   icon_color = ""        },
    { key = "S", desc = "Session managing",                 is_group = true,  icon = "",  icon_color = "#b4b4b4" },
    { key = "t", desc = "5 keymaps",                        is_group = true,  icon = "",   icon_color = ""        },
    { key = "x", desc = "6 keymaps",                        is_group = true,  icon = "",   icon_color = ""        },
  }
end

function M.setup()
  if vim.g.symmetria_whichkey_native ~= 1 then
    return
  end

  local grp = vim.api.nvim_create_augroup("SymWhichKeyScaffold", { clear = true })

  -- Deferred so VimEnter fully completes before the mock fires; avoids
  -- racing the UI-attached flag and other VimEnter autocmds.
  vim.api.nvim_create_autocmd("VimEnter", {
    group = grp,
    callback = function()
      vim.defer_fn(function()
        M.emit_show(mock_items(), "<leader>", false)
      end, 250)
    end,
  })

  -- <Esc> while the mock is visible hides it — gives the user an
  -- escape hatch during scaffolding-only operation. State machine in
  -- C3 will supersede this.
  vim.keymap.set("n", "<Esc>", function()
    M.emit_hide()
    -- fall through to whatever <Esc> would normally do (clear search
    -- highlight etc.); feedkeys with 'n' to avoid remapping.
    vim.api.nvim_feedkeys(vim.api.nvim_replace_termcodes("<Esc>", true, false, true), "n", false)
  end, { desc = "Symmetria whichkey (scaffold) hide overlay + normal <Esc>" })
end

return M
