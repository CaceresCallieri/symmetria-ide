---
name: chrome-system-ui-font
description: Chrome resolves system-ui to CaskaydiaCove NFM on this host — not the nested compositor; do not hunt in the IDE
metadata: 
  node_type: memory
  type: reference
  originSessionId: 1dc16f14-6524-437a-9b81-8d0fde68876c
  modified: 2026-07-28T01:29:09.220Z
---

Chrome resolves `system-ui` — and its no-family default font — to
`CaskaydiaCove NFM` (a Nerd Font Mono) on this machine. It looks like a bug in
the IDE's nested-Wayland browser pane. **It is not.** Do not go looking for it
in the compositor.

## What was measured

Four configurations, via CDP `CSS.getPlatformFontsForNode`:

| configuration | `system-ui` | `sans-serif` | no family |
|---|---|---|---|
| nested compositor, headed | **CaskaydiaCove NFM** | Liberation Sans | **CaskaydiaCove NFM** |
| host session, **headed** | **CaskaydiaCove NFM** | Liberation Sans | **CaskaydiaCove NFM** |
| host session, headless | Nimbus Sans | Liberation Sans | Nimbus Sans |
| host session, headed, **bare `google-chrome-stable`** (none of the IDE's flags) | **CaskaydiaCove NFM** | Liberation Sans | **CaskaydiaCove NFM** |

The variable is **headed vs headless**, not nested vs host. A plain Chrome
window on the desktop does the same thing.

## Why an earlier conclusion was wrong

The first control was headless-only — and headless is the one configuration
where the symptom does not appear. That made it exonerate the wrong thing and
blame the nested compositor. **When A/B-ing a browser rendering question, the
control must be headed**, or it is not a control.

## What is NOT the explanation

- No fontconfig rule names Caskaydia: `fc-match` returns Nimbus Sans for
  `system-ui`, `sans-serif`, an empty pattern, and any unresolvable family, and
  Chrome's own default families (`Times New Roman`, `Arial`, `Courier New`)
  resolve to Liberation/Nimbus faces.
- There is no user fontconfig config at all (`~/.config/fontconfig` is absent).
- GTK settings and the xdg-desktop-portal `Settings.Read` both report
  `Cantarell 10`.
- Narrowed further: only the STANDARD/default font is affected — `sans-serif`
  is correct everywhere.

Any fix belongs in the host's font configuration, not in the symmetria-ide
repo.
