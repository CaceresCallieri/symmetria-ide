---
name: tmux_swallows_osc_title
description: "Under the tmux substrate, agent chip names go blank because tmux doesn't forward the harness's OSC title to the outer KSession unless set-titles is on"
metadata: 
  node_type: memory
  type: reference
  originSessionId: 6971a6bf-9baf-438e-b7b9-fb00127fe464
---

# tmux swallows the OSC title → nameless agent rows

**Symptom (2026-07-21):** every agent chip in the AgentTopBar (the thread rail
since 2026-08-15) rendered as a bare
slot number (1..5) with no session name, even though claude/opencode were clearly
working. Only appears when the tmux substrate is on (`SYMMETRIA_IDE_AGENT_TMUX=1`,
which BOTH `~/.local/bin/symmetria-ide{,-stable}` launchers set — so the daily
driver is always affected). The direct-PTY path names chips fine.

**Root cause:** the chip name pipeline is
`harness emits OSC 0/2 title → the terminal HOSTING it fires titleChanged →
qml onTitleChanged → controller.on_agent_title → _clean_agent_title → agentTitles`.
Pre-tmux, the host was KSession, so titles flowed. With the tmux substrate,
claude's OSC title lands on tmux's per-pane `#{pane_title}` (`#T`) and STOPS
there — tmux only re-emits a title to its OUTER client (KSession) when the
session option `set-titles` is `on`, and it defaults to **off**.
`runtime/agent-tmux.conf` never set it, so KSession never saw a title and
`titleChanged` never fired. Nothing in the IDE's Python/QML was wrong.

**Fix (dev, 2026-07-21):** in `runtime/agent-tmux.conf`:
```
set -g set-titles on
set -g set-titles-string "#T"
```
`#T` alone (not tmux's verbose default `"#S:#I:#W - \"#T\" ..."`) because the IDE
wants only the harness's title. Plus a guard in `AppController._clean_agent_title`
(`_HOSTNAME_PLACEHOLDERS`) suppressing the bare hostname, because `#{pane_title}`
DEFAULTS to the hostname (e.g. `arch`) in the sub-second window before the harness
sets its first OSC title — without it a fresh chip flashes the hostname.

**Empirically verified** (not from tmux docs) by attaching a tmux client through a
Python PTY and scanning the bytes tmux writes outward for the title inside an OSC
seq: OFF → nothing reaches the outer client; ON + `#T` → it does. Two more
non-obvious tmux facts confirmed the same way, both load-bearing for a LIVE fix
without an IDE restart:
- `refresh-client` does NOT push the title — tmux emits it only on a pane_title
  *change*, not a plain redraw. Re-asserting the identical title (`select-pane -T`
  with the current value) is deduped and also emits nothing.
- A *fresh attach* under `set-titles on` DOES emit — so relaunching an IDE
  populates all chips cleanly.
- To light already-attached (idle) chips live: force a change via
  `select-pane -t <s> -T "<current_title> "` (trailing space, invisibly stripped
  by `_clean_agent_title`); the harness's next OSC resets it.

**Deployment gotchas:**
- `agent-tmux.conf` is sourced ONCE when the shared tmux server first starts
  (`tmux -f`). A server already running keeps the old options — apply live with
  `tmux -S ~/.vigilia/tmux.sock set-option -g set-titles on \; set-option -g
  set-titles-string "#T"`, else the change only bites after the server dies.
- Fix lives in the `dev` worktree's conf; the `stable` copy updates via the normal
  promotion, not by editing `symmetria-ide-stable/` directly.
- The conf's header says keep it byte-identical with Vigilia's ttyd copy. As of
  2026-07-21 `~/projects/vigilia/config/tmux/tmux.conf` is a DIFFERENT general
  server config (status bar on, plugins), not the byte-identical agent copy — the
  ttyd-specific copy this refers to isn't realized yet, so that alignment is a
  cross-project follow-up, not done here.

See also [tmux_server_env_leak](./tmux_server_env_leak.md) — same tmux substrate,
different failure (env inheritance poisoning session identity).
