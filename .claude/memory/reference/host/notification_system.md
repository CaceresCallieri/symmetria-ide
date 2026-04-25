---
name: Notification system is Symmetria Shell
description: Symmetria Shell (QuickShell-based) is the notification system; do not invoke swaync-client or makoctl
type: reference
originSessionId: 14b51be9-07da-45a2-97ee-3de0c00434a8
---
The notification daemon on this system is Symmetria Shell (QuickShell-based), not swaync and not mako.

**Why:** User explicitly corrected a tool invocation that called `swaync-client -C` and `makoctl dismiss --all`. Those commands do nothing here. Symmetria Shell is the whole desktop-shell layer described in CLAUDE.md — "replaces traditional tools like Rofi, Waybar, etc."

**How to apply:** Never run swaync or mako CLIs. If notifications need to be dismissed or controlled during testing, ask the user for the correct command or find it in the Symmetria Shell source (QuickShell-based, likely lives under `~/.dotfiles/.config/quickshell/symmetria/` or similar). Do not guess; ask.
