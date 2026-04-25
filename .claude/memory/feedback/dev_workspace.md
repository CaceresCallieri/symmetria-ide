---
name: Dev launches target workspace 6
description: Launch Symmetria IDE on Hyprland workspace 6 during dev iteration
type: feedback
originSessionId: 14b51be9-07da-45a2-97ee-3de0c00434a8
---
When launching Symmetria IDE for smoke tests or iteration, open it on Hyprland workspace 6 (not the currently active workspace).

**Why:** The user asked for this explicitly so the dev window doesn't interfere with whatever they're doing on their primary workspace, and so they can check on it from their phone without it being front-and-center. Not a one-time request — treat it as the default for future dev launches of this project.

**How to apply:** Before launching, `hyprctl dispatch workspace 6` to switch, OR use a Hyprland window rule to force the app onto workspace 6 (preferred — the workspace switch is disruptive). Window class for the IDE is likely `symmetria-ide` or similar — verify via `hyprctl clients` after launch if unsure.
