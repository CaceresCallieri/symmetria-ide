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
  query,
  type CanUseTool,
  type Options,
  type PermissionResult,
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

const pendingPermissions = new Map<
  string,
  (result: PermissionResult) => void
>();

const canUseTool: CanUseTool = (toolName, input, opts) => {
  return new Promise<PermissionResult>((resolve) => {
    const requestId = randomUUID();
    pendingPermissions.set(requestId, resolve);

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
    const resolver = pendingPermissions.get(cmd.request_id);
    if (resolver === undefined) {
      log(
        `permission_response for unknown request_id ${cmd.request_id} — ignored`,
      );
      return;
    }
    pendingPermissions.delete(cmd.request_id);
    if (cmd.behavior === "allow") {
      resolver({ behavior: "allow", updatedInput: {} });
    } else {
      resolver({
        behavior: "deny",
        message: cmd.message ?? "denied by user",
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
  // `permissionMode: "default"` routes through canUseTool when present;
  // bypassPermissions / acceptEdits would short-circuit the callback for
  // some tools, defeating the whole point of the in-pane card. Stay on
  // default and let canUseTool be the single decision point.
  permissionMode: "default",
};

const translateMessage = (msg: SDKMessage): OutboundEvent | null => {
  // The Python optimistic-render in submit_prompt is the single source of
  // truth for user rows. Drop SDK echoes (both fresh and replay) so the agent
  // pane doesn't render duplicate user turns.
  if (msg.type === "user") return null;

  // Everything else is a passthrough — SessionModel._row_from_* routes by
  // top-level `type` and the inner fields match what those helpers already
  // consume (assistant.message.content, system.subtype, result.duration_ms,
  // stream_event.event.type=content_block_delta, rate_limit_event.*).
  return msg as unknown as OutboundEvent;
};

(async () => {
  log("ready; starting SDK query");
  try {
    const q = query({ prompt: userMessages, options });
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
