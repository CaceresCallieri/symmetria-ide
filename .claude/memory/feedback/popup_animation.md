---
name: popup-animation
description: Every IDE popup gets the File-Manager scale-pop + opacity-fade entrance via Theme.anim tokens
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 0007416b-ca54-4e67-9515-6da4e18d2932
---

All floating popup surfaces in the IDE should use the **same entrance
animation as the File Manager's popups**: a scale-pop (scale from small →
1 with a slight overshoot) paired with an opacity fade. This is a
project-wide standard the user asked to apply going forward, not a
one-off for the spawn menu.

The motion is tokenised in `qml/design/Theme.qml` under `Theme.anim`
(mirrors the FM's `FmTheme.animDuration` / `animCurveStandard` + the
`Easing.OutBack` overshoot that `FuzzyFinderPopup`/`ZoxidePopup` use):

- `Theme.anim.duration` (400) · `Theme.anim.standardCurve` (`[0.2,0,0,1,1,1]`,
  stored as `var` — `list<real>` crashes `qmllint` with exit 255)
- `Theme.anim.popFromScale` (0.1) · `Theme.anim.popOvershoot` (1.5)

Reference implementation: `qml/AgentSpawnMenu.qml` — the panel binds
`scale`/`opacity` off `root.visible` (replays on every open) with two
`Behavior`s (scale → `Easing.OutBack` overshoot; opacity → `BezierSpline`
standardCurve), and the scrim fades its opacity on the same curve. The
exit animates while the root Item is already hidden, so only the pop-IN
is ever seen.

**Why:** keeps the Symmetria toolkit's motion language coherent — the FM,
shell, and IDE share the same "pop in" feel. Tokenising it (vs per-component
durations) means a future motion nudge propagates everywhere, the same
aliasing discipline the palette uses.

**How to apply:** for a new popup, bind the panel's `scale`/`opacity` to its
visible/active state and add the two `Behavior`s with `Theme.anim.*`; fade
the scrim on the standard curve. Don't hand-roll a duration or easing.
Drive scale from `transformOrigin: Item.Center` so it grows from the middle.
See also [UI surface discipline](./ui_surface_discipline.md) — placeholder
styling still applies; this is just the entrance motion.
