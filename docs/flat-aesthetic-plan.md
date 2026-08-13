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

**Phase 2 — the palette. DONE.**
- FM `0ca0ec8`; IDE `4ce260c`. Review fixes in the commits that follow each.
- Base is `#0F0F10` in both apps. Radii down to 8. Hairline border down to 8%.
  The `selected` rung shipped at `#1c1c1f`, not the `#1F1F22` this plan's table
  below proposes — the table is the proposal, the code is the record.
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

### Phase 5 — delete the machinery

Once the flat look is settled: remove `PillSurface.qml`, `PillCard.qml`, the
`Theme.depth` block, and the `RectangularShadow` imports, replacing consumers
with plain `Rectangle` or the new `SegmentedControl`. This is deliberately last
— the components stay as the seam while the aesthetic is still moving.

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
