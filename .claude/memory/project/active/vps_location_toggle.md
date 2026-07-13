---
name: vps-location-toggle
description: "Local↔VPS location toggle SHIPPED (all 5 phases, 2026-07-12); v1 deferrals + follow-up list"
metadata: 
  node_type: memory
  type: project
  originSessionId: cb49982d-c379-4cc4-ace1-865cf4657921
---

# VPS location toggle — shipped 2026-07-12 (dev, needs stable promotion)

All 5 phases landed on `dev` (`dce2985`→`bbc56c7`): location state +
pairing probe, VPS agents (tmux attach on the Vigilia shared socket,
detach-vs-kill, hub-fed sparkles), SSHFS tree + remote git status/poll +
status bar, full git surface (log/branches/pull-push) remote, remote
terminal pane. Architecture + invariants live in CLAUDE.md "The location
toggle" — read that first; this note carries only the non-code state.

**Why:** the phone (Vigilia app) starts agent sessions on the VPS; the
desk resumes/observes/reviews them with full chrome. Desktop half of
`~/projects/vigilia/docs/convergence-architecture.md`.

**How to apply / follow-ups (agreed v1 deferrals):**
- Reconnect chip state: an ssh drop CLOSES the vps slot with a toast;
  a `disconnected` chip + one-key reattach is the wanted fast-follow.
- Coordination (`wait_for_agent`) refuses vps chips (judge reads local
  transcripts). Cross-location coordination is v2.
- No mosh (plain ssh + ControlMaster); no auto-clone when the repo is
  missing remotely (probe just doesn't pair); tree header above the
  changes panel still shows the LOCAL compact root in vps (cosmetic).
- 1B (move a VPS conversation LOCAL, i.e. sync ~/.claude sessions) was
  explicitly deferred at discourse time — revisit when asked.
- Live interactive verification still owed on the user's screen: toggle
  UX feel, RemoteSessionPicker against phone-started sessions,
  Ctrl+Shift+K confirm, git surface on the remote repo, laptop-sleep
  recovery. Headless E2E + real-VPS smoke all passed.

Registry: `~/.config/symmetria-ide/servers.json` (created 2026-07-12,
entry `vigilia-vps` → dev@64.176.22.138). Related:
[prefer peer over shell coordination](../../feedback/prefer_peer_over_shell_coordination.md).
