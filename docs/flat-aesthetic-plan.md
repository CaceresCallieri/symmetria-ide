# Flat aesthetic — IDE + File Manager

Plan for the shared visual simplification of Symmetria IDE and Symmetria File
Manager: remove claymorphism, darken the base, reduce the toggle surface.
Target reference is Zed's chrome — flat fills, hairline separation, small
radii, state expressed by fill and text weight rather than by extrusion.

Branch `feat/flat-aesthetic` in both repos:

- `~/projects/symmetria-ide-wt/flat-aesthetic` (off `dev`)
- `~/projects/symmetria-file-manager-wt/flat-aesthetic` (off `main`)

## Progress log

Append one entry per phase as it lands, newest last. Hashes are on the
`feat/flat-aesthetic` branch of each repo.

**Phase 1 — kill the clay. DONE.**
- FM `78b3bcc` + `0e9d263` + `9be6ee9`; IDE `3a3b55d` + `7e01f51`.
- Every depth alpha is 0 in both repos; offsets and blurs kept, inert.
- Review caught two things worth keeping: a zero-alpha `RectangularShadow`
  still runs its blur shader every frame (both repos now gate it on
  `visible: <token> > 0`), and the consumer count was off by one (13, not 14).
- Prediction that did NOT hold: the active state did not become illegible
  between Phase 1 and Phase 3. `elevated` only ever gated the DEPTH — fill and
  border always painted — so the segmented controls still read correctly. What
  the flat pass actually exposed was SHAPE, not state: capsule radii and the
  16px panel corners, both handled in Phase 2.

**Phase 2 — the palette. DONE, then SUPERSEDED on 2026-08-13.**

> ⚠ **Every rung value in this phase's notes and in the table further down is
> stale.** Phase 2 shipped a single-rung model — one `chrome` colour for bars,
> panels and the content area alike. The 2026-08-13 surface ladder replaced it
> with four rungs (`canvas` < `chrome` < `bar` < `raised`) and moved
> `selected` and `raisedSelected` up with them. **The authoritative values and
> the reasoning live in the ladder note in `qml/design/Theme.qml`, not here.**
> This section is kept for the reasoning trail of how the flat palette was
> arrived at; do not read a colour out of it.

- FM `0ca0ec8`; IDE `4ce260c`. Review fixes in the commits that follow each.
- Base is `#0F0F10` in both apps. Radii down to 8. Hairline border down to 8%.
  The `selected` rung shipped at `#1c1c1f`, not the `#1F1F22` this plan's table
  below proposes — the table is the proposal, the code is the record. (Both
  numbers are now historical; see the banner above.)
- The scheme file mechanism shipped, but INVERTED from what this plan first
  described: the dark palette is the built-in DEFAULT in both apps, and the
  file is an optional override. The shell's `color-scheme.json` was dropped
  from the chain entirely rather than kept as a fallback — the whole point is
  that these two apps no longer follow the shell. New IDE module:
  `src/symmetria_ide/ui_scheme.py`, exposed to QML as the `uiScheme` context
  property; FM repoints its existing `FileWatcher` to the same path.
- Two things this plan did not anticipate, both found by implementing, both
  in the FM:
  - `FmTheme._mattePill()` derives pill lightness from a HARDCODED formula and
    takes only hue/saturation from the palette. Darkening the scheme therefore
    could not darken a single pill; the formula itself had to come down.
  - `overlay.subtle` was doing two jobs at one alpha — zebra row FILL and panel
    BORDER. At 6% over a near-black base the border reads right and the fill
    reads as a stripe, so the fill moved to a new `overlay.zebra` token at 3%.
- Review found, and the fixes commit addressed: no value validation in
  `ui_scheme.py` (a bad hex would BLANK a token rather than fall back, because
  QML lands an unconvertible string on Qt's default, not on the property's);
  three hand-copied mirrors of the text ramp left a rung behind (`diff.contextFg`
  now binds instead of mirroring; the minimap's ramp and its Python twin were
  re-derived); and the IDE's floating surfaces (which-key, cmdline, completion
  popup) were still painting `bg.chrome`, i.e. the exact colour of the chrome
  they float over.
- Carried into Phase 3, deliberately:
  - `bg.selected` (#1c1c1f) sits only ~6 lightness units above `bg.raised`
    (#161618), so a selected row inside a modal or picker reads about half as
    strongly as the same token does on chrome. Wants a `bg.raisedSelected`
    rung — which is state grammar, so it belongs with the segmented control.
  - The word "clay" appears ~50 times across 15 IDE QML files. Only the one
    comment that stated a WRONG fill token was fixed; the terminology sweep
    happens in Phase 5, when the components it names are deleted.

**Phase 3 — a flat state grammar, extracted once. DONE (IDE only).**
- IDE `4bbc6c3` + `c68b90e`. The FM's `TabBar` was NOT converted — see below.
- New `qml/SegmentedControl.qml` replaces four hand-rolled copies: the location
  toggle and surface switcher in `AgentTopBar`, the scope switcher in
  `GitStatusPanel`, and the tab header in `GitHistoryView`. Net −101 lines, and
  the lint baseline fell 1653 → 1598 because four copies of the same
  unresolvable-`Theme` warnings collapsed into one file's worth.
- State grammar without depth: active = filled + hairline border + `text.strong`
  bold; inactive = transparent fill AND border + `text.dim` medium. All three
  move together, because with the shadow gone no single one carries the signal.
- Segment corners came off the capsule (`height / 2`) to `radius.md`. This is
  the shape observation from Phase 1 finally applied: a generous corner was
  what made an extruded chip read as physical, and on a flat fill it reads as a
  dated pill.
- `bg.raisedSelected` added and wired to the seven selection surfaces that sit
  INSIDE a PillCard or a raised popup (both session pickers, ConfirmDialog's
  buttons, the completion popup, and the three git detail cards). This closes
  the contrast finding Phase 2 deferred. List rows in the git columns keep
  plain `bg.selected` — those columns paint chrome, not raised.
- Deliberately NOT done: the FM's own `TabBar` still hand-rolls the same
  control. It lives in the shared `Symmetria.FileManager.UI` module, so
  converting it means either duplicating `SegmentedControl` into that module or
  making the FM depend on the IDE's QML — a toolkit-layering decision, not a
  styling one. Left for a deliberate call rather than settled by momentum.

**Phase 4 — reduce the toggles. TWO OF FIVE ROWS DONE (2026-08-13).**

The product conversation happened. The user rejected the dropdown framing for
binary controls and approved two changes; the table below is the inventory it
was decided from, kept for the reasoning trail.

Decided and shipped:

1. **The changes-scope switcher hides when it is inert.** `visible:
   focusedAgentSlot > 0 || scope === "agent"`. The second clause is the way
   back: the pool can empty while the scope is still "agent", and a control
   that vanished in its own non-default state would strand the panel on an
   empty agent view. Both chords stay unconditional — flipping to agent scope
   with no agent lands on the "No focused agent" empty text, which answers the
   question rather than blocking it.
2. **The surface switcher draws one icon per surface and names only the
   active one.** `Theme.glyph.surface.*`, escapes not literals. Chosen against
   a rendered comparison of three candidate sets at the real 11px on the real
   background, not from a list of codepoint names: FILLED marks (nf-md-robot,
   the boxed terminal glyphs) carry more ink than the flat `bg.selected` fill
   behind the ACTIVE segment, so an inactive surface outweighed the current
   one and inverted the control's signal. Everything here is monoline for that
   reason. Cost accepted and commented in `SegmentedControl.qml`: the active
   segment is wider, so switching re-flows the glyphs after it — eased and
   clipped into a reveal, not removed.
3. Labels capitalised across all four switchers (user preference).

4. **`local | vps` takes the same icon + active-only-label treatment**
   (decided after seeing 1–3 on screen; reverses this control's first cut).
   `Theme.glyph.location.*`: a monitor outline for local, a stacked rack for
   vps — chosen for SHAPE contrast at 11px, which is why `cod-vm_connect`
   lost (a monitor with a badge, near-identical to the local monitor). It
   stays SEGMENTED, not a cycling label: the axis grows past two machines,
   where a dropdown is right and a click-to-cycle label cannot go at all.

   The first cut kept both words on the argument that a wrong LOCATION is
   discovered later than a wrong surface — you find out when you run
   something. What overturned it: the active half is still spelled out, so
   the state you are IN is never the one inferred from a glyph.

   ⚠ Codicon codepoints do not follow their names in any guessable order.
   **Five of the seven** taken from a chart drew a different picture than
   their name promised: `cod-server` is `U+EB50`, not the plausible-looking
   `U+EB9F` (an unrelated struck-through mark); `cod-device_desktop` renders
   a circuit board; `cod-home` renders a plus sign. Read them out of the
   font BY GLYPH NAME (fontTools `getBestCmap()`); a wrong codepoint renders
   silently as a different icon, never as a missing glyph, so nothing warns
   you — the only way to catch it is to render the candidates and look.

   Codepoints are written here as `U+XXXX`, never as the glyph itself. This
   paragraph first embedded the two characters raw, which is exactly the
   failure it warns about one sentence earlier: invisible in review, tofu
   without the exact font, and silently destroyable by any edit pipeline
   that does not preserve non-ASCII bytes.

Decided and deliberately NOT changed:

- **The git tab header keeps every label.** Each carries live data — pending
  count, checked-out ref, open-PR count — whose entire purpose is to be
  readable WITHOUT switching tabs. Active-only labelling would trade the data
  for width.

Not from this phase, but found by it: **Phase 2 broke
`test_pr_tab_qml.py::test_prs_mode_in_cycle_and_header` and three seal runs
did not catch it.** The assertion pinned an exact source line (`{ mode: "prs",
label: "PRs" }`) that the SegmentedControl extraction reformatted. The seal's
reviewers read diffs; none of them runs the suite. Fixed, and rewritten to
assert the segment's existence rather than its punctuation. **Run the suite
once per phase from here on — a find-only reviewer is not a substitute.**

The inventory the decisions were taken from:

| Control | Where | Always visible? | Recommendation |
|---|---|---|---|
| `local` `vps` | AgentTopBar, right | Only on paired projects | **Keep.** The one control whose visibility is already earned — it hides itself on the projects where it means nothing. |
| `terminal` `editor` `agents` `git` | AgentTopBar, left | Always | **Keep, but it is the biggest single block of chrome text in the IDE** — four words, permanently. The alternative is showing only the ACTIVE surface and revealing the rest on hover, which trades a glance for a motion. Worth trying once the palette settles; not obviously better. |
| `all` `this agent` | GitStatusPanel header | Whenever the panel shows | **Reduce.** This is the clearest candidate: a two-segment control in the narrowest column in the IDE, duplicating a global chord (`Ctrl+Shift+D`) and an in-panel key (`a`). Collapsing it to a single label that NAMES the current scope and toggles on click halves its width and removes a segmented control from a panel that has no room for one. |
| `changes` `history` `PRs` | GitHistoryView tab header | On the git surface | **Keep.** This is the surface's primary navigation, and each label carries a live count that is worth reading. |
| browser MCP | McpMenu popup | Only inside the popup | **Keep.** Costs nothing when closed. |

So the concrete proposal is ONE change (the changes-panel scope) plus one
experiment (collapsing the surface switcher). Everything else earns its place.

**Phase 4.5 — round the canvas. DONE.**
- `qml/CanvasCorners.qml` (new), mounted in `Main.qml`'s `mainContent`;
  `Theme.radius.canvas` = 24, Hyprland's `decoration:rounding`.
- The IDE's windows are not fullscreen, so Hyprland ALREADY rounds their outer
  corners at 24. That is what settled which rectangle was meant: the window
  corner was never square, the canvas corner was. The interior corner now
  echoes the one the compositor draws.
- Painted, not clipped. `clip` in Qt Quick is a rectangular scissor and cannot
  follow a radius; a `layer` + mask would route every terminal frame AND the
  nested compositor's surface through an FBO, which is the one place in this
  app where frame delivery is already fragile. So four Bézier wedges in the
  surrounding colour, drawn on top.
- Verified by measurement, not by eye: the arc's profile at the top-left corner
  runs x=24 at the first canvas row down to x=0 twenty-four rows later, which
  is a circle. The soft 7px ramp along the first row is the arc being TANGENT
  to that edge, not antialiasing slop.
- Two corrections fell out of it. `pragma ComponentBehavior: Bound` removes
  the nine `unqualified` findings in `SegmentedControl.qml` that a comment in
  that file called unfixable — the claim was wrong and the comment is gone.
  And a comment line whose first word after `//` is the linter's name is
  parsed as a lint directive; writing one turned a paragraph into twelve
  `invalid-lint-directive` findings.

**Phase 5 — delete the machinery. DELIBERATELY NOT STARTED.**

Held on purpose, per this plan's own reasoning: `PillSurface` / `PillCard` /
`Theme.depth` are the single place where the whole look retunes in one edit,
and the user has not seen the result yet. Deleting the seam before the first
round of adjustments removes exactly the lever those adjustments need. It is
also the right moment for the `clay` terminology sweep (~50 references across
15 IDE QML files), since the components those comments name die in the same
change. Start it after the aesthetic is settled, not before.

## Decisions already taken

| Question | Answer |
|---|---|
| Palette scope | IDE + FM get their OWN scheme file. Symmetria Shell keeps its metallic look, untouched. |
| Mechanism | Reuse the existing `color-scheme.json` machinery — a second scheme file, not a new system. |
| Clay removal | Neutralize the depth tokens first (instant flat, no consumer edits), delete the machinery last. |
| Base colour | `#0F0F10` — near black. |

## The two palettes (why this is not one edit)

> **SUPERSEDED in part by the Phase 2 log entry above.** This section describes
> the design as first proposed. What shipped drops Symmetria Shell's
> `color-scheme.json` from the resolution chain ENTIRELY — there are two steps,
> not the three listed below — and makes the dark palette the built-in default
> rather than something the file supplies. Read it for the reasoning; do not
> implement from it.

The IDE renders chrome from two independent palettes:

1. **`qml/design/Theme.qml`** (IDE-owned, hardcoded) — status bar, agent top
   bar, which-key, cmdline, popups, terminal ANSI colours.
2. **`FmTheme.qml`** (File Manager module, installed at
   `/usr/lib/qt6/qml/Symmetria/FileManager/UI/`) — the file tree, the git
   status badges, the Active Changes panel rows. `FmTheme` loads its palette
   from `~/.config/quickshell/symmetria/config/color-scheme.json`, which is
   the **Shell's** file. The Shell's palette is frozen and static (see the
   shell repo's `project_frozen_dark_theme.md`), not wallpaper-generated.

Editing the Shell's file would darken the Shell's bar, popups and notification
centre along with the IDE and FM. That is out of scope: the Shell's current
metallic direction stays as it is. So `FmTheme` needs a **scheme path with a
fallback chain**, letting IDE and FM read a toolkit-owned file while a
standalone FM on a plain Symmetria desktop still falls back to the Shell's.

Proposed resolution order in `FmTheme._configDir`:

1. `$SYMMETRIA_UI_SCHEME` (explicit override — used by the worktree runs)
2. `~/.config/symmetria/ui/color-scheme.json` (the toolkit file this plan adds)
3. `~/.config/quickshell/symmetria/config/color-scheme.json` (today's path)

`Theme.qml` reads the same file so the IDE's chrome and the FM's tree stop
drifting. `Theme.qml` currently has no loader at all — its colours are QML
literals — so this phase adds one. Read it through Python (`app.py` already
builds context properties) rather than a QML `FileView`, because the IDE has no
QuickShell `FileWatcher` available.

## Where the claymorphism lives

Contained, in both repos. No module consumer defines its own shadow.

**IDE** — `Theme.depth` (the `chip` and `card` presets plus `highlightAlpha`),
consumed only by `qml/PillSurface.qml` and `qml/PillCard.qml`. 13 files use
those two components: `AgentTopBar`, `GitStatusPanel`, `ModalOverlay`,
`ConfirmDialog`, `Toast`, `UsageDetailPopup`, and 7 files under `qml/githistory/`.

**FM** — no `FmTheme.depth` block exists. The constants are the property
defaults inside `components/PillSurface.qml`, overridden in
`components/PillCard.qml`. 12 files under `modules/filemanager/` consume them.

Consequence: the IDE's kill switch is one token block; the FM's is two files.
Neither needs consumer edits to go flat.

## Phases

### Phase 0 — baseline

- Capture before-screenshots of every affected surface through the headless
  harness (`SYMMETRIA_IDE_SCREENSHOT=<path>`, see `docs/dev-workflow.md`). It
  launches an ephemeral instance and exits, so it never touches the user's
  visible session.
- Settle the FM iteration loop: `./run.sh` in the FM worktree builds and
  launches the standalone host, which reads panel QML from its own source
  tree. For the FM module *inside the IDE*, launch the IDE worktree with
  `SYMMETRIA_IDE_FM_QML_PATH=<fm-worktree>/qml`.

### Phase 1 — kill the clay (both repos, no consumer edits)

- IDE: zero every alpha in `Theme.depth` (`darkAlpha`, `lightAlpha`,
  `highlightAlpha`, `innerShadowAlpha`) for both presets.
- FM: same, in `PillSurface.qml`'s defaults and `PillCard.qml`'s overrides.
- Screenshot and compare. Expect the active-state cue to become *weak*, not
  absent — every segmented control today leans on `elevated` to say "this one
  is current". Phase 3 replaces that cue; this phase is allowed to look flat
  and under-differentiated in between.

### Phase 2 — the palette

Add the toolkit scheme file and the fallback chain, then re-point both themes
at it. Starting values, to be tuned live:

| Token | Now (IDE / FM) | Proposed |
|---|---|---|
| base / window | — / `#18191a` | `#0F0F10` |
| chrome bars, panels | `#201F1F` | `#0F0F10` — separated by a hairline, not by fill |
| raised (popups, modals) | `#201F1F` | `#161618` |
| selected / active | `#282728` | `#1F1F22` |
| hairline border | white 12% | white 8% |
| text dim / normal / strong | `#7a7a7a` / `#b0b0b0` / `#e0e0e0` | `#6E6E73` / `#A8A8AE` / `#D4D4D8` |

Radii come down with the fills — clay needed generous corners, flat does not:
`Theme.radius.lg` 16 → 8, and `FmTheme.rounding.lg` 16 → 8 to match. The
`radius: height / 2` pill capsules on segments are retired in Phase 3.

Accent colours (`mode.*`, `usage.*`, `diff.*`, git badge colours) are **out of
scope**. They carry semantics, the user is happy with them, and a darker base
raises their contrast for free.

### Phase 3 — a flat state grammar, extracted once

Today four separate places hand-roll the same clay two-segment switcher:
`AgentTopBar`'s location toggle, `AgentTopBar`'s surface switcher,
`GitStatusPanel`'s scope switcher, and `GitHistoryView`'s tab header — plus the
FM's own `TabBar`. That duplication is why the toggles read as "too many": five
controls that look identical but sit in unrelated contexts.

- Extract one `SegmentedControl.qml` in the IDE and use it at all four sites.
- Express "current" without depth: filled `bg.selected` + `text.strong`, versus
  transparent + `text.dim` for the rest. No border on inactive segments.
- Decide whether the FM's `TabBar` adopts the same component or stays separate
  (it lives in the shared module, so the IDE would inherit it either way).

### Phase 4 — reduce the toggles

Inventory first, then cut. Known toggle-shaped controls in the IDE:

| Control | Where | Keyboard |
|---|---|---|
| local / vps | AgentTopBar, right rail | `Ctrl+Shift+U` |
| terminal / editor / agents | AgentTopBar, left edge | `Ctrl+Shift+E`, `Ctrl+Shift+T`, `Ctrl+Shift+A` |
| all / this agent | GitStatusPanel header | `Ctrl+Shift+D`, `a` in-panel |
| changes / log / branches / PRs | GitHistoryView tab header | `Tab` |
| per-project browser MCP | McpMenu popup | `Ctrl+Shift+M` → `w` |

Every one has a keyboard binding, which is the IDE's non-negotiable. So the
question per control is whether the *visible* affordance earns its space, not
whether the function survives. Candidates to discuss: the location toggle
(hidden already on unpaired projects), and whether the surface switcher needs
labels or reduces to the active surface's name alone.

This phase is a product conversation, not a mechanical one. Do not start it
before Phases 1–3 are visible on screen.

### Phase 4.5 — round the canvas

The central surface takes Hyprland's own corner (24px), so the interior of the
window echoes the window's own outline instead of meeting it at a hard right
angle.

Implementation is `qml/CanvasCorners.qml` — four wedge paths drawn OVER the
panes, because none of the things that live on this surface can be given a
radius: QMLTermWidget paints Konsole's own opaque rectangle, the browser pane
is a nested Wayland compositor hosting real Chrome, and the FM module paints
its own. Read that file before changing the approach; it records why `clip`
and `layer` are both wrong here.

Open on a real seat: whether 24 is right for an INTERIOR corner. It is the
compositor's value, chosen so the two corners agree, but Hyprland applies it to
a whole window against a wallpaper while this one sits against a chrome bar four
lightness units away. `Theme.radius.canvas` is the single lever.

### Phase 5 — delete the machinery

Once the flat look is settled: remove `PillSurface.qml`, `PillCard.qml`, the
`Theme.depth` block, and the `RectangularShadow` imports, replacing consumers
with plain `Rectangle` or the new `SegmentedControl`. This is deliberately last
— the components stay as the seam while the aesthetic is still moving.

## Deferred past this branch

### Delete the per-agent change filter entirely (decided 2026-08-13)

**Not in this branch.** Sequence: finish the aesthetic work, merge it to `dev`,
then remove the feature there. It is recorded here because the decision was
taken during this branch's Phase 4 conversation, and because Phase 4 just spent
work on the control that fronts it.

**The decision.** Remove the "all | this agent" changes scope and the whole
provenance machinery under it — not just the switcher.

**The user's reasoning**, which is the part worth keeping: attributing a working-
tree change to a specific agent is hard to infer *reliably*, and the complexity
of trying is not worth its maintenance. The problem it addresses is better
solved upstream by git hygiene — one worktree or one branch per agent — which
makes the attribution structural instead of inferred. The v2 Bash-attribution
caveats already in CLAUDE.md are evidence for this rather than against it: a
snapshot diff around a Bash command races the reporter, so fast writes are
missed and the feature is honestly approximate at its core.

**⚠ The one thing that must NOT be deleted with it.** `_on_agent_hook` writes
`work_root` and `touched` from the SAME write-tool branch, a few lines apart:

  * `touched` — the provenance set. This feature. Goes.
  * `work_root` — the worktree follow, which re-roots the tree and every git
    surface onto the focused agent's live worktree. **Stays**, and is precisely
    the mechanism this decision leans harder on.

Deleting the write-tool branch wholesale would remove the replacement along
with the thing being replaced. The cut is inside that branch, not around it.

**Rough surface of the removal** (verify before trusting; this is a map, not an
inventory): `GitStatusPanel.qml`'s scope switcher, agent-scope sections and
foreign-repo Repeater; `GitChangeSectionHeader.qml` and Main.qml's
`gitForeignProviderAdapter` if nothing else claims them; `agent_bash_attribution.py`
whole; `git_controller._fold_agent_changes` / `changed_path_set_for`;
AppController's `focusedAgentChanges*` / `focusedAgentForeignChanges` properties,
`_partition_foreign_touched`, `_refresh_foreign_changes`, the `bash-attrib` and
`foreign-status` thread pools and their `_foreign_probe_inflight` bookkeeping;
the `Ctrl+Shift+D` chord and the panel's `a` key; `tests/test_agent_changes_filter.py`,
`test_agent_changes_e2e.py`, `test_agent_foreign_changes.py`, and the two
scope-switcher tests in `test_chrome_toggle_reduction_qml.py`; and the
"Per-agent change filter" paragraph in CLAUDE.md.

Phase 4's `visible:` gate on the switcher is not wasted work in the meantime —
it keeps the control out of sight on every project without a focused agent
until the removal lands.

## Open questions

- **Popup motion.** `Theme.anim` currently defines a 400 ms scale-pop with an
  `Easing.OutBack` overshoot (`popFromScale: 0.1`, `popOvershoot: 1.5`), shared
  with the FM. A bouncy overshoot is not a flat-minimal idiom; Zed's popovers
  fade in around 100–150 ms with no scale. Changing it contradicts a recorded
  user preference (`.claude/memory/feedback/popup_animation.md`), so it needs
  an explicit call before it is touched.
- **The surfaces the user likes** — the status bar and the agent/usage visuals —
  keep their content and layout. But `UsageDetailPopup` is a `PillCard`, so it
  *will* lose its clay in Phase 1. Confirm the flat version still reads well
  before moving on.
- **`symmetria-agents-ui`** (the shared sparkle/chip module, `AgentChip.qml`)
  carries no clay of its own, so it needs no change. If the chips end up
  looking wrong against the darker base, that is a third repo to branch.
- **Promotion.** The FM module is installed system-wide, so the FM half only
  reaches the daily-driver IDE after `install.sh` runs. Sequence the two
  promotions together, or the stable IDE gets a dark chrome around a
  light-grey tree.
