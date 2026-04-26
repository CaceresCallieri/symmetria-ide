# PRD — Orchestrator replacement

A multi-session plan for absorbing `orchestrator.nvim`'s essential workflow into Symmetria IDE and retiring the plugin.

This document is the planning anchor. It is intentionally long and self-contained so any future session — including ones running on a freshly-compacted context — can pick up at the next phase without re-deriving prior decisions. Every phase below names files, line numbers, and acceptance criteria. Open questions are flagged explicitly so they don't silently become assumptions.

**Read order for cold pickup:**
1. This PRD (you are here).
2. `docs/agent-dashboard-integration.md` — the cross-project commitment this PRD operationalizes.
3. `CLAUDE.md` § "The agent backend (Node SDK sidecar)" — current single-instance implementation surface.
4. The phase you are about to work on (skip the others on first pass).

---

## 1. Vision

Symmetria IDE replaces `orchestrator.nvim` as the user's primary multi-Claude orchestration surface. The IDE keeps **the same keybinds, the same workflow, and the same dashboard integration** — but moves from terminal-output scraping to a structured SDK-driven backend. The plugin is retired only when the IDE meets the gates in `docs/agent-dashboard-integration.md`; until then, both can run side by side.

**Non-goals of this PRD:**
- Renaming the IDE to "Orchestrator IDE" (the IDE *contains* the orchestrator role; it is not only that role — see `docs/identity.md`).
- Replacing the Symmetria Shell agent dashboard with an in-IDE alternative. The dashboard stays; the IDE feeds it.
- Designing a final visual treatment for parallel agent panes / instance switcher / status bar. Per `.claude/memory/feedback/ui_surface_discipline.md`: ship the structural surface first, iterate the visual treatment once real cadence is observable.
- Cross-project agent management (one IDE process tracking agents across multiple cwds). The IDE remains single-project per process; multi-project support is a separate, larger initiative.

---

## 2. Phase sequencing

Phases are ordered by dependency, not by user-facing visibility. The earlier phases are mostly invisible refactors; the user-facing payoff lands in Phase B.

| # | Phase | User-facing? | Estimated size | Blocks |
|---|-------|--------------|----------------|--------|
| A | Per-instance refactor (foundation) | No — invisible plumbing | Medium | All later phases |
| B | Multi-instance spawn + focus keybinds | Yes — `<C-1>..<C-5>` work | Medium | C, D, E, F |
| C | Spawn semantics (`fresh` / `continue` / `resume` + `--dangerously-skip-permissions`) | Yes — `<leader>aN/aR/aC/an/ar/ac` differentiate | Small | E, F |
| D | SDK→bridge emitter (dashboard integration) | Yes — IDE sessions appear in the bar | Medium | F |
| E | Spawn menu UI (single-key shortcuts) | Yes — `<leader>aa` opens menu | Small | F |
| F | `orchestrator.nvim` deprecation | Yes — plugin removed from `~/.dotfiles` | Trivial | G |
| G | Edit tracker | Yes — `<leader>ae / aj` or successor binding | Large | — |

**Indefinitely deferred** (no phase number, no date):
- Floating multi-tab markdown prompt editor (`<leader>ap`, `<C-S-Space>`).
- URL handler (extract-and-open URLs from Claude output).
- Status bar redesign (vertical agents vs bubbles).
- Two-way control surface (dashboard → IDE: focus, switch, set mode).
- Multi-project agent management (one IDE process tracking agents across cwds).

The deferred items are documented at the end; they are NOT cancelled — they are parked until the user signals demand.

---

## 3. Cross-cutting concerns

These rules apply to every phase. Future-you: read this section even if you skip phase content; skipping it has burned this codebase before.

### 3.1 Threading and signals

- Cross-thread signal connections use **explicit `Qt.QueuedConnection`** at every connect site, with a one-line `# queued: <reason>` comment. (`.claude/project-standards.md` §4 P2.)
- Every long-running thread is `daemon=True` AND owns a `threading.Event` for cooperative shutdown. (`.claude/project-standards.md` §1 P0.)
- GC is suspended around worker-thread signal emission whenever payload construction allocates. See `CLAUDE.md` gotcha #10 — this codebase has SEGV'd on this exact race in the past.

### 3.2 Design tokens

- Every pixel of new QML chrome binds to `Theme.*` tokens from `qml/design/Theme.qml`.
- New tokens land in `Theme.qml` first, with a provenance comment citing the wine theme source, then are referenced from delegates. No inline color/spacing/radius/typography literals.
- The 8-color agent palette must match `~/projects/orchestrator.nvim/lua/orchestrator/highlights.lua:16-27` exactly (same colors, same order, modulo-8 wrapping). Identical color indices in the dashboard and the IDE preserves cross-feed consistency.

### 3.3 Testing

- Hermetic offscreen tests via `QT_QPA_PLATFORM=offscreen`. The session-scoped `qt_app` fixture in `tests/conftest.py` is automatic.
- Pool-level tests use `_FakeSessionHost` (already in `tests/test_app_controller_awaiting.py` and `tests/test_app_controller_permission_mode.py`). Extend the fake to take an `index` parameter; do NOT spawn real subprocesses in tests.
- `pytest-qt` idioms (`qtbot.waitSignal`, `qtbot.waitUntil`) — never `time.sleep` (`.claude/project-standards.md` §1 P0).
- New regression tests guard the failure modes documented under each phase's "Risks" — every Risk that doesn't have a corresponding test is a future bug waiting to happen.

### 3.4 Authoritative state lives in the sidecar

`CLAUDE.md` gotcha #25 is the load-bearing context for any permission-mode work in this PRD. The sidecar's local `currentMode` is the source of truth; the SDK's `Query.setPermissionMode` is best-effort. **For multi-instance:** each sidecar has its own `currentMode`; they don't interfere; the Python controller mirrors per-instance echoes into per-instance state.

### 3.5 Pre-warm contract

The current `AppController.start()` calls `_session_host.start("")` to pre-warm the sidecar so `Shift+Tab` and the mode pill are live before the first message. **For multi-instance:** the pool pre-warms ONE sidecar at app start (the first instance); additional instances pre-warm on spawn. Do not pre-warm all 5 slots eagerly — `claude` subprocess startup is non-trivial and most users won't open all five.

### 3.6 No silent feature-flag or backwards-compat debt

The user has been explicit (per `~/.claude/CLAUDE.md`): no half-finished implementations, no shims, no "just in case" code. Mark hacks with `HACK` / `WORKAROUND` per the global guideline; surface them in the response summary so the user can decide whether to accept them.

---

## 4. Phase A — Per-instance refactor (foundation)

**Goal:** Restructure `AppController`, `SessionHost`, and `SessionModel` so they support N instances without changing user-facing behavior. No new keybinds, no new spawn semantics — the IDE still spawns exactly one sidecar at app start, but the plumbing is index-aware and ready.

This phase is invisible to the user. It exists to make Phase B small and reviewable.

### 4.1 Surface changes

**`src/symmetria_ide/app.py`:**

- Replace `self._session_host: SessionHost` with `self._session_hosts: dict[int, SessionHost]`.
- Replace `self._session_model: SessionModel` with `self._session_models: dict[int, SessionModel]`.
- Replace scalar state (`_awaiting_response: bool`, `_permission_mode: str`) with per-instance dicts (`_awaiting_response: dict[int, bool]`, `_permission_mode: dict[int, str]`).
- Add `_focused_instance: int = 1` — the instance the QML pane currently mirrors. Default 1 throughout Phase A (only one instance exists).
- Convert `awaitingResponse` and `permissionMode` `@Property` accessors to read from the focused instance's dict slot. The QML side keeps reading `controller.awaitingResponse` / `controller.permissionMode` unchanged.
- Add `@Slot(int) focus_instance(index: int)` — sets `_focused_instance`, emits `awaitingResponseChanged` and `permissionModeChanged` so the QML pane re-binds. No-ops if `index` is not in `_session_hosts`.
- Add `@Slot(int) cycle_permission_mode_for(index: int)` — same body as today's `cycle_permission_mode`, but routes to `_session_hosts[index]`. Keep `cycle_permission_mode()` as a wrapper that calls `cycle_permission_mode_for(_focused_instance)` so the QML pane's Shift+Tab still works.
- Add `@Slot(str, int) submit_prompt_for(prompt: str, index: int)` — same body as today's `submit_prompt`, but routes to `_session_hosts[index]`. Keep `submit_prompt(str)` as a wrapper that calls `submit_prompt_for(prompt, _focused_instance)`.
- Add `@Slot(str, str, int) respond_to_permission_for(request_id: str, decision: str, index: int)`. Keep the 2-arg form as a wrapper. (Note: permission `request_id` already disambiguates which sidecar issued the request — the pool can route by lookup table, not by index. But keeping the indexed form avoids lookup overhead in the QML round-trip.)

**`src/symmetria_ide/session_models.py`:**

- No public surface change. `SessionModel` is already 1:1 with one sidecar's event stream. The pool just instantiates N copies.
- Add an `instance_index: int` constructor parameter so the model can self-identify in logs. Default to 0 (back-compat for tests).

**`src/symmetria_ide/session_host.py`:**

- No public surface change. `SessionHost` is already 1:1 with one subprocess.
- Add an `instance_index: int` constructor parameter for log prefixing.

**`qml/AgentPane.qml`:**

- No structural change. The pane continues to bind `sessionModel` and `controller` as today.
- Add a small instance indicator in the chrome (bottom-left, beside the permission pill?) showing `<focused_instance> / <total_instances>` — even when total is 1, this surfaces the new state to the user as proof of plumbing. Bind to `controller.focusedInstance` and `controller.instanceCount` (both new `@Property`).

**`qml/Main.qml`:**

- No change in Phase A. Single `AgentPane` remains.

**`runtime/init.lua`:**

- No change in Phase A.

### 4.2 Acceptance criteria

- IDE launches and behaves identically to today: one agent pane, one sidecar pre-warmed, Shift+Tab cycles mode, `<leader>aN` opens fresh session.
- `controller.focusedInstance` reads `1`. `controller.instanceCount` reads `1`. The new instance indicator shows `1 / 1`.
- Test suite passes: existing 371 tests + new tests for the per-instance dispatch (focus_instance, cycle_permission_mode_for, submit_prompt_for).
- `git grep "self._session_host\b"` returns ZERO matches in `src/`. (Catches accidental survival of the singular field.)
- `pyright` baseline unchanged. `qmllint` baseline unchanged.

### 4.3 Risks

- **Permission `request_id` routing collision** — if two sidecars emit overlapping request IDs, the pool routes the wrong response to the wrong sidecar. The SDK uses UUIDs, so collision is statistically impossible, but the pool MUST validate that the `request_id` was actually issued by the target sidecar before calling `send_permission_response`. Add a `_pending_permissions: dict[str, int]` map (request_id → instance_index) populated when `permission_request` events land; pop on response or session close.
- **Pre-warm semantics drift** — today, `_on_agent_event` action="new" stops + restarts the single sidecar. With N instances, action="new" must NOT touch other instances' sidecars. Be precise about which instance's host gets reset.
- **QML re-bind cost on focus switch** — switching focus means flipping `awaitingResponse` and `permissionMode` for the bound model. Single-instance behavior is unaffected because `_focused_instance` doesn't change in Phase A; this risk surfaces in Phase B.

### 4.4 Open questions

- **(A1)** Should `_session_hosts` be a `dict[int, SessionHost]` or a `list[SessionHost | None]` of fixed length 5? List is simpler indexing; dict is sparse. Recommend dict — instances can have arbitrary numbers in case the user later wants `<leader>a6+`.
- **(A2)** Should the new instance indicator render even when `instanceCount == 1`, or only when ≥ 2? Recommend always — surfaces the plumbing immediately, normalizes the user's mental model.

---

## 5. Phase B — Multi-instance spawn + focus keybinds

**Goal:** Wire `<leader>aN` to spawn an additional instance (next free slot 1..5), and `<C-1>..<C-5>` to focus by index. The IDE now manifestly supports multiple parallel sidecars.

### 5.1 Surface changes

**`runtime/init.lua` (lines 555–650 region):**

- Extend `AGENT_KEYMAP_SPECS` with the `<C-1>..<C-5>` focus keybinds. Each emits `vim.rpcnotify(0, "agent", { op = "focus", index = N })`.
- Add `<C-S-q>` → `vim.rpcnotify(0, "agent", { op = "close", index = focused })` (close the focused instance). The `index` field is omitted; AppController interprets a missing index as "the focused instance".
- Keep the buffer-local + nowait + `slot_owned_by_us` self-heal pattern from gotcha #20. Add the new keybinds to the `wanted` list inside `install_agent_keymaps`.

**`src/symmetria_ide/app.py::_on_agent_event`:**

Extend the dispatch table:

| `op` | `action` | `index` | Effect |
|------|----------|---------|--------|
| `show` | `new` | omitted or 0 | **Phase B change**: spawn into next free slot (1..5); focus the new slot; show pane |
| `show` | `new` | 1..5 | Spawn into specific slot if free; if occupied, route to existing pool entry's `clear()` + re-warm; focus |
| `focus` | — | 1..5 | Set `_focused_instance = index` if `index in _session_hosts`; no-op + log otherwise |
| `close` | — | omitted | Close `_focused_instance`'s host (call `stop()`, drop from pool, drop model). If pool becomes empty, hide the pane and reset `_focused_instance = 1`. If pool not empty, focus the next-lowest occupied slot. |
| `close` | — | 1..5 | Same as above but for the explicit index |
| `hide` / `toggle` | — | — | Unchanged from Phase A (operates on the pane's visibility, not on instances) |

**`src/symmetria_ide/app.py::AppController` new helpers:**

- `_next_free_slot() -> int | None` — returns the lowest slot in `1..5` not present in `_session_hosts`, or None if full.
- `_spawn_instance(slot: int, prompt: str = "") -> None` — instantiates `SessionHost(instance_index=slot)`, instantiates `SessionModel(instance_index=slot)`, wires their signals (using the same `Qt.QueuedConnection` pattern as today's single-host wire), inserts into the pool dicts, calls `host.start(prompt)`. Initializes `_awaiting_response[slot] = False` and `_permission_mode[slot] = "default"`.
- `_close_instance(slot: int) -> None` — calls `host.stop()`, joins workers (already in `stop()`), removes from all pool dicts, drops `_pending_permissions` entries matching that slot.

**`qml/AgentPane.qml`:**

- The bound `sessionModel` context property is currently global. In Phase B, `AppController` exposes `@Property() session_model_for_focused: SessionModel` (computed from `_session_models[_focused_instance]`). The pane binds to that.
- The QML side does NOT need to know about the pool. It always shows "the focused instance's transcript". Switching focus re-binds the model, which Qt handles by emitting `model.layoutChanged()` — **verify** this works cleanly without leaking event log between instances; if it doesn't, use `Connections { target: controller; function onFocusedInstanceChanged() { eventList.model = controller.sessionModelForFocused; } }`.

**`qml/design/Theme.qml`:**

- Add `Theme.color.agent.palette: list<color>` — the 8 colors from `orchestrator.nvim/lua/orchestrator/highlights.lua:16-27`. Provenance comment cites that file.
- Add `Theme.color.agent.colorForIndex(idx: int): color` (a `function` in the singleton) returning `palette[(idx - 1) % palette.length]`. Used by future chip rendering and by the in-pane instance indicator.

### 5.2 User-facing behavior

- `<leader>aN` (no instance running) → spawns instance 1, focuses it, shows pane.
- `<leader>aN` (instance 1 running) → spawns instance 2, focuses it.
- `<leader>aN` (instances 1–5 all running) → log warning ("agent slots full"), focus slot 5.
- `<C-1>` → focus instance 1 (no-op if not running). Pane re-binds to instance 1's transcript.
- `<C-3>` → focus instance 3 (no-op if not running).
- `<C-S-q>` → close focused instance. If others remain, focus the next-lowest. If none remain, hide the pane.
- Shift+Tab on the pane cycles permission mode for the FOCUSED instance only.
- The composer's first message goes to the FOCUSED instance.
- Permission approval/denial card buttons route the response to the issuing instance (via `request_id` lookup, not via `_focused_instance`).

### 5.3 Acceptance criteria

- Spawning 5 instances shows 5 distinct sidecars in `ps aux | grep node`. Each has its own `claude` CLI subprocess child.
- Focus-switching between instances correctly mirrors per-instance permission mode in the pill, per-instance awaiting-response in the spinner, and per-instance event log in the ListView.
- Closing instance 2 of {1,2,3} leaves {1,3} with focus on 1 (next-lowest after 2's removal — i.e., the one BELOW the closed one if it exists, otherwise the next ABOVE). The instance indicator shows `1 / 2`.
- Killing the IDE process cleanly terminates all 5 sidecars (no orphaned `node` processes). Test: `pkill -INT -f symmetria_ide && sleep 1 && pgrep -f sidecar/dist/index.js` returns nothing.
- New tests: focus_instance dispatch, _next_free_slot bounds, close-and-refocus cascade, request_id routing across instances.

### 5.4 Risks

- **Slow spawn UX** — spawning a sidecar takes ~500ms (Node startup + SDK initialize handshake). The user pressing `<leader>aN` rapidly to open multiple instances could see UI lag. Acceptable for now; if it becomes painful, batch spawns with a loading indicator in the pane.
- **Pre-warm storms** — the user opening 5 instances at IDE start would pre-warm 5 Node processes. **Mitigation:** Phase A's pre-warm contract still pre-warms only ONE sidecar at app start; additional instances pre-warm only on actual `<leader>aN` press.
- **Focus switch during streaming** — if instance 1 is mid-streaming and the user `<C-2>`s away, then `<C-1>`s back, the partial-text coalescing index in `SessionModel` (per-instance, so isolated) must continue from where it left off. Test with synthetic bursts.
- **Composer focus drift** — the QML composer (currently bound to "the pane") needs a clear contract: typing into the composer always sends to the FOCUSED instance. If the user types, then `<C-2>` away, then comes back to `<C-1>`, the composer's current text is preserved (it's a local QML field). Decision required: is the composer's text per-instance, or shared across all panes?

### 5.5 Open questions

- **(B1)** Composer text persistence: per-instance (each instance remembers its draft) or shared (one composer for all)? Recommend per-instance — matches `orchestrator.nvim`'s prompt-editor model where each tab held its own buffer. Implement via `dict[int, str]` keyed on instance index in `AppController`.
- **(B2)** Should focusing a non-existent slot (`<C-3>` when only instance 1 exists) be a no-op-with-log, or should it spawn instance 3? Recommend no-op — matches orchestrator.nvim semantics, avoids accidental spawn.
- **(B3)** Is the in-pane instance indicator a chip strip (showing all 1..5 slots, dim for empty, color for active) or a textual "3 of 5"? Defer the visual treatment per `.claude/memory/feedback/ui_surface_discipline.md`. Phase B ships textual; Phase E (spawn menu) revisits when we have richer chrome.
- **(B4)** Per-instance color assignment: do colors track creation order (instance 1 = palette[0], etc.) or are they reassigned on slot reuse (closing instance 1 then spawning into slot 1 picks the next color)? Recommend creation-order — matches orchestrator.nvim's `next_color_idx` round-robin, preserves visual consistency across the IDE↔dashboard handoff in Phase D.

---

## 6. Phase C — Spawn semantics: `fresh` / `continue` / `resume` + skip-permissions

**Goal:** Differentiate `<leader>aN/aR/aC` (normal) from `<leader>an/ar/ac` (skip-permissions), and add `continue` / `resume` spawn types.

### 6.1 Surface changes

**`runtime/init.lua`:**

- Extend the `AGENT_KEYMAP_SPECS` to include all six keybinds:

| Key | Action | Dangerous |
|-----|--------|-----------|
| `<leader>aN` | new (fresh) | false |
| `<leader>aR` | resume | false |
| `<leader>aC` | continue | false |
| `<leader>an` | new (fresh) | true (skip permissions) |
| `<leader>ar` | resume | true |
| `<leader>ac` | continue | true |

- Each keybind emits `vim.rpcnotify(0, "agent", { op = "show", action = "<spawn-type>", dangerous = <bool> })`.
- Remove the `HACK` comment in `runtime/init.lua:557-561` once `dangerous` is plumbed through.

**`src/symmetria_ide/session_host.py`:**

- Extend `start(prompt: str, cwd: Path | None = None)` to `start(prompt: str, cwd: Path | None = None, *, spawn_type: str = "fresh", dangerous: bool = False, session_id: str | None = None)`.
- Forward these as args to the sidecar via stdin or env. Recommend env (`SYMMETRIA_AGENT_SPAWN_TYPE`, `SYMMETRIA_AGENT_DANGEROUS=1`, `SYMMETRIA_AGENT_RESUME_ID=<uuid>`) — clean separation from the wire protocol, easy to test with subprocess fakes.

**`sidecar/src/index.ts`:**

- Read the env vars at startup. Translate to SDK options:
  - `spawn_type: "fresh"` → no special options.
  - `spawn_type: "continue"` → `options.continue = true`.
  - `spawn_type: "resume"` → `options.resume = SYMMETRIA_AGENT_RESUME_ID`.
  - `dangerous: true` → `options.permissionMode = "bypassPermissions"` (the sidecar's local `currentMode` initializes here too — so the pill renders the right state from frame 1).
- Bypass mode + `canUseTool` short-circuit already work today (CLAUDE.md gotcha #24). No protocol changes.

**`src/symmetria_ide/app.py::_on_agent_event`:**

- Extend the dispatch to read `payload.get("dangerous", False)` and `payload.get("action")` (which is now `"new" | "continue" | "resume"`).
- For `resume`, the IDE needs to know which session to resume. Open question: see (C1).

### 6.2 User-facing behavior

- `<leader>aN` → spawns fresh instance, normal permission mode (`default`).
- `<leader>an` → spawns fresh instance in `bypassPermissions` mode. Pill renders red/orange/whatever-token from the start.
- `<leader>aR` / `<leader>ar` → spawns instance that resumes the most recent session for the cwd. (Open question: how does the user pick WHICH session to resume?)
- `<leader>aC` / `<leader>ac` → spawns instance that continues the most recent session.

### 6.3 Acceptance criteria

- All six keybinds visibly differentiate in their behavior.
- Skip-permissions instance shows the bypass pill from frame 1 (no flash of `default`).
- A test pre-spawns one instance with `dangerous=true`, asserts the sidecar's `currentMode` is `bypassPermissions` after `start()`. Use the existing `_FakeSessionHost` shape.

### 6.4 Risks

- **Conflating `currentMode` initialization** — the sidecar today initializes `currentMode = "default"` regardless of the SDK options. Phase C must initialize it from `options.permissionMode` so the echo on first emit reflects the spawned mode. Cross-reference CLAUDE.md gotcha #25 in the implementing commit.
- **Wrong skip-perms semantics** — orchestrator.nvim uses `--dangerously-skip-permissions` as a CLI flag. The SDK uses `permissionMode: "bypassPermissions"`. Both behave identically at the auth gate, but the SDK path is preferable: it's runtime-mutable (Shift+Tab can cycle out of bypass), the CLI flag is locked at spawn. The IDE always uses the SDK path; document this in CLAUDE.md.

### 6.5 Open questions

- **(C1)** `resume` semantics: does the IDE auto-pick the most recent session for the cwd (like `claude -r` does), or open a session picker? Recommend auto-pick for the keybind path; offer a future picker via the spawn menu (Phase E). Auto-pick reads `~/.config/claude/sessions/<project-id>/sessions.json` to find the latest. Confirm path with `claude -r --help`.
- **(C2)** Should `dangerous` be reflected in the in-pane instance indicator color so the user can see at a glance which instances are in bypass mode? Recommend yes, but as a Phase B/C polish, not a separate phase. Use `Theme.color.danger` (already in Theme.qml) for a small dot beside the instance number.

---

## 7. Phase D — SDK→bridge emitter

**Goal:** IDE-spawned sidecars register with the existing Symmetria Shell agent dashboard and emit activity events. The dashboard shows IDE sessions alongside terminal-`claude` sessions with NO change to the bridge or the dashboard.

This phase realizes the "dual-feed" architecture from `docs/agent-dashboard-integration.md`. Read that doc in full before starting this phase — it has the protocol-level reasoning the implementation depends on.

### 7.1 Surface changes

**New file: `sidecar/src/emitters/agent-bridge.ts`:**

- Connects to `process.env.SYMMETRIA_AGENT_SOCKET ?? '/run/user/' + uid + '/symmetria-agents.sock'` on sidecar startup.
- Sends `{ type: "added", nvim_pid: 0, instance: { buf: <instance_index>, cwd: <process.cwd()>, project: <basename(cwd)>, spawn_type: <"fresh"|"continue"|"resume">, color_idx: <1..8>, dangerous: <bool>, title: "Symmetria IDE", spawned_at: <epoch_ms>, active: true } }` on `system: init` from the SDK.
- For each subsequent SDK message, emits an `activity`-shaped envelope:
  - `assistant` w/ `tool_use` → `{ type: "activity", agent_id: "<IDE_PID>_<instance_index>", hook_event: "PreToolUse", state: "working", tool: <tool_name>, in_plan_mode: <currentMode === "plan">, event_ts_ns: <BigInt>, source: "sdk", tool_input: <tool_use.input> }`.
  - `user` w/ `tool_result` → `{ ..., hook_event: "PostToolUse", state: "idle", tool: <tool_name>, source: "sdk" }`.
  - `permission_request` → `{ ..., hook_event: "Notification", state: "needs_permission", tool: <tool_name>, source: "sdk" }`.
  - `result` → `{ ..., hook_event: "Stop", state: "idle", source: "sdk", cost_usd: <result.total_cost_usd> }`.
  - `permission_mode_changed` (sidecar-synthesized) → no separate envelope; instead, every subsequent emit carries `permission_mode: <currentMode>` so the dashboard sees the latest mode on every event.
- Sends `{ type: "removed", nvim_pid: 0, buf: <instance_index> }` on `process.exit` (use `process.on('beforeExit')` for the graceful path; `SIGTERM` handler for the abrupt path).
- **Failure-quiet:** if the socket isn't there (Symmetria Shell not running), log once at INFO and no-op for the rest of the sidecar's lifetime. The IDE must NOT depend on the bridge being up.

**`sidecar/src/index.ts`:**

- Instantiate the emitter near the top of `main()`. Pass it the SDK message stream alongside the existing stdout JSONL writer — sibling consumers, not a chain.

**Wire-protocol additions on the bridge side (`~/.config/quickshell/symmetria/scripts/agent-bridge.py`):**

- The current `added` / `removed` / `activity` shapes already accept the additional fields as pass-through (`AgentService.qml` reads them as JS object properties). **Verify this with one test before the IDE side ships** — write a tiny script that connects to the socket, sends an `added` message with `nvim_pid: 0, source: "sdk", cost_usd: 0.0042`, then reads the bridge's stdout and confirms the agent appears with all fields intact.
- If the bridge rejects `nvim_pid: 0` (e.g., because the resolution code at line 156-182 walks /proc upward and crashes on PID 0), patch the bridge to accept a sentinel value like `nvim_pid: -1` meaning "not an nvim-spawned agent". This is a Symmetria Shell change, NOT an IDE change — needs a parallel commit in that repo.

### 7.2 User-facing behavior

- When the IDE spawns instance 1, the agent dashboard's bar shows a new chip for it within ~100ms.
- The chip's color matches the IDE's pane chrome color for that instance (palette match per §3.2).
- When Claude does work in instance 1, the chip's `activityTool` updates ("Bash", "Edit", etc.) — same as a terminal-`claude` chip.
- When the IDE closes instance 1, the chip disappears.
- Killing the IDE causes all its chips to disappear within 1s (graceful) or 15s (`ACTIVITY_STALENESS_TIMEOUT` in the bridge — fallback for hard-kill).

### 7.3 Acceptance criteria

- IDE-spawned sidecar appears in `symmetria shell agentbar status` JSON output.
- Chip color matches `Theme.color.agent.colorForIndex(N)` for instance N.
- Tool-use events update the chip's tool annotation.
- Bridge logs (`~/.local/state/symmetria/debug.log`) show `recv | agents=N` increments when the IDE spawns.
- Closing the IDE during a session leaves the bridge log clean (no orphaned activity entries past the staleness window).

### 7.4 Risks

- **Subagent dedup mismatch** — the bridge dedupes recap subagents by tracking `SubagentStart`/`SubagentStop` pairing. The SDK doesn't emit those events. **Decision:** the SDK emitter does NOT synthesize subagent events; the dashboard sees the SDK feed at a flatter granularity than the hook feed. This is acceptable — the dashboard's chip shows `activity_state` and `activity_tool`, which the SDK feed populates correctly; the subagent counter is an internal bridge optimization.
- **`agent_id` collision across IDE restarts** — if the IDE crashes and restarts with the same PID (rare but possible after a fast SIGKILL), agent_ids could collide with stale bridge entries. **Mitigation:** the IDE's emitter sends a `removed` for any agent_id matching its own PID at startup. Belt and suspenders: rely on the bridge's `ACTIVITY_STALENESS_TIMEOUT` to reap orphans.
- **Cost-accumulation semantics** — `result.total_cost_usd` is per-turn, not cumulative. The IDE emitter must accumulate per-instance into a `_costSoFar: Map<string, number>` and emit the running total in the `cost_usd` field. Otherwise the dashboard shows the cost of the last turn, not the session.
- **Project name derivation** — the bridge derives `project` from `cwd` via `agent-bridge.py:project_from_cwd`. If the IDE sets `project` explicitly in the `added` message, the bridge may overwrite or ignore it depending on the code path. **Verify** which behavior wins; align the IDE emitter accordingly.

### 7.5 Open questions

- **(D1)** Should the IDE emit `permission_mode` on EVERY activity event, or only on mode-change events? Per-event is simpler but noisier on the wire; mode-change-only requires the dashboard to remember the last seen mode per agent. Recommend per-event — bridge passes optional fields through cheaply, JSON parse cost is negligible.
- **(D2)** Should the IDE emitter retry the socket connection on failure, or bind once at sidecar startup and give up? Recommend retry-on-event — every emit attempts a reconnect if disconnected, with a 5s backoff. Matches the bridge's own restart resilience.

---

## 8. Phase E — Spawn menu UI

**Goal:** Reproduce orchestrator.nvim's spawn-menu UX as IDE chrome (NOT a NeoVim floating window). User presses a key, sees a menu of `n/c/r N/C/R` shortcuts, picks one, that instance spawns and focuses.

This is the LAST phase before deprecation. Once it ships, the keybind surface fully matches orchestrator.nvim and the plugin can be removed.

### 8.1 Surface changes

**`runtime/init.lua`:**

- Add `<leader>aa` → `vim.rpcnotify(0, "agent", { op = "spawn_menu" })`.

**`src/symmetria_ide/app.py::_on_agent_event`:**

- Handle `op = "spawn_menu"` by setting a new `spawnMenuVisible: bool` `@Property` to true. Cleared by the menu's own dismiss path.

**New file: `qml/SpawnMenu.qml`:**

- Floating overlay parented to `Main.qml`'s root, with `anchors.fill: parent` and a semi-transparent backdrop (`color: Theme.color.bg.scrim`).
- Centered card showing six rows, one per key:
  - `n` — New (skip permissions)
  - `c` — Continue (skip permissions)
  - `r` — Resume (skip permissions)
  - `N` — New
  - `C` — Continue
  - `R` — Resume
- Single-key dispatch: pressing any of n/c/r/N/C/R triggers the corresponding spawn (`controller.spawn_instance(action, dangerous)`).
- Esc dismisses without spawning.
- Visible iff `controller.spawnMenuVisible`. Captures keyboard focus when shown.
- Binds entirely against `Theme.*` tokens. New token if needed: `Theme.color.bg.scrim` (rgba(0, 0, 0, 0.5)?), with provenance.

**`qml/Main.qml`:**

- Mount `SpawnMenu` as a sibling of `AgentPane`, on top of everything else.

### 8.2 User-facing behavior

- `<leader>aa` (anywhere in the editor) → spawn menu opens, capturing focus.
- Pressing `n` → instance spawns in skip-perms mode, menu closes, agent pane focuses.
- Pressing `Esc` → menu closes, focus returns to the editor.
- Pressing any other key → menu closes (no spawn), focus returns to the editor. (Or do we hold the menu open? Decision needed.)

### 8.3 Acceptance criteria

- Menu visually matches the IDE's chrome aesthetic (Theme tokens only).
- Keyboard-only flow: open menu, pick option, instance spawns, pane focuses — without touching the mouse.
- All six options work.
- Esc closes without side effect.
- Menu is reachable from the agent pane too (not just from the editor) — same `<leader>aa` keybind.

### 8.4 Risks

- **Focus stealing** — when the menu opens, `editor.focus = true` (the IDE's non-negotiable) must not fight with the menu's focus capture. Test: open menu, type `n`, confirm the `n` reaches the menu (not the editor) and triggers the spawn.
- **Menu visible during streaming** — if instance 1 is mid-stream and the user opens the menu, the menu must not block paint of the agent pane behind it. Use a transparent overlay + small card, not a full-window opaque modal.

### 8.5 Open questions

- **(E1)** Should pressing an unrecognized key dismiss or hold? Recommend dismiss — matches orchestrator.nvim's behavior, less surprising for a single-key picker.
- **(E2)** Do we need a project switcher in the menu (e.g., to spawn an instance in a different cwd)? **No** — out of scope per §1's non-goals. Single-project IDE.

---

## 9. Phase F — orchestrator.nvim deprecation

**Goal:** Retire the plugin from the user's daily workflow. The IDE has feature parity for the core spawn/focus/dashboard surface; the plugin's remaining unique features (edit tracker) are tracked as Phase G.

### 9.1 Gates (must all clear before this phase fires)

- ✅ Phase A — per-instance refactor.
- ✅ Phase B — multi-instance spawn + focus keybinds.
- ✅ Phase C — spawn semantics differentiated.
- ✅ Phase D — SDK→bridge emitter shipped, dashboard chips appearing.
- ✅ Phase E — spawn menu UI shipped.
- ✅ User has spent at least one full work-week using the IDE-only flow without falling back to `orchestrator.nvim`.

### 9.2 Surface changes

**`~/.dotfiles/.config/nvim/lua/jc/plugins/orchestrator.lua`:**

- Delete the file. (Or stub it to `return {}` if the user wants the slot preserved for documentation.) Run `:Lazy reload` or equivalent.

**`~/projects/orchestrator.nvim/`:**

- Tag the last working commit: `git tag -a v0-final -m "Last release before Symmetria IDE absorbs the workflow"`.
- Update `README.md` to point at Symmetria IDE as the successor: "This plugin is feature-frozen as of v0-final. New work happens in Symmetria IDE — see https://github.com/.../symmetria-ide. The plugin remains available for users running plain nvim outside the IDE."
- Update `CLAUDE.md` to mark the project as archived.

**`/home/jc/projects/symmetria-ide/CLAUDE.md`:**

- Remove the "slated for deprecation" annotation from the orchestrator.nvim cross-reference (it's now actually deprecated).
- Optionally drop the cross-reference entirely.

**`docs/agent-dashboard-integration.md`:**

- Update the deprecation gates section to mark all gates as cleared.

### 9.3 User-facing behavior

- `<leader>aN`, `<C-1>..<C-5>`, `<C-S-q>`, `<leader>aa`, etc. all work via the IDE — same keys, same feel.
- The agent dashboard continues to show every Claude session on the system, IDE-spawned or otherwise.
- Users running plain `nvim` outside the IDE can still install `orchestrator.nvim` from the v0-final tag if they want the plugin's flow.

### 9.4 Risks

- **Muscle memory regression** — if the IDE's `<leader>aN` behavior diverges in any subtle way from the plugin's, the user feels it. Phase B/C must be polished before this gate clears.
- **Bridge integration regressions** — if Phase D shipped buggy and dashboard chips stop updating after the plugin is removed, the user has no fallback. **Mitigation:** keep `orchestrator.nvim` installed (just disabled in `lazy.nvim`'s `enabled = false`) for one week post-deprecation as an instant rollback.

### 9.5 Open questions

- **(F1)** Should the deprecation commit also delete `~/projects/orchestrator.nvim` from the local filesystem? Recommend NO — keep it as an archive. The disk cost is trivial and it's the only copy of the working code.

---

## 10. Phase G — Edit tracker (late)

**Goal:** Replace orchestrator.nvim's edit tracker (`<leader>ae` quickfix, `<leader>aj` jump) with an IDE-native equivalent built on top of the SDK's structured `tool_result` rows.

This is deliberately the LAST phase. The user has signaled it's "very useful" but wants better navigation than orchestrator's quickfix; the design space is wide. Park until Phases A–F land and the user has time to articulate what they actually want.

### 10.1 Direction (sketch only — full design lands when this phase activates)

- The IDE's `SessionModel` already exposes `tool_result` rows with the original tool input (file path, old_string, new_string for Edit; file_path + content for Write). Iterate the model history once on `<leader>ae` to extract every Edit/Write/MultiEdit call across all instances, build a per-file ordered list of edits.
- UI options to compare:
  - Quickfix list (orchestrator's flow — known to feel coarse).
  - Side panel listing files with edit counts; click to expand to per-edit diffs (Zed-style).
  - Inline gutter chips in the editor (NeoVim sign column markers) at every line touched by Claude in this session.
- Cross-instance tracking — edits from all 5 sidecars merge into one tracker. Instance index is metadata on each entry.

### 10.2 Acceptance criteria

- Defer until phase activates. The user's design taste is required input.

### 10.3 Open questions

- **(G1)** Is the tracker session-scoped (clears on close) or persistent (survives IDE restart)? Recommend session-scoped initially — persistence is a separate scope creep.
- **(G2)** Does the tracker show edits the user has already manually reviewed (and would like to dismiss)? Recommend a per-entry "ack" affordance.

---

## 11. Indefinitely deferred

These are NOT cancelled. They are parked until the user signals demand. If a session approaches one of them, escalate to the user before starting work.

### 11.1 Floating multi-tab markdown prompt editor

- The orchestrator.nvim feature at `lua/orchestrator/editor.lua` (~539 lines).
- User's stated reason for deferring: doesn't use it heavily in NeoVim either.
- Reactivation trigger: user says "I miss the prompt editor" or "we need a richer composer".
- Implementation note when revived: do NOT port verbatim. Build it as IDE chrome, sized and positioned for the agent pane context, not the editor's. The IDE's existing inline composer covers the 90% case.

### 11.2 URL handler

- The orchestrator.nvim feature at `lua/orchestrator/url_handler.lua` (~259 lines).
- User's stated reason: not really important.
- Reactivation trigger: user wants to extract URLs from Claude output without copy-paste.
- Implementation note when revived: trivial against the SDK's structured event stream — scan `assistant` messages for URL patterns, render as clickable chips inline in the transcript.

### 11.3 Status bar / agent chip redesign

- The user mentioned "maybe we are going to do like a vertical agent. I don't know, we will think about that in the future."
- Defer per `.claude/memory/feedback/ui_surface_discipline.md` — wait until the SDK feed is live in the dashboard (Phase D) and we can iterate against real cadence.

### 11.4 Two-way control surface (dashboard → IDE)

- E.g., clicking an agent chip in the dashboard focuses the corresponding pane in the IDE, or sets its mode.
- The existing `IpcHandler` in `~/.config/quickshell/symmetria/services/AgentService.qml` is the precedent.
- Reactivation trigger: user wants dashboard-driven control (likely never, given keyboard-first non-negotiable).

### 11.5 Multi-project agent management

- One IDE process tracking N agents across M cwds.
- Today: IDE is single-project per process; multi-project via multiple IDE instances.
- Reactivation trigger: user wants a single IDE window with a project switcher. Not aligned with current vision.

---

## 12. Appendix

### 12.1 Keybind reference

| Key | Action | Phase |
|-----|--------|-------|
| `<leader>aN` | Spawn fresh instance, normal permissions | B (basic), C (differentiates from `<leader>an`) |
| `<leader>aR` | Resume most recent session, normal permissions | C |
| `<leader>aC` | Continue most recent session, normal permissions | C |
| `<leader>an` | Spawn fresh instance, skip permissions | C |
| `<leader>ar` | Resume most recent session, skip permissions | C |
| `<leader>ac` | Continue most recent session, skip permissions | C |
| `<leader>aa` | Open spawn menu | E |
| `<C-1>` | Focus instance 1 | B |
| `<C-2>` | Focus instance 2 | B |
| `<C-3>` | Focus instance 3 | B |
| `<C-4>` | Focus instance 4 | B |
| `<C-5>` | Focus instance 5 | B |
| `<C-S-q>` | Close focused instance | B |
| `Shift+Tab` (in pane) | Cycle permission mode for focused instance | (already shipped) |

Edit tracker keys (`<leader>ae`, `<leader>aj`) land in Phase G with their final binding TBD.

### 12.2 File index

**Symmetria IDE — touched by this PRD:**

| Path | Phases |
|------|--------|
| `src/symmetria_ide/app.py` | A, B, C, E |
| `src/symmetria_ide/session_host.py` | A, C |
| `src/symmetria_ide/session_models.py` | A |
| `qml/AgentPane.qml` | A, B |
| `qml/Main.qml` | E |
| `qml/SpawnMenu.qml` | E (new) |
| `qml/design/Theme.qml` | B, E |
| `runtime/init.lua` | B, C, E |
| `sidecar/src/index.ts` | C, D |
| `sidecar/src/emitters/agent-bridge.ts` | D (new) |
| `tests/test_app_controller_*.py` | A, B, C |
| `tests/test_session_models.py` | A |
| `tests/test_session_host_*.py` | A, C |
| `CLAUDE.md` | A, D, F |
| `docs/agent-dashboard-integration.md` | D, F |

**Symmetria Shell — possibly touched by this PRD:**

| Path | Phases |
|------|--------|
| `~/.config/quickshell/symmetria/scripts/agent-bridge.py` | D (only if the bridge rejects `nvim_pid: 0` — verify first) |
| `~/.config/quickshell/symmetria/services/AgentService.qml` | (none — pass-through) |
| `~/.config/quickshell/symmetria/modules/agentbar/AgentChip.qml` | (none for now — rich chip is post-PRD polish) |

**orchestrator.nvim — touched by this PRD:**

| Path | Phases |
|------|--------|
| `~/projects/orchestrator.nvim/README.md` | F |
| `~/projects/orchestrator.nvim/CLAUDE.md` | F |
| `~/projects/orchestrator.nvim/.git` (tag v0-final) | F |
| `~/.dotfiles/.config/nvim/lua/jc/plugins/orchestrator.lua` | F |

### 12.3 Glossary

- **Instance** — one Claude session = one sidecar subprocess = one `SessionHost` + one `SessionModel` in the IDE pool.
- **Slot** — one of the 1..5 numbered positions in the IDE's instance pool. Matches the `<C-N>` focus keybind.
- **Sidecar** — the `node sidecar/dist/index.js` process running the SDK's `query()` loop. One per instance.
- **Pool** — the dict of sidecars + models keyed by slot inside `AppController`.
- **Bridge** — `~/.config/quickshell/symmetria/scripts/agent-bridge.py`, the aggregator the dashboard reads.
- **Hook source** — agents whose activity is reported by `~/.config/quickshell/symmetria/scripts/symmetria-agent-hook.py` invoked by Claude Code's hook system.
- **SDK source** — agents whose activity is reported by the IDE sidecar's bridge emitter (Phase D).
- **Dangerous** — orchestrator.nvim shorthand for `--dangerously-skip-permissions` / SDK's `permissionMode: "bypassPermissions"`.
- **Pre-warm** — spawning the sidecar before the user sends the first message so Shift+Tab and the mode pill are live. CLAUDE.md gotcha #25.
- **Skip-perms** — same as `dangerous`.
- **Focused instance** — the one whose transcript the agent pane currently displays. Drives `controller.awaitingResponse`, `controller.permissionMode`, the composer's send target.

### 12.4 Open questions index

| ID | Phase | Question |
|----|-------|----------|
| A1 | A | `_session_hosts: dict[int, SessionHost]` vs `list[SessionHost \| None]`? |
| A2 | A | Instance indicator visible always or only when ≥ 2? |
| B1 | B | Composer text per-instance or shared? |
| B2 | B | Focus into non-existent slot: no-op or auto-spawn? |
| B3 | B | Instance indicator: chip strip or text? |
| B4 | B | Per-instance color: creation-order or slot-reuse? |
| C1 | C | `resume` keybind: auto-pick latest or open picker? |
| C2 | C | Skip-perms reflected in instance indicator color? |
| D1 | D | Emit `permission_mode` per-event or only on change? |
| D2 | D | Socket reconnect: retry-on-event or once-at-startup? |
| E1 | E | Spawn menu unrecognized key: dismiss or hold? |
| E2 | E | Spawn menu project switcher? (Answer: NO — out of scope.) |
| F1 | F | Delete orchestrator.nvim from disk on retirement? (Answer: NO.) |
| G1 | G | Edit tracker: session-scoped or persistent? |
| G2 | G | Edit tracker: per-entry "ack" affordance? |

Resolve each with the user before landing the corresponding phase. Recommendations are noted under each phase's Open Questions section but are not commitments.

### 12.5 Cross-references

- `docs/agent-dashboard-integration.md` — the dual-feed architecture this PRD operationalizes.
- `CLAUDE.md` § "The agent backend (Node SDK sidecar)" — current single-instance state.
- `CLAUDE.md` gotchas #10, #20, #24, #25 — load-bearing for any pool/permission/bridge work.
- `.claude/memory/project/meta/agent_dashboard_commitment.md` — the commitment in pointer form.
- `.claude/memory/feedback/ui_surface_discipline.md` — why we ship structural surface before visual treatment.
- `~/projects/orchestrator.nvim/lua/orchestrator/highlights.lua:16-27` — the 8-color palette to replicate.
- `~/projects/orchestrator.nvim/lua/orchestrator/spawn_menu.lua` — the spawn-menu reference behavior.
- `~/projects/orchestrator.nvim/lua/orchestrator/terminal.lua:175-191` — the spawn-type CLI args reference.
- `~/projects/orchestrator.nvim/lua/orchestrator/bridge.lua:316-334` — the bridge wire-protocol reference.
- `~/.config/quickshell/symmetria/scripts/agent-bridge.py` — the bridge implementation. Read its message-type handlers before writing the SDK→bridge emitter.
- `docs/phases.md` — the broader phase sequencing this PRD's phases nest under (all of A–G fall inside what `docs/phases.md` calls "Phase 2 follow-ups").
