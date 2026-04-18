-- Shared constants for the orchestrator.whichkey subsystem.
-- Centralising these prevents silent drift between modules that match
-- on the same string (e.g. state.lua and triggers.lua both check the
-- trigger description to identify our own keymaps vs. third-party ones).

---@class WhichKeyConstants
local M = {}

-- Description tag placed on every trigger keymap installed by
-- triggers.lua. Any code that needs to distinguish "is this keymap
-- ours?" should match against this string via `desc:find(TRIGGER_DESC,
-- 1, true)` (plain-text search, not pattern, to avoid false positives
-- on special chars).
M.TRIGGER_DESC = "symmetria-whichkey-trigger"

return M
