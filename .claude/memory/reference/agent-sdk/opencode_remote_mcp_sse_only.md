---
name: opencode-remote-mcp-sse-only
description: "How to inject the IDE's browser MCP into opencode agents — SSE transport, OPENCODE_CONFIG_CONTENT, headers all verified via spike"
metadata: 
  node_type: memory
  type: reference
  originSessionId: 7327feb9-c282-488b-96ee-453c17a74b72
---

# Wiring the IDE browser MCP into opencode agents

**SHIPPED 2026-07-01** — code: `agent_harness.py` (`mcp_config_env` field +
`spawn_argv`), `browser_mcp.py` (`agent_config_content` + dual-transport
`_start` + `sse_url`), `app.py::agent_spawn_argv` (harness branch). This file
keeps the durable EXTERNAL constraints (opencode's SSE-only limitation, the
merge semantics) that the code comments reference but don't fully re-derive.

opencode has **no `--mcp-config` flag** (`opencode --help`, v1.17.7), so the
claude path (`mcp_config_flag="--mcp-config"` → a temp config file) does not
translate. The three facts below were established by web research + a throwaway
spike (2026-07-01, opencode 1.17.7).

## 1. Transport: opencode `type: remote` MCP is **SSE-only** today

Our `symmetria-browser` FastMCP server is served via `streamable_http_app()`.
opencode's remote MCP prefers/negotiates **SSE**, not streamable-HTTP — a
streamable-only URL 405s ("Invalid content type, expected 'text/event-stream'").
Open upstream feature requests: github.com/anomalyco/opencode/issues/8058 and
/6242. **Fix: serve `FastMCP.sse_app()` ALONGSIDE `streamable_http_app()`** on
the same server (both methods exist on the same FastMCP instance → same tools).
Point claude at the streamable URL, opencode at the `/sse` URL. `sse_app()`
routes: GET `/sse` (stream) + POST `/messages/` (RPC). Spike confirmed
`opencode mcp list` → `✓ connected` against a standalone `sse_app()`.
Re-verify SSE is still required on any opencode version bump — if #8058 lands,
the `POST /sse` probe opencode already sends may start hitting streamable.

## 2. Injection lever: `OPENCODE_CONFIG_CONTENT` env var (inline JSON)

Mirror the existing claude-flag / opencode-env asymmetry in `agent_harness.py`
(cf. `dangerous_flag` vs `dangerous_env`). Add `mcp_config_env` naming this var;
`spawn_argv` emits it in the `env` prefix. Verified: it **deep-merges** with the
project's `opencode.json` (does NOT replace) at the **highest precedence**
(position 6), so injected servers add to — never clobber — the project's own.
Distinct from `OPENCODE_PERMISSION` (dangerous mode), so no conflict.

opencode `mcp` schema (top-level `mcp` key, NOT claude's `mcpServers`):
- remote: `{"type":"remote","url":"…/sse","enabled":true,"headers":{…}}`
- local (stdio, e.g. chrome-devtools): `{"type":"local","command":["npx","-y","chrome-devtools-mcp@<ver>","--browserUrl","http://127.0.0.1:<cdp>"],"enabled":true,"environment":{…}}`

## 3. Attribution over SSE **works** (no best-effort caveat)

opencode attaches configured `headers` to the GET `/sse` **and every POST
`/messages/`**, and FastMCP surfaces them in `request_context.request` at
tool-call time. Spike: SSE tool call carrying `X-Symmetria-Agent` returned that
value from inside the tool body. So the Stage-3 ownership glyph + `Ctrl+Shift+B`
jump light up for opencode agents too — attribution is not claude-only.

Related: per-project browser gate is harness-agnostic — see
[mcp-enablement-per-project](../../feedback/mcp_enablement_per_project.md).
