---
name: tmux_server_env_leak
description: "Shared tmux server inherits its starter's Claude session env; CLAUDE_JOB_DIR leak renames every new agent session after a foreign job"
metadata: 
  node_type: memory
  type: reference
  originSessionId: 4f6949f5-3618-4cbb-a7b1-f1e0ae5ea7fb
---

# tmux server env leak — foreign session titles + poisoned resume picker

**Symptom (2026-07-13):** every NEW agent session, across all projects, was born
titled `mesura.consulting [voiced] Ok, creo que vaya a main… (Branch)` — shown in
Claude Code's header strip above the input AND as the session title in every
project's `claude -r` resume picker (making resume look broken: real sessions
hidden behind identical foreign titles). Even fresh 0-token sessions carried
`{"type":"custom-title"}` / `{"type":"agent-name"}` records with that name at
transcript birth.

**Root cause chain:**
1. The shared agent tmux server (`~/.vigilia/tmux.sock`) inherits the environment
   of whoever first touches the socket. Here it was started from INSIDE a Claude
   Code agent session (vigilia, `19412ad3`), so the server's environ carried the
   full session env: `CLAUDE_JOB_DIR`, `CLAUDE_CODE_SESSION_ID`,
   `CLAUDE_CODE_CHILD_SESSION`, `CLAUDECODE`, `CLAUDE_EFFORT`, plus stale
   `SYMMETRIA_*`.
2. tmux passes that environ to every new session's process. The spawn wrapper
   only unset `CLAUDE_CODE_CHILD_SESSION` + `CLAUDE_CODE_SESSION_ID`.
3. `CLAUDE_JOB_DIR=~/.claude/jobs/<id>` makes a fresh claude ADOPT that job and
   NAME its session after the job's `name` field (`state.json .name`) — which had
   itself been poisoned earlier with a mesura.consulting session title.

**Fixes shipped (dev, 2026-07-13):**
- `agent_harness.CLAUDE_ENV_UNSET_ARGS` (renamed from `CHILD_SESSION_UNSET_ARGS`)
  now also unsets `CLAUDE_JOB_DIR`, `CLAUDE_EFFORT`, `CLAUDECODE`,
  `CLAUDE_CODE_ENTRYPOINT`, `CLAUDE_CODE_EXECPATH` in every spawn's env wrapper.
- `runtime/agent-tmux.conf` gained `set-environment -gr <var>` removal marks for
  the CLAUDE_* set AND the SYMMETRIA_* set, so any future server start scrubs
  them for ALL processes (including plain shells), regardless of who starts the
  server. Safe with the per-spawn `env K=V` wrapper — that applies after tmux
  builds the base env.
- Live mitigation applied to the running server the same day
  (`tmux -S ~/.vigilia/tmux.sock set-environment -gr <each var>`) — holds until
  that server dies; old-stable spawns stay protected only via this until the fix
  is promoted to stable.

**Verification:** fresh session spawned on the socket after the `-gr` marks
showed 0 CLAUDE/SYMMETRIA vars. Note `-gu` is NOT enough — it only deletes the
global-table entry; the server's base environ still flows through. `-gr` is the
removal mark that actually strips vars from new processes.

**Related latent issue (flagged, not fixed):** deterministic tmux session names
+ `new-session -A` mean an IDE restart leaves orphan sessions that a later spawn
silently ATTACHES — the requested spawn type (fresh/resume/continue) is ignored
because the inner argv only runs on creation. Design tension between "re-adopt
after restart" and "spawn means what it says"; needs a user decision
(kill-orphan vs skip-to-free-slot vs startup reconciliation/adoption).

See also [daemon_freezes_agent_env](./daemon_freezes_agent_env.md) — same class
of ambient-identity poisoning, different vector (CC daemon spare pool vs tmux
server env).
