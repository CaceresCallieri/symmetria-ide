// NOTE: this file is the wire-protocol reference. The Python side mirrors
// these envelope shapes in src/symmetria_ide/session_host.py — keep both in
// sync. Any change here must be reflected there (and in CLAUDE.md
// '## The agent backend (Node SDK sidecar)').

/**
 * Inbound: commands the Python side sends to the sidecar on stdin, one JSON
 * object per line.
 */
export type InboundCommand =
  | { type: "user_message"; content: string }
  | {
      type: "permission_response";
      request_id: string;
      behavior: "allow" | "deny";
      message?: string;
    };

/**
 * Outbound: events the sidecar emits to the Python side on stdout, one JSON
 * object per line.
 *
 * Most events are passthroughs of SDK messages — their shapes match what
 * SessionModel._row_from_* helpers already consume (assistant, system,
 * stream_event, result, rate_limit_event). We translate from SDKMessage into
 * a flat dict via JSON serialization, with the SDK's nested fields preserved.
 *
 * Two kinds of envelopes are sidecar-synthesized rather than passed through:
 *
 * - `permission_request` — emitted from the SDK's canUseTool callback. Carries
 *   a UUID `request_id` so the matching permission_response can resolve the
 *   awaited Promise. The SDK's CanUseTool surface gives us toolName, input,
 *   toolUseID, and optional metadata; we forward what is most useful.
 *
 * - `error` — emitted when the sidecar itself fails (auth issues, SDK
 *   exceptions). Falls into SessionModel's empty-role fallback row so the
 *   user sees something visible rather than a silent stall.
 *
 * SDKUserMessage and SDKUserMessageReplay are NOT translated to wire events.
 * The Python side already inserts a synthetic user row in `submit_prompt`
 * before sending the user_message command — re-emitting the SDK's echo would
 * duplicate every user turn in the agent pane. The Python optimistic-render
 * is the single source of truth for user-row rendering.
 */
export type OutboundEvent =
  | PassthroughEvent
  | PermissionRequestEvent
  | SidecarErrorEvent;

/**
 * Passthrough envelopes. The `type` discriminator + nested fields match the
 * existing stream-json shape that SessionModel.apply consumes. Not
 * exhaustively typed here — the SDK's full message union is hundreds of
 * fields wide and SessionModel routes by string `type` only.
 */
export interface PassthroughEvent {
  type:
    | "assistant"
    | "system"
    | "stream_event"
    | "result"
    | "rate_limit_event"
    | string;
  [key: string]: unknown;
}

export interface PermissionRequestEvent {
  type: "permission_request";
  request_id: string;
  tool_name: string;
  tool_use_id: string;
  input: Record<string, unknown>;
  /**
   * Human-readable prompt sentence rendered by the SDK bridge. Use this
   * verbatim as the card body when present.
   */
  title?: string;
  description?: string;
  display_name?: string;
  blocked_path?: string;
  decision_reason?: string;
  /** sidecar-synthesized; no equivalent in raw stream-json */
  note: "sidecar-synthesized";
}

export interface SidecarErrorEvent {
  type: "sidecar_error";
  message: string;
  /** sidecar-synthesized; no equivalent in raw stream-json */
  note: "sidecar-synthesized";
}
