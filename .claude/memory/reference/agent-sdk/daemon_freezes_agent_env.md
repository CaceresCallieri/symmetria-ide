---
name: daemon_freezes_agent_env
description: Claude Code 2.1.x daemon+spare-pool freezes SYMMETRIA_AGENT_ID/SOCK across projects; attribution must use session_id+cwd via agent_registry
metadata: 
  node_type: memory
  type: reference
  originSessionId: abbd1f64-5c0c-4d36-a9f3-891535be69d9
  modified: 2026-08-04T20:01:12.160Z
---

# Claude Code's daemon/spare-pool breaks env-based agent attribution

**Symptom (reported 2026-07-04):** an IDE agent shows the working sparkle while
idle, while a genuinely-working agent in another project shows idle. Cross-IDE
activity desync that appeared after a Claude Code update (was on 2.1.197).

**Root cause.** Claude Code 2.1.x runs a **per-user daemon + pre-warmed spare
pool** (one pool dir `/tmp/cc-daemon-<uid>/<id>/` — `cc-daemon-1000` here). The `claude` process the IDE
spawns is only a thin **launcher**; it spawns a `claude daemon run` child, which
hosts the real interactive session as a **grandchild in a background PTY**
(`--bg-pty-host … --session-id … --effort …`). The daemon pre-warms **spare**
`claude` processes *before* any project claims them, and those spares inherit the
environment of **whoever created the pool first**. So `SYMMETRIA_AGENT_ID` and
`SYMMETRIA_IDE_AGENT_SOCK` — the two env vars the whole agent-ownership inversion
keyed identity on — get **frozen to the pool creator** and reused across
unrelated projects. Verified live: processes with `cwd=/home/jc/projects/vigilia`
running under the *team* agent's daemon all carried `SYMMETRIA_AGENT_ID=3741867_1`
(team) + team's socket. Even this diagnosing session (`cwd=symmetria-ide`) was
stamped team's id.

Consequence: agent B's hook events report to agent A's IDE under agent A's id.
A's sparkle lights for B's work (`agent_activity.py` keyed slot purely on the env
`agent_id` prefix); B's own IDE never hears about B.

**The signals that survive the daemon** (and the basis of the fix): the hook's
stdin `session_id` is the real per-session id (distinct per project even when
`agent_id` collides), and the hook process runs in the session's **real cwd**
(`os.getcwd()`). Only the env is poisoned. `--session-id <uuid>` injection was
considered but is riskier — the spawn path already scrubs CC-internal session env
vars (`CHILD_SESSION_UNSET_ARGS` in `agent_harness.py` unsets
`CLAUDE_CODE_SESSION_ID`) to force top-level transcript persistence, and injecting
a session id touches that same session-identity machinery — so it was NOT done.

**Fix shipped (dev 2026-07-04, PROMOTED — verified in `main` 2026-08-04) —
`see src/symmetria_ide/agent_registry.py`:**
- On-disk cross-IDE registry `$XDG_RUNTIME_DIR/symmetria-ide/registry/<pid>.json`
  = `{pid, socket, project_root, sessions{session_id: slot}}`, one file per IDE.
- The reporter hook (`runtime/symmetria-ide-agent-hook.py`) now adds `cwd` and
  **self-routes**: `resolve_socket(session_id, cwd)` → owning IDE's socket (by
  session id, else by project root), falling back to the frozen env socket.
- `AppController._on_agent_hook` resolves the slot via
  `agent_registry.resolve_slot_for_event` (session match → legacy env match →
  project-root claim of the newest unbound slot) instead of the env prefix, then
  normalizes `payload["agent_id"]` to the true `<pid>_<slot>` so the activity
  machine's subagent-depth counter can't collide across projects.
- `_sync_agent_registry()` re-publishes on spawn/close/root-change/session-bind;
  `reap_dead()` at startup, `remove_entry()` on shutdown.

**Rollout: DONE.** This needed stable promotion to reach the daily driver, and it
got there — checked 2026-08-04 by diffing `main..dev` over all three pieces
(`agent_registry.py`, `runtime/symmetria-ide-agent-hook.py`, and app.py's
`resolve_slot_for_event` call path): identical in both, so nothing here is
pending. Still only helps **newly-spawned** agents (a running agent baked its
reporter path into `--settings` at spawn). Degrades cleanly: empty registry →
old frozen-env-socket behaviour.

**Known remaining gap (still open as of 2026-08-04):** the **status-line tap**
(`_on_status_line` + `symmetria-statusline-tap.py` in dotfiles) still uses
env-prefix resolution, so model/effort/context% display can misattribute for
daemon-pooled agents. Not the reported symptom (sparkle); tracked as follow-up.
opencode is unaffected (separate binary, no shared claude daemon).

Narrower than it looks, though: the tap's **account usage** (5h/7d rate limits →
the subscription-usage panel) is ingested BEFORE the per-slot guard in
`_on_status_line`, deliberately, and those numbers are account-global anyway — so
a misattributed tap cannot corrupt them. Only the three per-agent fields
(model / effort / context%) are exposed to this.
