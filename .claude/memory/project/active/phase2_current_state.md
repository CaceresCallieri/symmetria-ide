---
name: Phase 2 agent pane — current state
description: 2026-04-26 snapshot. Permission-mode pill + Shift+Tab cycling landed atop the in-pane permission card; sidecar honours mode via setPermissionMode + canUseTool short-circuit.
type: project
originSessionId: a094e8d9-4dcb-4507-bc47-56d8a4453394
---

# Phase 2 — current state (2026-04-26)

## What's wired today

- `SessionHost` spawns the Node sidecar (`node sidecar/dist/index.js`) at IDE launch via `AppController.start()` (pre-warm with empty prompt — see the dedicated bullet below). `SYMMETRIA_IDE_AGENT_PROMPT` still pre-runs an initial turn but now flows through the hot branch of `submit_prompt` rather than spawning the sidecar a second time. Sidecar drives `@anthropic-ai/claude-agent-sdk@0.2.119` programmatically and translates SDK messages to JSONL events on stdout.
- `send_user_message` writes `{type:"user_message",content}` envelopes; `send_permission_response` writes `{type:"permission_response",request_id,behavior}` envelopes — both on the same stdin stream under `_stdin_lock`.
- `SessionModel` renders events flat with partial-text coalescing for streaming assistant turns. New `permission_request` rows carry `permission_state` + `request_id`; `resolve_permission(id, behavior)` mutates them to `"approved"`/`"denied"`.
- `AgentPane.qml` is a sibling of `NvimView` inside `Main.qml`'s `RowLayout` (60/40). Theme-tokens only; the new permission card uses `Theme.color.agent.{permissionBorder,permissionApprove,permissionDeny}`.
- `AppController.respond_to_permission` is the QML-facing slot wired from the card buttons; it dispatches host-first then model-resolves.
- **Permission-mode pill + Shift+Tab cycle (2026-04-26).** `AgentPane.qml` chrome carries a Theme-tokened pill bound to `controller.permissionMode`. Shift+Tab on the pane (root + composer) cycles `default → acceptEdits → bypassPermissions → plan → default`. The cycle slot dispatches `set_permission_mode` to the sidecar; **the sidecar's local `currentMode` is the authoritative source of truth** — the handler updates `currentMode` synchronously, emits the `permission_mode_changed` echo immediately, then fires `Query.setPermissionMode()` as a best-effort SDK sync. AppController mirrors the echo into `_permission_mode` so the QML pill reflects the accepted state. The sidecar's `canUseTool` reads `currentMode` directly and short-circuits per mode: `bypassPermissions` auto-allows everything, `plan` auto-denies everything, `acceptEdits` auto-allows the four edit tools, `default` (and `acceptEdits` for non-edit tools) round-trips through the in-pane card. `allowDangerouslySkipPermissions: true` is set in `Options` so transitions into `bypassPermissions` succeed.
- **SDK CLI pre-warm via `startup()` (2026-04-26).** Sidecar's IIFE calls `await startup({options})` and then `warm.query(userMessages)` instead of `query({prompt, options})` directly. `startup()` spawns the SDK's internal CLI subprocess AND awaits the initialize-handshake before returning, so the CLI is alive and addressable from sidecar startup — matches what Claude Code TUI does. NOTE: this is necessary but not sufficient for pre-first-message Shift+Tab cycling — the SDK's iteration loop must be actively reading control responses, which requires the prompt iterable to have yielded. See gotcha #25 for the full picture and why the sidecar-authoritative `currentMode` model above is the load-bearing fix.
- **`system: status` keepalive filter (2026-04-26).** `translateMessage` in the sidecar drops `{type: "system", subtype: "status"}` events so they never reach Python's `_row_from_system`. Without this filter, the SDK's continuous heartbeat stream rendered as "status / status" rows that drowned out real session events.
- Tests cover model permission routing, scoped `dataChanged([PermissionStateRole])`, AppController dispatch ordering, the new envelope shapes on the write path, AND the permission-mode state machine (initial, cycle order + wraparound, event-driven update, invalid mode ignored, idempotency, subprocess-closed reset).
- **Sidecar pre-warm (2026-04-26).** `AppController.start()` calls `_session_host.start("")` unconditionally so the SDK subprocess + permission-mode echo are live the moment the agent pane is reachable. `SessionHost.start("")` spawns the subprocess and skips the initial `send_user_message`; the SDK's prompt async iterable blocks on its first await until the user types. This closes the user-visible Shift+Tab silent-drop window — `cycle_permission_mode` writes were previously dropped on a non-existent stdin until the first message. The `SYMMETRIA_IDE_AGENT_PROMPT` env-var path still works: `submit_prompt` now sees `is_running == True` and takes the hot branch (`send_user_message` instead of `start(prompt)`); the synthetic user-row injection still renders the prompt optimistically.

**How to use today:** `PYTHONPATH=src python -m symmetria_ide`. `<leader>aN` / `<leader>an` opens the agent pane. `SYMMETRIA_IDE_AGENT_VIEW=1` opens it on startup. `SYMMETRIA_IDE_AGENT_PROMPT="..."` opens + pre-runs a prompt. Multi-turn is on; permission cards render inline approve/deny on tool use.

## Why we pivoted away from `claude -p --output-format stream-json`

The CLI mode self-resolves permissions server-side and exposes no in-band approve/deny surface. The SDK's `canUseTool` callback gives us a structured permission request as a typed async function — the same path Zed's `claude-code-acp`, opencode, and the official VS Code extension take.

## Sidecar build artifact

`sidecar/dist/index.js` is gitignored — `npm run build` in `sidecar/` regenerates it via esbuild (SDK marked `external` so its native binary opt-deps resolve from `node_modules` at runtime, not bundled). Requires Node `>=20`. `SessionHost.start` checks `_sidecar_dist_path()` exists before spawning and surfaces a clear log error pointing at `cd sidecar && npm install && npm run build` if missing.

## Open next steps (deferred, not blockers)

- **Permission persistence.** "Always allow X for project Y" — the SDK exposes `PermissionUpdate.addRules` via `PermissionResult.updatedPermissions`, but we don't surface that in the card yet. Add a third button (Allow once / Allow always / Deny) once the placeholder UX is exercised.
- **Stop control.** SDK's `Query.interrupt()` exists but isn't wired. Useful for mistyped prompts and runaway tool loops. Sidecar would expose it as a new inbound command (`{type:"interrupt"}`) that calls `AbortController.abort()` on the active query.
- **Turn grouping + tool-call drill-in.** Flat list intentional for the placeholder; the SDK now gives us a much richer typed event surface to iterate against.
- **Media rendering.** User-passed images, assistant-generated diagrams (inline `QtWebEngineView`), URL chips, code-fence copy actions.
- **Focus switching.** Keyboard binding to hop between editor and agent pane.
- **Session resume UI.** SDK exposes `resume`/`sessionId` options on `query()`.
- **MCP / hooks / slash commands via SDK options.** All available, none wired.

## Pointers for resumption

- `src/symmetria_ide/session_host.py` — sidecar spawn + write paths; module docstring mirrors `sidecar/src/protocol.ts`.
- `src/symmetria_ide/session_models.py` — `SessionModel.apply` event routing + permission row treatment.
- `src/symmetria_ide/app.py` — `AppController._on_session_event` Python-side routing; `respond_to_permission` slot.
- `qml/AgentPane.qml` — pane + permission card delegate variant.
- `sidecar/src/index.ts` + `sidecar/src/protocol.ts` — sidecar entrypoint and wire-protocol contract.
- `docs/phases.md` — phase sequencing reflects the sidecar pivot.
- See also: `../meta/project_governance.md` for the standards layer; `../../feedback/ui_surface_discipline.md` for the placeholder-first directive.

## Critical invariants (don't relearn)

- **Synthetic user-row injection in `submit_prompt` is the single source of truth.** The sidecar drops `SDKUserMessage` echoes to avoid duplication. Do NOT remove the synthetic injection when refactoring `submit_prompt`.
- **`_stop_event.clear()` at the top of `SessionHost.start()`** — without it, a `<leader>aN` "New Claude" restart after a prior `stop()` leaves the event set and every worker exits on its first iteration (silently non-functional).
- **Cross-thread connects use explicit `Qt.QueuedConnection`** with a grep-able comment at the connect site (project-standards §4 P2).
- **GC suspension around worker-thread emission** in `SessionHost._run_stdout_loop` (gotcha #10 mitigation, mirrors `NvimBackend`).
- **SDK version pinned exactly** to `@anthropic-ai/claude-agent-sdk@0.2.119` (no caret) for reproducibility.
- **Sidecar is pre-warmed at app start.** `AppController.start()` calls `_session_host.start("")`. Refactors that move the spawn back into `submit_prompt`'s cold branch reintroduce the silent-drop bug for `set_permission_mode` (Shift+Tab on a never-spawned subprocess does nothing visible). The `if prompt:` guard inside `SessionHost.start` is the gate that lets the empty pre-warm call skip the initial `send_user_message` — removing that guard would make pre-warm send `{"type":"user_message","content":""}` to the SDK, which causes either hallucinated context or an error.
- **Sidecar's `currentMode` is authoritative for permission gating.** `set_permission_mode` handler updates `currentMode` synchronously and emits the echo immediately; `Query.setPermissionMode()` is fire-and-forget. Refactors that revert this to "await SDK echo before mutating" reintroduce the user-visible Shift+Tab silent-drop bug — the SDK's control-protocol response requires an active iteration loop which doesn't exist pre-first-message. Full diagnosis in CLAUDE.md gotcha #25.
