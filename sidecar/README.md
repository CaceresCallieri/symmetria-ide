# symmetria-ide sidecar

A Node sidecar that drives `@anthropic-ai/claude-agent-sdk` programmatically.
The Python `SessionHost` spawns this process and talks JSONL on stdin/stdout.

See the project root `CLAUDE.md` section `## The agent backend (Node SDK sidecar)`
for the full wire-protocol contract.

## Setup

Requires Node `>=20`.

```sh
cd sidecar
npm install
npm run build
```

`npm run build` produces `dist/index.js` (gitignored — regenerate after each
`git pull`).

## Wire protocol

Inbound commands (Python → sidecar, JSONL on stdin):

```jsonl
{"type":"user_message","content":"..."}
{"type":"permission_response","request_id":"<uuid>","behavior":"allow"|"deny"}
```

Outbound events (sidecar → Python, JSONL on stdout): typed SDK events
translated to the existing `SessionModel.apply` shapes (`assistant`, `system`,
`result`, `stream_event`, `rate_limit_event`), plus a synthesized
`permission_request` envelope when the SDK's `canUseTool` callback fires.

See `src/protocol.ts` for the full TypeScript types.

## Why a sidecar (and not just `claude -p`)

The CLI's `-p` mode self-resolves permission requests server-side and does
not surface a structured request envelope on stdout. The SDK's `canUseTool`
callback exposes the same protocol as a typed async function, which lets the
Qt UI render an in-pane approve/deny card. Same pattern Zed and the official
VS Code extension use.
