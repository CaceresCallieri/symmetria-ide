---
name: Phase 2 agent pane — current state
description: 2026-04-25 snapshot of Phase 2 work in symmetria-ide; full-window mode + composer + orchestrator `<leader>a[Nn]` hijack + multi-turn stdin JSONL all landed and verified
type: project
originSessionId: a094e8d9-4dcb-4507-bc47-56d8a4453394
---
## Landed commits (verified working)

`git log --oneline main...` through `3555ca0`:

- `2b5514b` Theme.color.agent rung (user amber, assistant wine_theme.term_cyan)
- `5253350` SessionModel + 27 tests — flat rows, partial-text coalescing
- `2a2350b` SessionHost + 9 parser tests — subprocess + daemon workers, GC suspension, stop_event
- `a8d1c87` AgentPane.qml — flat ListView, Theme tokens only
- `132b603` AppController wiring + Main.qml split (side-panel, superseded)
- `3aa2103` docs pivot — retire pty+pyte, document stream-json
- `95fc48b` **full-window mode + interactive composer** — visibility swap instead of split, TextField composer at bottom, Enter submits, Escape hides
- `8a3f5be` / `be32ec8` orchestrator hijack at `<leader>aN` / `<leader>an` (initial attempt, lost the LazyLoad race)
- `faeb363` `feat(agent): win <leader>a[Nn] hijack via User LazyLoad autocmd` — race-winning install point, verified working
- `8b96302` `fix(agent): buffer-local nowait on <leader>a[Nn] per gotcha #20` — code-review pass; adds `buffer = 0, nowait = true`, narrows LazyLoad to orchestrator, downgrades debug logging
- `16bba67` **`feat(agent): multi-turn conversation via stream-json stdin`** — argv adds `--input-format stream-json`, stdin stays open across turns; `submit_prompt` cold/hot branches; synthetic user-event injection for instant local rendering; assistant streaming row finalises in place (no duplicate Claude row)
- `3555ca0` `fix(agent): clear stop_event on restart so "New Claude" works after first session` — code-review pass; adds `_stop_event.clear()` in `start()`, normalises spawn-failure error path, drops dead `proc.stdin is None` guard, locks in `dataChanged` role-list contract via test

**How to use today:** run `PYTHONPATH=src python -m symmetria_ide`. `<leader>aN` or `<leader>an` opens the agent pane (lowercase only — orchestrator never bound uppercase). `SYMMETRIA_IDE_AGENT_VIEW=1` opens agent view on startup. `SYMMETRIA_IDE_AGENT_PROMPT="..."` opens + pre-runs a prompt. **Multi-turn is on**: in the composer, Enter sends a turn, then Enter again sends a follow-up that retains session context — the same `claude` subprocess handles both.

## Resolved: orchestrator `<leader>a[Nn]` hijack (2026-04-24)

Root cause: orchestrator.nvim is lazy-loaded via `lazy.nvim`'s `keys = {"<leader>a*", ...}` spec — its keymap registration fires *on first keypress* of `<leader>a`, **after** any `VimEnter + vim.schedule` install and **between** `<leader>a` and `N` (so `BufEnter` self-heal doesn't fire either).

Fix: `User LazyLoad` autocmd narrowed to orchestrator-matching plugin names + `vim.fn.maparg` ownership check + buffer-local `nowait` (gotcha #20). See `lazy_keymap_hijack_pattern.md` for the generalised technical note.

## Resolved: multi-turn stdin JSONL (2026-04-25)

`SessionHost` now spawns `claude` with `--input-format stream-json` and keeps stdin open. The user-envelope shape (empirically verified) is `{"type":"user","message":{"role":"user","content":"<text>"}}` — the inner `"role":"user"` is load-bearing; without it `claude` silently discards the turn. `AppController.submit_prompt` is the single funnel for "send a user turn" — branches cold (spawn + first envelope via `send_user_message`) or hot (append envelope to running stdin), and **always** injects a synthetic `{"type":"user", ...}` event into `SessionModel` first because `claude` does NOT echo user envelopes back. The env-var startup path (`SYMMETRIA_IDE_AGENT_PROMPT`) routes through the same funnel. `SessionModel.apply` finalises the streaming assistant row in place when the canonical `assistant` event arrives (mutates text + `partial=False` + scoped `dataChanged`) — previously it appended a fresh canonical row, which read as a duplicated "Claude" entry.

**Critical restart invariant** (gotcha-class, see `session_host.py:start`): `_stop_event` is **cleared** at the top of `start()`. Without that clear, a `<leader>aN` "New Claude" restart after a prior `stop()` would leave the event set and every worker would exit on its first iteration — silently non-functional. Mirror this discipline in any future `daemon=True + threading.Event` adoption.

## Open next steps (not blockers — follow-up features)

- **Permission UI.** `claude` emits permission-request events on tool use; render inline approve/deny cards. Without this, tool-use prompts stall silently. Highest-leverage follow-up since multi-turn unlocks tool-using conversations.
- **Turn grouping + tool-call drill-in.** Flat list was intentional for the placeholder; real event cadence (now visible across multi-turn sessions) informs what the grouped view should be.
- **Focus switching.** No keyboard binding yet to hop between editor and agent pane (beyond `<leader>aN` / Escape). Worth adding.
- **Continue / Resume routing (`<leader>aC` / `<leader>aR`).** Deferred until `SessionHost` supports `claude -c` / `claude -r` flags — orchestrator's terminal flow handles those slots today.
- **Agent-side stop control.** Currently the only way to interrupt a slow turn is to `<leader>aN` (which destroys session state). A composer-side "interrupt" affordance — likely Esc-while-streaming or a dedicated bind — would let the user reclaim the prompt without losing context.

## Pointers for resumption

- `runtime/init.lua` bottom — the hijack/autocmd block starts at "Agent-pane triggers"
- `src/symmetria_ide/app.py` — `AppController._on_agent_event` for the Python routing
- `qml/AgentPane.qml` — the pane itself (composer Enter → `controller.submit_prompt`, Esc → `controller.hide_agent`)
- `docs/phases.md` — Phase 2 section reflects the stream-json pivot
- See also: memory `project_governance` for the standards layer and `ui_surface_discipline` for the placeholder-first directive
