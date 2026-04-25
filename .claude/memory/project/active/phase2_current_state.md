---
name: Phase 2 agent pane — current state
description: 2026-04-25 snapshot. Node SDK sidecar pivot landed; in-pane permission UI shipped. Multi-turn + permission cards verified. Open follow-ups listed.
type: project
originSessionId: a094e8d9-4dcb-4507-bc47-56d8a4453394
---

# Phase 2 — current state (2026-04-25)

## What's wired today

- `SessionHost` spawns the Node sidecar (`node sidecar/dist/index.js`) when `SYMMETRIA_IDE_AGENT_PROMPT` is set. Editor-first when unset. Sidecar drives `@anthropic-ai/claude-agent-sdk@0.2.119` programmatically and translates SDK messages to JSONL events on stdout.
- `send_user_message` writes `{type:"user_message",content}` envelopes; `send_permission_response` writes `{type:"permission_response",request_id,behavior}` envelopes — both on the same stdin stream under `_stdin_lock`.
- `SessionModel` renders events flat with partial-text coalescing for streaming assistant turns. New `permission_request` rows carry `permission_state` + `request_id`; `resolve_permission(id, behavior)` mutates them to `"approved"`/`"denied"`.
- `AgentPane.qml` is a sibling of `NvimView` inside `Main.qml`'s `RowLayout` (60/40). Theme-tokens only; the new permission card uses `Theme.color.agent.{permissionBorder,permissionApprove,permissionDeny}`.
- `AppController.respond_to_permission` is the QML-facing slot wired from the card buttons; it dispatches host-first then model-resolves.
- Tests cover model permission routing, scoped `dataChanged([PermissionStateRole])`, AppController dispatch ordering, and the new envelope shapes on the write path.

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
