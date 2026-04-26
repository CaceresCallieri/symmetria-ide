// Sidecar entry point.
//
// Spawned by Python's SessionHost. Reads JSONL InboundCommand on stdin,
// forwards user_message commands as SDKUserMessage entries onto an async
// iterable that the SDK's query() consumes. Translates SDK messages back to
// JSONL OutboundEvent on stdout. The SDK's canUseTool callback fires before
// each tool invocation; we synthesize a permission_request envelope on
// stdout, store the awaited Promise resolver in `pendingPermissions`, and
// resolve it when a matching permission_response command arrives on stdin.
//
// On stdin EOF (parent closed the pipe) or SIGTERM/SIGINT: drain pending
// events, close the user-message iterable so the SDK's query() returns,
// exit 0.

import { randomUUID } from "node:crypto";
import readline from "node:readline";
import {
  startup,
  type CanUseTool,
  type Options,
  type PermissionResult,
  type Query,
  type SDKMessage,
  type SDKUserMessage,
} from "@anthropic-ai/claude-agent-sdk";

import type { InboundCommand, OutboundEvent } from "./protocol.js";

const writeEvent = (event: OutboundEvent): void => {
  process.stdout.write(`${JSON.stringify(event)}\n`);
};

const log = (msg: string): void => {
  process.stderr.write(`[sidecar] ${msg}\n`);
};

// --- Permission mode state ------------------------------------------------
//
// `currentMode` is the sidecar's authoritative view of the SDK's permissionMode.
// It drives two paths: (a) the canUseTool short-circuit (Step 4) and (b) the
// permission_mode_changed echo emitted to Python so the AppController can
// render the mode pill. We track it locally rather than reading from the SDK
// because the SDK exposes no synchronous getter — once the user calls
// `Query.setPermissionMode(mode)` we update `currentMode` only on success.
//
// `queryInstance` is captured after the query() call below so handleCommand
// can dispatch setPermissionMode requests onto it. The variable is `null`
// until the IIFE at the bottom assigns it; set_permission_mode commands
// arriving before the SDK is ready are ignored with a log line.
type AgentPermissionMode = "default" | "acceptEdits" | "bypassPermissions" | "plan";
const VALID_MODES: ReadonlyArray<AgentPermissionMode> = [
  "default",
  "acceptEdits",
  "bypassPermissions",
  "plan",
];
let currentMode: AgentPermissionMode = "default";
let queryInstance: Query | null = null;
const EDIT_TOOLS: ReadonlySet<string> = new Set([
  "Edit",
  "Write",
  "MultiEdit",
  "NotebookEdit",
]);

// --- User-message queue (async iterable bridging stdin → SDK) -------------
//
// The SDK's query() consumes an AsyncIterable<SDKUserMessage>. We push to it
// asynchronously as new user_message commands arrive on stdin. The pattern:
// `pending` holds buffered messages, `notifyResolver` (when set) is the
// resolver of a Promise the iterable is currently awaiting. push() either
// appends to `pending` or — if the iterable is mid-await — resolves
// immediately. closeQueue() flips `closed` so the iterable terminates.

const pending: SDKUserMessage[] = [];
let notifyResolver: (() => void) | null = null;
let closed = false;

const push = (msg: SDKUserMessage): void => {
  pending.push(msg);
  if (notifyResolver !== null) {
    const r = notifyResolver;
    notifyResolver = null;
    r();
  }
};

const closeQueue = (): void => {
  closed = true;
  if (notifyResolver !== null) {
    const r = notifyResolver;
    notifyResolver = null;
    r();
  }
};

const userMessages = (async function* (): AsyncIterable<SDKUserMessage> {
  while (true) {
    if (pending.length > 0) {
      const next = pending.shift();
      if (next !== undefined) {
        yield next;
      }
      continue;
    }
    if (closed) return;
    await new Promise<void>((resolve) => {
      notifyResolver = resolve;
    });
  }
})();

// --- Permission state -----------------------------------------------------
//
// `pendingPermissions` maps a request_id to the resolver of a Promise the
// SDK's canUseTool callback is awaiting. We synthesize a UUID per request,
// emit a permission_request envelope to stdout, and stash the resolver here.
// When the matching permission_response arrives on stdin we look it up,
// resolve with the corresponding PermissionResult, and delete the entry.

// Each entry stores the resolver, the onAbort handler (so handleCommand
// can remove the abort listener on normal resolution and prevent stale
// listeners from accumulating on opts.signal), and the original tool
// `input`. The SDK's allow path threads `updatedInput` through to the
// actual tool invocation; if we resolve with `{ behavior: "allow" }`
// alone, `updatedInput` is `undefined` and each tool's own Zod schema
// (e.g. Edit's required {file_path, old_string, new_string}) rejects.
// `updatedInput?` in sdk.d.ts:1769 is a declarative lie — runtime is
// strict. Echoing the original input is the safe default for the
// placeholder UX (no "edit-and-approve" path yet).
type PendingPermission = {
  resolve: (result: PermissionResult) => void;
  onAbort: () => void;
  signal: AbortSignal;
  input: Record<string, unknown>;
};

const pendingPermissions = new Map<string, PendingPermission>();

const canUseTool: CanUseTool = (toolName, input, opts) => {
  // Mode-driven short-circuit. Per the Step 1 protocol-discovery finding:
  // the SDK's permissionMode gating logic lives in a native binary and
  // cannot be inspected by source-grep, so we apply our own conservative
  // policy here regardless of whether the SDK also short-circuits — duplicate
  // `allow` is idempotent and the SDK takes the first resolution. Branches
  // matching `bypassPermissions` / `plan` / `acceptEdits` (for edit tools)
  // resolve immediately and do NOT emit a permission_request envelope, so
  // the in-pane card is correctly suppressed for the auto-resolved cases
  // while remaining the single decision point under `default` and for the
  // non-edit tools under `acceptEdits`.
  if (currentMode === "bypassPermissions") {
    // Echo input as updatedInput per gotcha #24 (CLAUDE.md): the SDK's
    // PermissionResultAllow.updatedInput is declaratively optional but
    // functionally required — Edit/Write Zod schemas reject undefined.
    return Promise.resolve<PermissionResult>({
      behavior: "allow",
      updatedInput: input,
    });
  }
  if (currentMode === "plan") {
    return Promise.resolve<PermissionResult>({
      behavior: "deny",
      message: "plan mode — tool execution suppressed",
    });
  }
  if (currentMode === "acceptEdits" && EDIT_TOOLS.has(toolName)) {
    return Promise.resolve<PermissionResult>({
      behavior: "allow",
      updatedInput: input,
    });
  }
  // `default` (and `acceptEdits` for non-edit tools) — full round-trip.
  return new Promise<PermissionResult>((resolve) => {
    const requestId = randomUUID();

    // If the SDK aborts mid-decision (e.g. session shutdown, query
    // cancellation), resolve with deny so the caller's awaited tool
    // call returns cleanly rather than hanging.
    const onAbort = (): void => {
      if (pendingPermissions.has(requestId)) {
        pendingPermissions.delete(requestId);
        resolve({
          behavior: "deny",
          message: "aborted",
          interrupt: true,
        });
      }
    };
    opts.signal.addEventListener("abort", onAbort, { once: true });
    pendingPermissions.set(requestId, {
      resolve,
      onAbort,
      signal: opts.signal,
      input,
    });

    writeEvent({
      type: "permission_request",
      request_id: requestId,
      tool_name: toolName,
      tool_use_id: opts.toolUseID,
      input,
      title: opts.title,
      description: opts.description,
      display_name: opts.displayName,
      blocked_path: opts.blockedPath,
      decision_reason: opts.decisionReason,
      note: "sidecar-synthesized",
    });
  });
};

// --- Inbound command handling --------------------------------------------

const handleCommand = (cmd: InboundCommand): void => {
  if (cmd.type === "user_message") {
    push({
      type: "user",
      message: { role: "user", content: cmd.content },
      parent_tool_use_id: null,
    });
    return;
  }
  if (cmd.type === "permission_response") {
    const entry = pendingPermissions.get(cmd.request_id);
    if (entry === undefined) {
      log(
        `permission_response for unknown request_id ${cmd.request_id} — ignored`,
      );
      return;
    }
    pendingPermissions.delete(cmd.request_id);
    // Remove the abort listener — the permission was resolved via the
    // normal (non-abort) path, so the { once: true } listener would
    // otherwise linger on opts.signal until it is GC'd.
    entry.signal.removeEventListener("abort", entry.onAbort);
    if (cmd.behavior === "allow") {
      // Echo the original input as updatedInput — see PendingPermission
      // comment for why this is mandatory despite the `?` in the type.
      entry.resolve({ behavior: "allow", updatedInput: entry.input });
    } else {
      entry.resolve({
        behavior: "deny",
        message: cmd.message ?? "denied by user",
      });
    }
    return;
  }
  if (cmd.type === "set_permission_mode") {
    if (!VALID_MODES.includes(cmd.mode)) {
      log(`set_permission_mode: invalid mode ${cmd.mode} — ignored`);
      return;
    }
    // Sidecar's `currentMode` is the authoritative source of truth for
    // permission gating because (a) `canUseTool` short-circuits on it
    // directly — that's the actual decision point for tool calls, and
    // (b) `Query.setPermissionMode()` is a control-protocol call that
    // requires an active iteration loop to be processed. Pre-first-
    // message the iteration is blocked on the empty user-message queue,
    // so setPermissionMode promises queue forever and never resolve.
    // We update `currentMode` + emit the echo synchronously here so
    // the QML pill is responsive immediately (matching what Claude
    // Code TUI does — it manages mode locally and sends it with the
    // next turn rather than asking the API for permission to change
    // modes pre-turn).
    currentMode = cmd.mode;
    writeEvent({
      type: "permission_mode_changed",
      mode: cmd.mode,
      note: "sidecar-synthesized",
    });
    // Best-effort SDK push so server-side mode awareness tracks our
    // local state once the session is live. If queryInstance is null
    // (race window before startup() returns) or the call rejects (SDK
    // refuses the transition — extremely unlikely with
    // allowDangerouslySkipPermissions=true), we log and continue. The
    // local currentMode + canUseTool short-circuit are already
    // authoritative, so a failed SDK push does NOT affect tool gating.
    if (queryInstance !== null) {
      queryInstance.setPermissionMode(cmd.mode).catch((err: unknown) => {
        const message = err instanceof Error ? err.message : String(err);
        log(`setPermissionMode(${cmd.mode}) rejected: ${message}`);
      });
    }
    return;
  }
};

const rl = readline.createInterface({ input: process.stdin });

rl.on("line", (line: string) => {
  const trimmed = line.trim();
  if (trimmed === "") return;
  let cmd: InboundCommand;
  try {
    cmd = JSON.parse(trimmed) as InboundCommand;
  } catch (err) {
    log(`malformed inbound JSON: ${(err as Error).message}`);
    return;
  }
  handleCommand(cmd);
});

rl.on("close", () => {
  log("stdin closed; closing user-message queue");
  closeQueue();
});

// --- SDK query loop ------------------------------------------------------

const abortController = new AbortController();

const shutdown = (signal: string): void => {
  log(`received ${signal}; aborting query`);
  abortController.abort();
  closeQueue();
};

process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT", () => shutdown("SIGINT"));

const options: Options = {
  abortController,
  canUseTool,
  includePartialMessages: true,
  // Initial mode is `"default"` — every fresh sidecar starts in the
  // standard prompt path. Mode transitions land via the `set_permission_mode`
  // inbound command, which calls `queryInstance.setPermissionMode(mode)`
  // (sdk.d.ts:1977). The flag below MUST be `true` because the SDK
  // refuses to enter `bypassPermissions` without it (sdk.d.ts:3199–3202);
  // we set it eagerly so the user-driven cycle into bypass succeeds.
  // canUseTool short-circuits in our callback below based on `currentMode`,
  // so even with the flag set we retain the in-pane card UX for `default`
  // and the non-edit tools under `acceptEdits`.
  permissionMode: "default",
  allowDangerouslySkipPermissions: true,
};

// Anthropic's message protocol overloads the `user` role for two semantically
// distinct things: (a) what the human said, (b) tool_result blocks fed back to
// the model. We must let (b) through so the agent pane can show what each tool
// returned, but suppress (a) since Python's optimistic-render in submit_prompt
// is the single source of truth for user-prompt rows. Disambiguation is by
// content shape — a `user` envelope carrying any `tool_result` block is type
// (b) and gets passed through; one whose content is purely text is type (a)
// and is dropped.
const containsToolResult = (msg: SDKMessage): boolean => {
  if (msg.type !== "user") return false;
  const content = msg.message?.content;
  if (!Array.isArray(content)) return false;
  return content.some(
    (block) =>
      typeof block === "object" &&
      block !== null &&
      (block as { type?: unknown }).type === "tool_result",
  );
};

// SDK heartbeat / keepalive messages arrive as `{type: "system", subtype:
// "status"}` and have no UI value — they're internal liveness signals. The
// Python-side `_row_from_system` falls back to `text = subtype` for any
// unknown subtype, so without filtering they render as a continuous stream
// of "status / status" rows that drown out real session events. Drop them
// at the sidecar boundary so the wire never carries them. New `system`
// subtypes still pass through (per the placeholder discipline of letting
// unknown envelopes surface) — only the known-noise "status" is filtered.
const isSystemStatusKeepalive = (msg: SDKMessage): boolean =>
  msg.type === "system" &&
  (msg as unknown as { subtype?: unknown }).subtype === "status";

const translateMessage = (msg: SDKMessage): OutboundEvent | null => {
  if (msg.type === "user" && !containsToolResult(msg)) return null;
  if (isSystemStatusKeepalive(msg)) return null;

  // Everything else is a passthrough — SessionModel._row_from_* routes by
  // top-level `type` and the inner fields match what those helpers already
  // consume (assistant.message.content, system.subtype, result.duration_ms,
  // stream_event.event.type=content_block_delta, rate_limit_event.*,
  // user.message.content[*tool_result*]).
  return msg as unknown as OutboundEvent;
};

(async () => {
  log("ready; warming up SDK");
  // Use startup() instead of calling query() directly so the SDK's
  // internal CLI subprocess is spawned + initialize-handshaked BEFORE
  // any user message flows. With direct query() the CLI is lazily
  // spawned only when the prompt iterable yields its first value, so
  // setPermissionMode calls land on a non-existent control channel
  // until the first user_message arrives — user-visible symptom: the
  // permission-mode pill in the agent pane refuses to cycle until the
  // first prompt is sent. WarmQuery.query() returns a Query whose CLI
  // is already alive, so setPermissionMode and the system:init event
  // both fire immediately. Matches what Claude Code TUI does (sdk.d.ts:5142).
  let warm;
  try {
    warm = await startup({ options });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    log(`startup error: ${message}`);
    writeEvent({
      type: "sidecar_error",
      message: `startup failed: ${message}`,
      note: "sidecar-synthesized",
    });
    process.exit(1);
  }
  try {
    const q = warm.query(userMessages);
    queryInstance = q;
    // Emit the initial mode echo so AppController's _permission_mode
    // matches the sidecar's authoritative `currentMode` from the start.
    // Without this, the QML pill would render as `default` only because
    // that's the Python field's default — there'd be no positive
    // confirmation that the SDK actually started in default. The echo
    // closes that gap and makes the wire protocol self-describing.
    writeEvent({
      type: "permission_mode_changed",
      mode: currentMode,
      note: "sidecar-synthesized",
    });
    for await (const msg of q) {
      const event = translateMessage(msg);
      if (event !== null) writeEvent(event);
    }
    log("query loop ended");
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    log(`query error: ${message}`);
    writeEvent({
      type: "sidecar_error",
      message,
      note: "sidecar-synthesized",
    });
    process.exit(1);
  }
  process.exit(0);
})();
