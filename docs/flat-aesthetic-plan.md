# Flat aesthetic — IDE + File Manager

Plan for the shared visual simplification of Symmetria IDE and Symmetria File
Manager: remove claymorphism, darken the base, reduce the toggle surface.
Target reference is Zed's chrome — flat fills, hairline separation, small
radii, state expressed by fill and text weight rather than by extrusion.

Branch `feat/flat-aesthetic` in both repos:

- `~/projects/symmetria-ide-wt/flat-aesthetic` (off `dev`)
- `~/projects/symmetria-file-manager-wt/flat-aesthetic` (off `main`)

## Decisions already taken

| Question | Answer |
|---|---|
| Palette scope | IDE + FM get their OWN scheme file. Symmetria Shell keeps its metallic look, untouched. |
| Mechanism | Reuse the existing `color-scheme.json` machinery — a second scheme file, not a new system. |
| Clay removal | Neutralize the depth tokens first (instant flat, no consumer edits), delete the machinery last. |
| Base colour | `#0F0F10` — near black. |

## The two palettes (why this is not one edit)

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
consumed only by `qml/PillSurface.qml` and `qml/PillCard.qml`. 14 files use
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
