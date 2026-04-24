---
name: hijacking a keymap owned by a lazy.nvim plugin
description: technical note — VimEnter + BufEnter alone lose the race against lazy.nvim `keys = {...}` lazy-loading; need User LazyLoad autocmd to win the slot
type: reference
originSessionId: a094e8d9-4dcb-4507-bc47-56d8a4453394
---
When a user's plugin is lazy-loaded via `lazy.nvim`'s `keys = {"<leader>a*", ...}` spec, the plugin's keymap registration happens **on the user's first matching keypress**, not at startup. Concretely, pressing `<leader>a` triggers:

1. lazy.nvim sees the keypress match, loads the plugin
2. Plugin's setup registers its keymaps (replacing any earlier installs at those slots)
3. lazy.nvim `feedkeys`-replays the keypress
4. nvim re-dispatches; trailing `N` resolves to the plugin's freshly-installed keymap

An override installed on `VimEnter + vim.schedule` is there before step 1 and gets clobbered in step 2. `BufEnter` doesn't fire between step 2 and step 4 (no buffer transition). So both common self-heal points lose.

**The winning pattern** (symmetria-ide's agent-pane hijack uses this):

```lua
vim.api.nvim_create_autocmd("User", {
  pattern = "LazyLoad",
  callback = function(ev)
    -- ev.data is the plugin name; check if it's the one whose
    -- keymaps we want to shadow, or just re-install unconditionally.
    -- install_fn uses `vim.fn.maparg` to check ownership first —
    -- skips the no-op case and only replaces when an outsider
    -- took the slot.
    install_fn("LazyLoad:" .. tostring(ev.data))
  end,
})
```

`User LazyLoad` fires **synchronously inside lazy.nvim's load path**, after step 2 but before step 3. Installing our keymap there wins the slot for the trailing `N`.

**Verification approach**: pair the install with a diagnostic `rpcnotify` so the app log shows every install attempt + reason string; pair the handler with a `vim.notify` so a visual toast confirms it fired at press time. Without that observability, "our handler silently lost the race" and "our handler never installed" look identical from the outside.

**Further fallbacks if `User LazyLoad` also loses**: `ModeChanged *:n` (re-install on every normal-mode entry — cheap, covers ~everything), `vim.on_key` observation (more invasive), or coordinating with the plugin via a shared global flag so the plugin itself defers to us.

Related: CLAUDE.md gotcha #17 (menu keymap self-heal via `maparg`) and gotcha #21 (LspAttach keymap-trie race) — same class of problem, same `vim.fn.maparg` reconciliation mechanism.
