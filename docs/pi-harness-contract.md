# Pi harness — cross-repository contract

**Frozen by the IDE phase; implemented by later phases in `/home/jc/projects/pi-agent`.**

Pi (`pi`) is a first-class agent harness in Symmetria IDE: it runs in an
IDE-owned agent slot exactly like Claude Code and OpenCode. Two of the things it
needs — activity reporting and MCP tool delivery — cannot live in this
repository, because Pi has neither a hook system the IDE can register from the
launch argv (claude's `--settings`) nor any MCP client of its own. Both are
supplied by a **Symmetria extension shipped inside the Pi package**.

This file is the only thing the two sides share. Everything below is a wire
value: changing one without the other silently degrades a feature rather than
failing loudly, so treat every name here as load-bearing.

Ownership split:

| Side | Owns |
|---|---|
| `symmetria-ide` | The harness registry entry, the spawn argv, the env below, the socket server, the MCP config file |
| `pi-agent` | The extension that reads that env, translates Pi's events into the envelope below, and mirrors the MCP servers as Pi tools |

---

## 1. Process identity and delivery — environment variables

The IDE wraps every Pi spawn in `env …` (see `agent_harness.spawn_argv`;
`KSession::setEnvironment` is not QML-reachable, which is why it is an argv
wrapper and not a process-env mutation).

| Variable | Set when | Meaning |
|---|---|---|
| `SYMMETRIA_AGENT_ID` | always | `<ide_pid>_<slot>`. The extension's gate: **absent → do nothing at all**, so a Pi started by hand outside the IDE is untouched. |
| `SYMMETRIA_IDE_AGENT_SOCK` | always | Unix socket path of the owning IDE's `AgentEventsServer`. Where §2 envelopes go. |
| `SYMMETRIA_IDE_MCP_CONFIG` | when the IDE's MCP server is up | Path to this agent's MCP config **file** (§3). The pi-specific delivery of the very same file claude receives via `--mcp-config`. |
| `SYMMETRIA_IDE_PI_EXTENSION` | dev override only | Absolute path to an unpromoted extension file; the IDE appends `--extension <path>`. Production relies on Pi's own package discovery instead — the globally registered package is `pi-agent-stable`, so this exists purely so a dev worktree can be exercised without a promotion. |

Pi is **not** subject to Claude Code 2.1.x's daemon spare-pool env freeze, so
`SYMMETRIA_AGENT_ID` and `SYMMETRIA_IDE_AGENT_SOCK` are trustworthy here: the
extension reports to the env socket directly and does not re-resolve a target
through the cross-IDE registry. It still sends `session_id` + `cwd` (§2) so the
IDE's existing `agent_registry.resolve_slot_for_event` path works unchanged for
every harness.

### Launch flags (IDE side, for reference)

| Purpose | Flag |
|---|---|
| fresh | *(none)* |
| continue | `-c` |
| resume, interactive picker | `-r` |
| resume, exact session | `--session <id>` |
| model | `--model <id>` |
| reasoning effort | `--thinking off\|minimal\|low\|medium\|high\|xhigh\|max` |
| permissive variant | `--approve` |

⚠ **`-r` and `--session` are not interchangeable.** A non-flag token after `-r`
is pushed onto Pi's `messages` array (`dist/cli/args.js:194`), so `pi -r <uuid>`
opens the picker *and* queues the uuid as the session's first user message.
Resume-by-id must always be `--session`.

⚠ **`--approve` is not a skip-permissions flag.** It sets
`projectTrustOverride` — it suppresses the blocking project-trust dialog that
would otherwise wedge a freshly spawned slot. Pi has no per-tool permission gate
at all, so both spawn variants are equally unrestricted on tools. Do not
document it as parity with claude's `--dangerously-skip-permissions`.

---

## 2. Activity reporting — the hook socket envelope

One **JSON object per connection**, newline-terminated, to
`SYMMETRIA_IDE_AGENT_SOCK`. Fire-and-forget: ~1s timeout, every error swallowed,
never awaited on a path that can delay a turn.

This is a **subset** of the envelope claude's reporter
(`runtime/symmetria-ide-agent-hook.py`) sends — every field below is one claude
also sends, but Pi omits four claude-only ones. The IDE consumes it with **no
IDE-side change** because `agent_activity` reads each of them through
`event.get(..., <default>)`, so omission degrades to the neutral branch rather
than raising:

| Claude-only field | Why Pi omits it | What the IDE does without it |
|---|---|---|
| `permission_mode` | Pi has no per-tool permission gate at all, so there is no mode to report. | `in_plan_mode` is False — correct, since Pi has no plan mode. |
| `source` | Claude-specific `SessionStart` provenance (`"clear"` vs a real start). | The `SessionStart`+`source == "clear"` reset branch never fires. |
| `is_interrupt` | Undocumented even on claude; Pi surfaces no equivalent. | The `PostToolUseFailure(is_interrupt)` idle branch never fires; `agent_settled` → `Stop` already covers Pi's idle edge. |
| `idle_notification` | Set from claude's hook argv (`--idle-notification`); Pi has no such invocation. | The `Notification` idle branch never fires. Pi sends no `Notification` events either. |

Adding any of these later is additive and needs no IDE change; omitting a field
from §2's table below is **not** — those are load-bearing.

```json
{
  "type": "hook",
  "agent_id": "<ide_pid>_<slot>",
  "hook_event_name": "PreToolUse",
  "session_id": "019fc9e5-…",
  "cwd": "/home/jc/projects/example",
  "tool_name": "Edit",
  "tool_path": "/home/jc/projects/example/src/thing.ts",
  "event_ts_ns": 1785000000000000000
}
```

| Field | Rule |
|---|---|
| `"type"` | Always the literal `"hook"`. `AgentEventsServer._serve` (`agent_events.py`) names exactly three sibling routes — `stt_inject`, `stt_recording`, `status_line` — and **`hook` is the `else` branch**, so any unrecognised type falls through to it silently. Send the literal anyway: a typo would still be routed as a hook today, but would break the moment a fourth explicit route is added. |
| `"agent_id"` | Verbatim `SYMMETRIA_AGENT_ID`. |
| `"hook_event_name"` | One of the §2.1 vocabulary. Anything else is logged as unmapped. |
| `"session_id"` | `ctx.sessionManager.getSessionId()`. This is what the IDE persists for `--session` restore, so it must be the resumable id, not a display name. |
| `"cwd"` | `ctx.cwd`. Slot attribution falls back to this when the id is untrustworthy. |
| `"tool_name"` | Normalised per §2.2. `""` for non-tool events. |
| `"tool_path"` | **Absolute.** `resolve(ctx.cwd, args.path)` — Pi's write tools take `path`, relative *or* absolute. The IDE realpaths it for the worktree follow and the per-agent change filter; a relative path silently matches nothing. `""` when the event has no file target. |
| `"event_ts_ns"` | `CLOCK_REALTIME` nanoseconds. Used for out-of-order logging only. |

### 2.1 Event mapping

| Pi extension event | `hook_event_name` |
|---|---|
| `session_start` | `SessionStart` |
| `agent_start` | `UserPromptSubmit` |
| `tool_execution_start` | `PreToolUse` |
| `tool_execution_end` | `PostToolUse` |
| `tool_execution_end` with `isError` | `PostToolUseFailure` |
| `agent_settled` | `Stop` |
| `session_shutdown` | `SessionEnd` |

`agent_settled` is the busy→idle edge the `wait_for_agent` coordination trigger
watches. Pi's is locally observable and therefore *more* reliable than
OpenCode's, which is inferred from bridge-snapshot diffs.

### 2.2 Tool-name normalisation

Pi names its tools in lowercase; the IDE's `_AGENT_WRITE_TOOLS` and
`agent_activity.TOOL_DISPLAY_NAMES` are keyed on Claude's names. The extension
normalises so **neither** IDE table needs an entry for Pi:

| Pi tool | Reported as | Note |
|---|---|---|
| `bash` | `Bash` | Triggers the IDE's Bash dirty-diff attribution window. |
| `edit` | `Edit` | Write tool — drives worktree follow + the touched set. |
| `write` | `Write` | Write tool. |
| `read` | `Read` | Deliberately *not* a write tool: exploration must never yank the chrome. |
| `grep` | `Grep` | |
| `find` | `Glob` | Closest Claude equivalent; the IDE only uses it for the chip's tool label. |
| `ls` | `Read` | Pi has no Claude-side `LS`; folding it into `Read` keeps it non-write. |

An unlisted tool is forwarded with its raw name. That is safe — unknown names
fall through to a generic chip label — but it will not participate in write
attribution, so add a row here when Pi grows a new file-mutating tool.

### 2.3 Root-session-only reporting

Only the **root** session reports. Pi's `pi-flow` subagents run **in-process**
and inherit the parent's extension list (`spawn.ts`'s `extensionsOverride`
passes `base.extensions` through), so a naive extension reports for up to 12
concurrent subagents under one `SYMMETRIA_AGENT_ID`. That is the same class of
desync as the Claude daemon caveat: an idle agent shows working and vice versa.

Two independent guards:

1. **Session-dir guard** — skip when `ctx.sessionManager.getSessionDir()` is
   under `subagent-sessions`. (`PI_SUBAGENT_SESSION_DIR_NAME` is a pi-flow
   internal constant, not public API — verify it against a live fan-out before
   relying on it alone.)
2. **`globalThis` latch** keyed on agent id + session id, so a doubly-loaded
   copy (the dev `--extension` override alongside the package-discovered one)
   reports once.

---

## 3. MCP delivery — the config file

`SYMMETRIA_IDE_MCP_CONFIG` points at the **unmodified** file
`browser_mcp.agent_config_path` already writes for claude. Reusing it verbatim
is the whole design: per-project gating, call-time gating and browser
attribution keep working with no new IDE-side tool surface.

```json
{
  "mcpServers": {
    "symmetria-browser": {
      "type": "http",
      "url": "http://127.0.0.1:<port>/mcp",
      "headers": { "X-Symmetria-Agent": "<ide_pid>_<slot>" }
    },
    "chrome-devtools": {
      "command": "npx",
      "args": ["chrome-devtools-mcp", "--browserUrl", "http://127.0.0.1:<cdp>"]
    }
  }
}
```

| Entry | Transport | Presence |
|---|---|---|
| `symmetria-browser` | streamable `http` | **Always**, whenever the IDE's server is up. It hosts `wait_for_agent`, and coordination must work in every project. |
| `chrome-devtools` | `stdio` (`command` + `args`) | Only when the project opted in via `.symmetria/ide.json`'s `browser_agents`. This entry is the ~80–150 MB Node process the gate exists to avoid. |

Extension obligations:

- Absent or unreadable `SYMMETRIA_IDE_MCP_CONFIG` → register zero tools and
  **still start the session**. MCP is an enhancement, never a launch dependency.
- Connect per entry by shape: `type: "http"` → streamable-HTTP client carrying
  the `headers` map verbatim; a `command`/`args` entry → `stdio` client. The
  `X-Symmetria-Agent` header is what attributes a browser window to this agent
  (the chip globe) — dropping it does not fail anything, it silently
  un-attributes every call.
- Mirror `listTools()` one-for-one via Pi's `registerTool`, passing the remote
  JSON Schema through unchanged (`Type.Unsafe`), and delegate `execute` to
  `callTool`.
- Connect **asynchronously** from `session_start`. A failing server is logged
  and skipped; the others still register.
- Do **not** re-implement the browser tools' gating. `browser_open`,
  `browser_list_windows` and `browser_request_attention` are additionally gated
  server-side at call time and return a clear disabled-error in a non-opted
  project; `wait_for_agent` is never gated.

---

## 4. What this contract deliberately does not cover

- **⚠ Coordination on a Pi agent, until §2's reporter actually ships.**
  `wait_for_agent` is delivered to Pi from day one (§3 — the `symmetria-browser`
  entry is always present), so a Pi agent can *call* it and a registration
  naming a Pi agent as the WATCHED one will **arm**. It cannot yet **fire**: the
  trigger is driven by a busy→idle edge, and until the extension sends
  `agent_settled` → `Stop` the IDE never sees one for a Pi slot, so the trigger
  waits forever. Registering while Pi is already idle *with* a session id takes
  the immediate-evaluate path instead and does complete (with the §4 caveat
  below, since Pi is not judgeable). **This is a phase boundary, not a
  coordination bug** — do not debug it as one. Delete this bullet when the
  reporter phase lands.
- **VPS/remote Pi.** Remote spawns stay claude-only; `remote_location.py` is untouched.
- **A Pi coordination judge.** A watched Pi agent gets the generic non-claude
  caveat (`agent_coordination.non_claude_caveat_text`). Pi's transcript at
  `getSessionFile()` is genuine JSONL, so a judge is buildable — later.
- **Status-line parity.** Model/thinking/context% in the IDE status bar would
  ride the socket's existing `status_line` envelope, not the `hook` one above.
