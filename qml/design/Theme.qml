// Symmetria IDE design tokens — single source of truth for chrome
// typography, palette, spacing, and sizing. Every chrome component
// (StatusBar, CommandLine, WhichKeyOverlay, and future panes like the
// agent pane and file picker) binds against this singleton so the
// visual language stays coherent without cross-file literal drift.
//
// Resolution: `pragma Singleton` + `qmldir` sibling (`qml/design/qmldir`)
// registers this as the module's single Theme instance. Siblings import
// it via `import "design"` — Qt auto-discovers the qmldir inside that
// directory relative to the importing file's location.
//
// Font cascade note: `family` binds to the `editorFontFamily` context
// property exposed from Python (`app.py::_build_engine`). Python runs
// the actual `QFontDatabase` cascade via `editor_font.default_font()` and
// exposes the primary resolved family as a single QString — QML's
// `font.family` is single-valued, and `font.families` (plural) is not
// on the QML font value type in Qt 6.11 (see CLAUDE.md gotcha #23).
// Binding Theme.font.family means every chrome component picks up the
// grid's resolved primary family without drift.
//
// Color provenance:
//   - Chrome bg/border: the flat-aesthetic near-black set, defined as literals
//     below and overridable through the shared toolkit scheme file that
//     `src/symmetria_ide/ui_scheme.py` resolves. These NO LONGER derive from
//     Symmetria Shell's `mattePill()` — the shell took its own metallic
//     direction that the IDE and the File Manager deliberately do not follow.
//     Rationale + the full phase plan: `docs/flat-aesthetic-plan.md`.
//   - Mode badges + accents: user's wine_theme nvim colorscheme, from
//     `~/.config/nvim/lua/jc/plugins/theme/wine_theme/lua/lush_theme/
//     wine_theme.lua`. Badge label color is `wine_theme.bg_primary`
//     so the badge reads as painted in the editor's own background.
//
// Typography scale: fonts are deliberately ~20% smaller than a
// terminal-style chrome would default to — the IDE's identity favours
// a refined, quiet chrome over a busy tui. Adjust the rungs below to
// scale the whole UI uniformly.

pragma Singleton

import QtQuick

QtObject {
    id: theme

    // ─── Typography ──────────────────────────────────────────────
    readonly property QtObject font: QtObject {
        // Primary family resolved by Python — see header note.
        readonly property string family: editorFontFamily

        // Size rungs. Current usage:
        //   xs — mode badge labels, ultra-compact affordances.
        //   sm — body text: status fields, which-key entries, popup rows.
        //   md — cmdline input (the one place chrome text leads visually).
        //   lg — reserved for titles in future panes.
        readonly property QtObject size: QtObject {
            readonly property int xs: 9
            readonly property int sm: 10
            readonly property int md: 11
            readonly property int lg: 13
        }

        readonly property QtObject weight: QtObject {
            readonly property int normal: Font.Normal
            readonly property int medium: Font.Medium
            readonly property int bold: Font.Bold
        }
    }

    // ─── Icon glyphs ─────────────────────────────────────────────
    // Nerd Font glyphs shared across chrome surfaces. Written as \u
    // ESCAPES, never literal private-use-area characters: a literal PUA
    // glyph once arrived through an edit pipeline as an EMPTY string and
    // silently rendered nothing (zero-width Text, no warning). The escape
    // is diff-visible and encoding-proof. Bind via Theme.glyph.* so each
    // codepoint lives in exactly one place. Render with font.family:
    // editorFontFamily (the chrome UI font may lack icon glyphs).
    readonly property QtObject glyph: QtObject {
        // nf-oct-git_branch — the worktree mark (tree header + agent chips).
        readonly property string worktree: "\uf418"

        // nf-fa-globe — the browser-ownership mark on an agent's thread-rail
        // row. Late to this table, and that is the whole point: it lived as a
        // LITERAL private-use character inside AgentThreadRail.qml, and had
        // already been flattened to an EMPTY STRING by the time anyone looked
        // — so the globe rendered as nothing on every row that owned a browser
        // window, with no warning, exactly as the block comment above warns.
        // Verified at the byte level (`text: ""` is `22 22`) in the committed
        // file on 2026-08-19, and the codepoint verified against the installed
        // CaskaydiaCove Nerd Font, whose cmap maps U+F0AC to `fa-globe`.
        // Second glyph lost to that pipeline: the argument for the TOKEN, not
        // only for the escape.
        readonly property string browser: "\uf0ac"

        // nf-cod-list_tree — the side panel's FILES tab. Codicon, monoline,
        // the same family and stroke weight as the surface marks below,
        // because the two controls sit one above the other in the same column
        // and a mixed family reads as two unrelated widgets.
        //
        // The side panel's other tab (CHANGES) deliberately takes NO token of
        // its own: it binds `glyph.surface.git` (nf-cod-source_control), the
        // same mark the central git surface carries. One codepoint, one
        // meaning — a second "changes" glyph would claim the two surfaces show
        // different things, and they show the same changeset at two scopes.
        readonly property string fileTree: "\ueb86"

        // The four central surfaces, as the AgentTopBar switcher draws them.
        // ONE icon family on purpose (Codicons, VS Code's set) plus a bare
        // shell prompt: every mark here is MONOLINE, drawn with a single
        // stroke weight. Filled candidates were rejected on measurement, not
        // taste: a solid mark (nf-md-robot for agents, the boxed terminal
        // variants) carries more ink than the flat `bg.selected` fill behind
        // the ACTIVE segment, so an inactive surface pulled the eye harder
        // than the current one and inverted the control's whole signal.
        // Keep any replacement monoline for the same reason.
        readonly property QtObject surface: QtObject {
            // nf-fa-terminal: a bare `>_`, no enclosing box. The boxed
            // variants (nf-cod-terminal, nf-oct-terminal) read heavier than
            // the pencil and sparkle beside them.
            readonly property string terminal: "\uf120"
            // nf-cod-edit: pencil.
            readonly property string editor: "\uea73"
            // nf-cod-sparkle: the same sparkle idiom the agent chips use for
            // activity, so one mark means "agent" across the whole IDE.
            readonly property string agent: "\uec10"
            // nf-cod-source_control. Deliberately NOT `worktree` (\uf418)
            // above: that glyph is a per-agent STATE badge ("this agent lives
            // in a worktree"), and one codepoint carrying both a state and a
            // destination is how a badge stops being readable as a badge.
            readonly property string git: "\uea68"
        }

        // The execution LOCATION (AgentTopBar's Local/VPS control). Same
        // monoline Codicon family and the same active-only-label treatment as
        // the surfaces above.
        //
        // Codicon codepoints do NOT follow their names in any guessable order:
        // `cod-server` is \ueb50, while \ueb9f -- a plausible-looking guess --
        // is an unrelated struck-through mark, and `cod-device_desktop` draws
        // a circuit board. Read the codepoint out of the font BY GLYPH NAME
        // (fontTools `getBestCmap()`) instead of taking one from a chart: a
        // wrong guess here renders silently as the wrong picture, never as a
        // missing glyph, so nothing warns you.
        readonly property QtObject location: QtObject {
            // nf-cod-vm: a monitor outline. Reads as "this machine".
            readonly property string local: "\uea7a"
            // nf-cod-server: a stacked rack. Distinct in SHAPE from the
            // monitor, which is what makes the pair readable at 11px. The
            // rejected alternative (nf-cod-vm_connect, \ueba9) was a monitor
            // with a small badge, so the two states looked nearly identical.
            readonly property string vps: "\ueb50"
        }

        // NOTE: the subscription-usage panel deliberately takes NO glyph from
        // here. Nerd-Font marks (nf-fa-asterisk / nf-fa-code) were the first
        // cut and read as generic symbols rather than as Anthropic and OpenAI;
        // it renders the real brand SVGs from `qml/assets/` instead, via
        // `UsageFormat.providerIcon` \u2014 the same Claude asset the spawn menu
        // uses, so one mark means one thing across the IDE.
    }

    // ─── Optional shared palette override ────────────────────────
    // `uiScheme` is a context property set in `app.py::_build_engine` from
    // `ui_scheme.py`, which reads an OPTIONAL toolkit file at
    // `~/.config/symmetria/ui/color-scheme.json`. The File Manager's FmTheme
    // reads the same file, so one edit re-skins the IDE's chrome and the FM's
    // panels together. The file is normally ABSENT — the literals below are the
    // real default; the scheme only overrides what it declares.
    //
    // Guarded with `typeof` so this file still loads in a plain qmlscene or
    // static-analysis context, where no context property exists.
    //
    // ⚠ Do NOT write the linter's name as a bare word in a .qml comment: it
    // treats ANY comment containing that token as a lint directive and turns
    // every following word into an unknown category. The `list<real>` note
    // further down already costs the baseline 17 findings that way.
    //
    // Only the neutral SURFACE and TEXT rungs read from the scheme. Accents
    // (`mode.*`, `usage.*`, `diff.*`, `agent.*`) stay literal on purpose: they
    // carry semantics tied to the wine_theme colorscheme, and a palette swap
    // should re-skin the chrome without recolouring what "error" or "insert
    // mode" means. `border.hairline` is literal too, for a different reason
    // given at that token: it is an alpha-white overlay, and M3 has no role
    // for one.
    readonly property var _scheme: (typeof uiScheme !== "undefined" && uiScheme) ? uiScheme : ({})

    // Look up a Material-3 role name, falling back to a literal.
    // NOT a live binding: a function call in a binding does not re-evaluate
    // (gotcha #3). Correct here because `uiScheme` is a load-once snapshot set
    // before `engine.load()` and never mutated. Do NOT extend this to anything
    // that changes at runtime.
    function _c(role: string, fallback: string): string {
        const value = theme._scheme[role];
        return (typeof value === "string" && value.length > 0) ? value : fallback;
    }

    // ─── Color ───────────────────────────────────────────────────
    readonly property QtObject color: QtObject {
        // Backgrounds. Near-black base with small lightness steps between
        // rungs: with the clay depth gone, the step in FILL plus the hairline
        // border below are the only separation cues left, so the rungs are
        // deliberately close together and the border does the edge work.
        //
        // ─── THE SURFACE LADDER (2026-08-13) ─────────────────────────
        // Lightness increases with DISTANCE FROM THE CONTENT, not with
        // "height". The content area is the darkest thing in the window and
        // every layer of chrome around it is a step lighter:
        //
        //     canvas  #0f0f0f   editor, terminal, agents, git, FM
        //     chrome  #131313   framed cards and detail views ON the canvas
        //     bar     #171717   top bar, status bar, side panel, window root
        //     raised  #1e1e1e   popups and modals, which must clear `bar`
        //
        // ⚠ THE RAMP IS ACHROMATIC, AND THAT IS THE POINT (2026-08-19). Every
        // rung is r == g == b. It was NOT: each carried a cool cast that grew
        // with the rung (blue-minus-red ran +1, +3, +3, +4, +5, +6), which the
        // ladder documented as a trend to keep new rungs "on trend" with. On a
        // real seat the accumulated result read as blue-grey rather than as
        // neutral, most visibly on the largest flat areas — the thread rail and
        // the side panel. The reference is T3 Code, whose chrome is pure
        // neutral and carries its identity in ONE saturated accent instead.
        //
        // The neutralisation dropped the blue channel to meet r/g and left r/g
        // untouched, so every rung's ORDERING and spacing survive exactly; only
        // the hue is gone. Luminance falls by under half a point per rung (blue
        // contributes ~7% of it), which is below the threshold at which any of
        // the contrast decisions recorded here would change.
        //
        // Do NOT re-introduce a per-rung cool skew "for warmth balance" or to
        // put a new rung on the old trend. If a future rung is needed, give it
        // r == g == b like its neighbours. The warm accents below are where
        // this palette carries colour; the neutrals are deliberately inert.
        //
        // ⚠ The SIDE PANEL is on `bar`, not on `chrome`, and that is a
        // correction rather than an inconsistency (2026-08-13, from the first
        // look on a real seat). It sat on `chrome` on the principle that a
        // rung follows what a surface BELONGS to — the panel is a panel — and
        // it looked wrong: the panel column meets both bars along its top and
        // bottom edges, so a one-rung step there draws a visible border out of
        // nothing at exactly the seam that should read as continuous. The
        // full-width hairline had been hiding it. What survives on `chrome` is
        // the narrower and more defensible case: a panel that floats ON the
        // canvas and needs to lift off it (the git surface's detail views,
        // PillSurface's default). Two chrome surfaces that TOUCH share a rung;
        // a rung step is for chrome against content.
        //
        // This is Zed's convention, and it is Zed's rather than a guess:
        // measured across all six of its dark themes (One Dark, Ayu Dark,
        // Ayu Mirage, Gruvbox Dark/Hard/Soft), `editor.background` is the
        // darkest surface in every one, `panel.background` sits above it and
        // `background`/`title_bar`/`status_bar` above that. The same six also
        // set `terminal.background` == `editor.background` WITHOUT EXCEPTION,
        // which is why `canvas` is also what the QMLTermWidget panes paint
        // (via the fork's Symmetria colorscheme — keep the two in step, and
        // remember the fork needs `makepkg -sif` for a scheme change to reach
        // a launcher-started IDE).
        //
        // ⚠ The intuition this replaced was that the content area should be
        // LIGHTER than the chrome, like a lit page. That is a real convention
        // — JetBrains and Xcode do it — but it is the opposite of Zed's, and
        // Zed is what this project's aesthetic targets and may adopt as its
        // editor. Do not "fix" the direction without re-measuring.
        readonly property QtObject bg: QtObject {
            // The CENTRAL surface's ground: editor, terminal, agents, git and
            // FM. The DARKEST rung — see the ladder note above.
            //
            // `surfaceContainerLowest`, NOT the more obvious `surfaceDim`:
            // Material 3 gives `surfaceDim` and `surface` the SAME value in a
            // dark scheme (#141218 in the reference palette), so mapping the
            // content rung onto it would collapse `canvas` and `chrome` into
            // one colour for any user scheme that supplies a real M3 role set
            // — silently erasing the distinction this ladder exists to make.
            // `surfaceContainerLowest` (#0F0D13) is the one role that sits
            // BELOW `surface`, which keeps the six rungs strictly increasing.
            readonly property color canvas: theme._c("surfaceContainerLowest", "#0f0f0f")
            // Framed cards and detail views that float ON the canvas — the git
            // surface's columns, PillSurface's default fill. One step out from
            // the content, which is what lifts them off it. NOT the side panel
            // (see the correction in the ladder note above).
            readonly property color chrome: theme._c("surface", "#131313")
            // The two chrome BARS (AgentTopBar, StatusBar), the SIDE PANEL
            // they bracket, and the window root behind everything. The
            // outermost rung, so the chrome reads as one frame around the
            // content rather than as parts of it.
            readonly property color bar: theme._c("surfaceContainerLow", "#171717")
            // Popups, modals and any surface that must read as sitting ABOVE
            // the chrome. Replaces the drop shadow that used to say so. Note
            // it must clear `bar`, not `chrome` — a popup usually opens over
            // the bars, which are the lightest chrome.
            readonly property color raised: theme._c("surfaceContainer", "#1e1e1e")
            readonly property color selected: theme._c("surfaceContainerHigh", "#262626")
            // The `selected` twin for anything sitting ON a raised surface (a
            // PillCard modal, a picker, a detail card). `selected` is only a
            // few lightness units above `raised`, so the same token used
            // inside a card reads at about half the strength it has on chrome.
            // Consumers pick between the two; nothing derives one from the
            // other, because the step that reads correctly over `chrome` is
            // not the step that reads correctly over `raised`.
            // Was #303036, on a since-removed rule that each rung carry a
            // slightly stronger cool skew than the one below it. The ramp is
            // achromatic now (see the ⚠ note in the ladder above), so this is
            // #303030 for the same reason every other rung lost its blue: the
            // r/g value that set its lightness is unchanged.
            readonly property color raisedSelected: theme._c("surfaceContainerHighest", "#303030")
            // Modal backdrop (AgentSpawnMenu). Black @ 45% — dims the
            // surface enough to read "modal" without hiding context;
            // sits over the already-translucent terminal panes, so a
            // heavier alpha would read as a full blackout on Hyprland.
            readonly property color scrim: "#73000000"
        }

        // Borders.
        readonly property QtObject border: QtObject {
            // White @ 8% alpha. Dropped from 12% with the flat move: the
            // border is now the primary separation cue, and at 12% over a
            // near-black base it reads as a drawn outline rather than an edge.
            // Mirrors FmTheme's `_mattePill` border alpha — keep the two equal.
            //
            // NOT scheme-driven, unlike the surface and text rungs. This is a
            // translucent white overlay that has to work over whatever surface
            // it lands on, and M3 has no role for that — `outlineVariant` is
            // an opaque colour and would stop the border adapting. A palette
            // swap therefore leaves the edge alpha alone, which is correct.
            readonly property color hairline: "#14ffffff"
        }

        // Neutral text ramp. Five rungs cover the useful range from
        // "almost invisible" (dim) to "selected row foreground"
        // (selected). Most chrome text uses `normal` or `strong`.
        // Less bright than the pre-flat ramp: the darker base raises every
        // rung's contrast, so the old values read as glare.
        //
        // Achromatic, on the same 2026-08-19 pass as the surface ladder and
        // for the same reason — these rungs also carried a cool cast (+4 to +6
        // blue-minus-red), and text is where it was hardest to unsee once
        // noticed: a whole list of titles at `normal` reads blue-grey. Same
        // transformation, so the ramp's lightness steps are unchanged.
        readonly property QtObject text: QtObject {
            readonly property color dim: theme._c("outline", "#6e6e6e")
            readonly property color normal: theme._c("onSurfaceVariant", "#a8a8a8")
            readonly property color strong: theme._c("onSurface", "#d4d4d4")
            // `emphasis` and `selected` stay LITERAL: M3 has no neutral role
            // above `onSurface`, and the nearest candidates are accent-derived
            // (`onPrimaryContainer`) or inverted (`inverseSurface`). Reading
            // either would let a scheme tint the top of a ramp the block
            // comment above declares neutral — the exact outcome that comment
            // rules out. They therefore ride `strong`'s scheme value visually
            // rather than tracking it literally; if a swap makes them collide,
            // raise them here rather than routing them.
            readonly property color emphasis: "#e4e4e4"
            readonly property color selected: "#f0f0f0"
        }

        // Warm accents. `primary` and `bright` derive from wine_theme
        // highlights so they feel continuous with the editor palette.
        // `focus` is a deliberate outlier — white at ~40% alpha rather than
        // amber (see the block comment below for the full rationale).
        //
        // `focus` is the active-pane hairline that lights up when
        // keyboard focus enters a pane (Main.qml's mainContent /
        // treeScope border overlays). DELIBERATELY NEUTRAL (white @
        // ~40% alpha) rather than an amber alias — the amber accent
        // at full saturation reads as "warm/important" against the
        // dark chrome and over-signals what is really just a focus
        // hint. White at sub-50% alpha drops both the saturation cue
        // and the contrast cue, leaving the indicator perceptible
        // without competing with the cmdline / branch / mode badges
        // that legitimately own the amber accent. The token still
        // lives under `accent` because semantically it's a "focus
        // accent", not a chrome surface or text rung; only the visual
        // realization shifted (per the aliasing-can-drift rationale
        // already anticipated in this file).
        //
        // Alpha rung: `border.hairline` is ~12% white (matte-pill
        // border, "barely there"); 40% gives clear daylight above
        // that for the active state without crossing into "loud."
        // Tune-up: bump alpha (e.g. `#80ffffff` ≈ 50%) if too subtle
        // on Hyprland with the user's actual wallpaper contrast.
        // Tune-down: drop to `#4dffffff` (~30%) if it still pops.
        readonly property QtObject accent: QtObject {
            readonly property color primary: "#c8a37a"     // cmdline firstchar, branch glyph
            readonly property color bright: "#e8ab6f"      // which-key group headers
            readonly property color focus: "#66ffffff"     // white @ ~40% alpha — active-pane hairline
        }

        // Mode badge palette — wine_theme derivations. See the mapping
        // block above StatusBar.qml's mode badge for the original
        // colorscheme sources.
        readonly property QtObject mode: QtObject {
            readonly property color normal: "#C28B12"      // wine_theme.keyword
            readonly property color insert: "#62BA46"      // wine_theme.string
            readonly property color visual: "#D86DE9"      // wine_theme.term_bright_magenta
            readonly property color replace: "#D2602D"     // wine_theme.error_red
            readonly property color command: "#6D94E9"     // wine_theme.accent_blue
            readonly property color terminal: "#5BDFD8"    // wine_theme.term_bright_cyan
            // Label text color for every badge — wine_theme.bg_primary
            // so the badge reads as the editor background "cut out" of
            // the mode color.
            readonly property color badgeLabel: "#131313"
        }

        // Account-usage thresholds — StatusBar's 5h/7d + context segment.
        // ALIAS the editor `mode.*` hues so the palette stays single-sourced
        // (same provenance pattern as permissionMode / diagnostic / gitDiff).
        // Green→amber→red so the colour itself signals "how close to the limit":
        //   good — < 50% used  (mode.insert, green)
        //   warn — 50–80% used (mode.normal, amber — "reaching a considerable
        //          amount"; NOT mode.command/blue, which read as calm, not warning)
        //   crit — ≥ 80% used  (mode.replace, red)
        // Matches the bash status line's tiers (status-line.sh::get_usage_color,
        // which uses yellow for the mid tier).
        readonly property QtObject usage: QtObject {
            readonly property color good: theme.color.mode.insert
            readonly property color warn: theme.color.mode.normal
            readonly property color crit: theme.color.mode.replace
        }

        // Permission mode pill palette — agent pane chrome. Surfaces the
        // sidecar's authoritative `permissionMode` (default | acceptEdits |
        // bypassPermissions | plan) as a pill the user cycles with
        // Shift+Tab. The colors deliberately ALIAS the editor's `mode.*`
        // hex values so the agent pane's "permission state" semantic
        // reads continuous with the editor's "vim mode" semantic — both
        // are wine_theme-derived state badges, both use the same
        // "your turn / go / stop / command" colour grammar:
        //
        //   default_         — mode.normal (amber): standard prompt path,
        //                       canUseTool round-trips through the in-pane
        //                       card. Reads "your turn to decide per tool".
        //                       Trailing underscore because `default` is a
        //                       JS reserved word in QML property identifiers.
        //   acceptEdits      — mode.insert (green): editor-grade "go" green
        //                       for "auto-allow file-edit tools". Continuous
        //                       with diff.addedBg / permissionApprove which
        //                       both alias the same green for the same
        //                       "approved change" semantic.
        //   bypassPermissions — mode.replace (red): editor-grade "stop" red
        //                       repurposed as "warning — all gates open".
        //                       Red here is intentional: the user is opting
        //                       OUT of safety, the pill should feel loud
        //                       so they cannot forget which mode they're in.
        //   plan             — mode.command (blue): cmdline blue, fitting
        //                       for "planning only, no execution" — same
        //                       palette family as the cmdline firstchar
        //                       which is also a "thinking before doing" cue.
        //
        // Aliasing (not duplicating) means a future palette nudge to the
        // mode colors propagates here automatically — the editor pane and
        // the agent pane stay in lockstep for free. Per the Step 1 protocol
        // discovery for the permission-mode feature: the SDK's gating logic
        // is opaque (native binary), so the sidecar applies a defensive
        // canUseTool short-circuit per mode; this palette must read
        // honestly because it's the user's only feedback for which mode
        // is actually active.
        readonly property QtObject permissionMode: QtObject {
            readonly property color default_: theme.color.mode.normal
            readonly property color acceptEdits: theme.color.mode.insert
            readonly property color bypassPermissions: theme.color.mode.replace
            readonly property color plan: theme.color.mode.command
        }

        // Agent pane role accents. Distinct rung so the agent surface
        // reads as its own context — neither editor chrome nor a mode
        // badge — while staying inside the wine_theme family.
        //
        //   user      — shares tone with `accent.primary` so the user's
        //               turns feel continuous with other user-authored
        //               affordances (branch glyph, cmdline firstchar).
        //               Kept as a dedicated rung rather than aliasing
        //               `accent.primary` so the two can drift independently
        //               without touching unrelated chrome.
        //   assistant — `wine_theme.term_cyan`, the dim sibling of
        //               `mode.terminal`'s bright `term_bright_cyan`.
        //               Same palette family (external-process / otherness),
        //               different tone; does not collide with any
        //               existing mode color.
        //   system    — fully covered by `text.dim`; session-lifecycle,
        //               rate-limit, and result envelope rows borrow it
        //               rather than getting their own rung. Mentioned
        //               here so future readers know the omission is
        //               intentional, not an oversight.
        readonly property QtObject agent: QtObject {
            readonly property color user: "#c8a37a"        // warm amber, shares tone with accent.primary
            readonly property color assistant: "#3DB6B0"   // wine_theme.term_cyan — dim sibling of mode.terminal

            // Permission card tokens. The card fires when claude's SDK
            // canUseTool callback awaits a decision; the user must
            // approve or deny before the tool runs. We deliberately
            // alias the same hex values used by mode.normal/insert/replace
            // so the card's semantic palette reads continuous with the
            // editor's existing "awaiting" / "go" / "stop" cues. Aliasing
            // (not duplicating) means a future palette nudge to those
            // mode colors would update both surfaces together — a
            // single decision touches both editor and agent pane.
            //
            //   permissionBorder — wine_theme.keyword (== mode.normal):
            //                      warm amber says "your turn to decide",
            //                      paired with the user-amber tone above.
            //   permissionApprove — wine_theme.string (== mode.insert):
            //                       editor-grade "go" green for the
            //                       allow affordance.
            //   permissionDeny    — wine_theme.error_red (== mode.replace):
            //                       editor-grade "stop" red for the
            //                       deny affordance.
            readonly property color permissionBorder: "#C28B12"
            readonly property color permissionApprove: "#62BA46"
            readonly property color permissionDeny: "#D2602D"
        }

        // Thread-rail row states (AgentThreadRail.qml). THREE of them can be
        // true on the SAME row at the same time — the agent currently on the
        // central surface (active), the keyboard cursor (selected), and the
        // pointer (hover) — so each one is given a different visual CHANNEL
        // rather than a different value of one fill:
        //
        //   active   — a filled surface        (this rung's `activeRow`)
        //   selected — a left marker bar       (`selectionMarker`)
        //   hover    — a translucent wash ON TOP of whatever fill is there
        //              (`hoverRow`)
        //
        // Three competing FILLS was the first cut and it cannot work: a row
        // that is active AND hovered AND selected has one `color` binding, so
        // two of the three states are simply invisible, which is the bug this
        // rung exists to fix (the rail shipped painting the keyboard cursor in
        // the "active" colour, so the highlight sat on the row the cursor last
        // touched while a DIFFERENT agent was on screen).
        readonly property QtObject rail: QtObject {
            // The active row is RECESSED, not raised — it aliases the CANVAS
            // rung, which is DARKER than the `bar` rung the rail is painted on.
            // It ran on `raisedSelected` (the lightest rung) until 2026-08-19,
            // on the reasoning that the active row is the strongest thing in
            // the column; the user's reading was the opposite and is the better
            // one, because it says something true that a highlight cannot: the
            // active thread is the one whose pane is ON the canvas, so the row
            // showing the canvas colour reads as a window onto it rather than
            // as a brighter version of its neighbours.
            //
            // ⚠ UNDERSTATED ON PURPOSE, and this is the part to read before
            // changing it. At this rung the fill is only ~3 lightness units
            // below the rail, so the mark is quiet. A `border.hairline` edge
            // shipped beside it for exactly that reason and was REMOVED the
            // same day (2026-08-19) on the user's call: it drew a hard bright
            // rectangle, which is a heavier mark than "this is the one you are
            // looking at" needs in a column of quiet neutrals.
            //
            // So do not add the border back, and do not lighten this rung to
            // "restore contrast" — that returns the row to reading as a raised
            // highlight and loses the one thing this says. The fill is also not
            // alone: the active row's title is the only one at the `strong`
            // text rung.
            readonly property color activeRow: theme.color.bg.canvas
            // Pointer wash. White at ~5% alpha, deliberately BELOW
            // `border.hairline`'s 8%: it is painted over BOTH the bar rung and
            // `activeRow`, so it has to read as "the pointer is here" on
            // either ground without ever reading as a second selection.
            readonly property color hoverRow: "#0dffffff"
            // Keyboard cursor bar. The neutral near-white the chrome already
            // uses for its most prominent text, NOT the amber accent: amber is
            // the language of "this thing is special" (worktree glyph, cmdline
            // firstchar, browser attention), and the cursor is not special —
            // it is just where you happen to be standing. A coloured bar read
            // as a status the row did not have.
            readonly property color selectionMarker: theme.color.text.strong
        }

        // Diff visualization. Tints `tool_diff` rows in the agent pane —
        // assistant edits land as a `user`-role tool_result envelope,
        // which `SessionModel._row_from_user` re-routes into a `tool_diff`
        // row carrying a stdlib `difflib.unified_diff` string. The
        // delegate splits on \n and tints each line by leading char.
        //
        // Background colors are low-alpha overlays of the same wine_theme
        // greens/reds that drive `mode.insert` / `mode.replace`. Aliasing
        // the same hue family means a future palette nudge to those mode
        // colors would visually carry through here without touching this
        // rung — the editor's "go" / "stop" semantic stays continuous
        // with the agent pane's "added" / "removed" semantic.
        //
        // Foreground colors are softer siblings tuned for legibility on
        // the tinted background — the saturated mode colors would be
        // hard to read at the body-text size used here.
        //
        //   addedBg / addedFg     — green family (wine_theme.string).
        //   removedBg / removedFg — red family (wine_theme.error_red).
        //   hunkBg / hunkFg       — amber accent for `@@ ... @@` markers,
        //                           derived from accent.primary so the
        //                           hunk header reads as a section
        //                           heading rather than a change line.
        //   contextFg             — neutral text.normal for unchanged
        //                           context lines (` ` prefix).
        readonly property QtObject diff: QtObject {
            readonly property color addedBg: "#3362BA46"     // mode.insert @ ~20% alpha
            readonly property color removedBg: "#33D2602D"   // mode.replace @ ~20% alpha
            readonly property color hunkBg: "#22c8a37a"      // accent.primary @ ~13% alpha
            readonly property color addedFg: "#a8e088"
            readonly property color removedFg: "#f0a285"
            readonly property color hunkFg: "#c8a37a"        // accent.primary
            // BOUND, not mirrored: this was a hand-copied "#b0b0b0 // text.normal"
            // and the flat-aesthetic palette move left it a rung behind the
            // ramp it claimed to follow. Binding removes the drift class.
            readonly property color contextFg: theme.color.text.normal
        }

        // Terminal pane palette — the canonical source mirrored by the
        // forked qmltermwidget's `Symmetria.colorscheme` (see
        // /home/jc/projects/symmetria-qmltermwidget/lib/color-schemes/). The
        // 16-slot ANSI palette deliberately aliases editor `mode.*` and
        // `text.*` tokens where they line up tonally, so the terminal surface
        // reads continuous with the editor — both panes are wine_theme-derived
        // chrome on the same wallpaper-blend backdrop. Background is
        // "transparent" (alpha=0) per the Q2-d topology decision: the terminal
        // is the persistent home surface and shares the editor's ambient
        // wallpaper tint instead of standing out as its own opaque pane.
        // (Drift between this and the fork scheme is a manual sync until the
        // palette is wired through to the fork at build time.)
        //
        // ANSI slot mapping (xterm/VT100 convention):
        //   0 black           — mode.badgeLabel (wine_theme.bg_primary)
        //   1 red             — mode.replace    (wine_theme.error_red)
        //   2 green           — mode.insert     (wine_theme.string)
        //   3 yellow          — mode.normal     (wine_theme.keyword)
        //   4 blue            — mode.command    (wine_theme.accent_blue)
        //   5 magenta         — mode.visual     (wine_theme.term_bright_magenta)
        //   6 cyan            — mode.terminal   (wine_theme.term_bright_cyan)
        //   7 white           — text.normal
        //   8 bright black    — text.dim
        //   9–14 bright       — lighter siblings of slots 1–6, hex values
        //                       below; provisional until cross-referenced
        //                       with wine_theme.lua's own `term_bright_*`
        //                       entries in a follow-up.
        //  15 bright white    — text.selected (brightest neutral rung)
        //
        // The 256-color cube (slots 16–255) is handled inside the
        // qmltermwidget engine (Konsole's color table), NOT exposed
        // here — it's a derived xterm-256 lookup, not a design token.
        // Minimap pane (Phase 0 — editor minimap, see docs/minimap-prd.md).
        // Background: a subtle dim over the wallpaper-blend base, deeper
        // than the editor's ambient tint so the minimap reads as a
        // distinct right-edge ribbon without needing a hairline border.
        // The terminal/editor panes use ~60% black ambient over the
        // wallpaper-blend (the qmltermwidget Symmetria scheme's 0.6 opacity);
        // the minimap adds an additional ~20% black overlay so it sits
        // perceptibly darker than the editor while staying in the same
        // wallpaper-blend family — no hard edge between the two surfaces.
        //
        // Mirrored on the Python side as
        // `minimap_view.py::_BACKGROUND_RGBA` (memoized QColor at module
        // load — gotcha #10). Drift between the two sources is detected
        // by `tests/test_minimap_view.py::test_background_matches_theme_qml`,
        // same dual-source-of-truth pattern the ANSI palette uses (a v2
        // refactor that wires Theme through Python via context property
        // removes the duplication).
        readonly property QtObject minimap: QtObject {
            readonly property color background: "#33000000"   // black @ 20% alpha

            // Indent block palette — Phase 2 of docs/minimap-prd.md.
            // Each buffer line renders as a horizontal bar whose color
            // encodes its leading-whitespace indent depth (0..3,
            // clamped). The four-rung scale is intentionally narrow:
            // four levels covers the dominant code-shape signal
            // (top-level, function body, conditional body, deeply
            // nested) without the painter having to disambiguate
            // finer steps that would not read at 1-2 px row height.
            //
            // Tone gradient is brightest-at-shallow → dimmest-at-deep
            // (visual cue: deeper nesting = quieter), inverting how
            // many block-mode minimaps colour by indent depth. Reason:
            // when you glance at the minimap you want top-level
            // structure to pop (function boundaries, top-level
            // sections), not deep nesting noise. The dim-deep
            // gradient makes the silhouette read as "shape of the
            // file's outline" rather than "where the nesting is."
            //
            // Levels derived from text.normal (strongest) → text.dim
            // (faintest) family so the minimap stays inside the
            // existing chrome palette without introducing a new
            // colour identity.
            //
            readonly property QtObject indent: QtObject {
                // Neutral gray palette tracking the editor's text ramp
                // (Theme.text.emphasis → text.normal → text.dim) so the
                // silhouette reads as "this is text" rather than warm
                // chrome decoration. Earlier amber→brown palette
                // overstated the minimap as its own visual layer; the
                // gray palette lets the surface fade into the editor
                // and only the viewport indicator / gutter markers
                // assert presence.
                //
                // The four rungs still encode indent depth (brightest
                // at top-level so document outline pops), but now they
                // step within Theme's neutral text family rather than
                // wine_theme's amber accent family. Phase 6 (off-
                // viewport tree-sitter highlights) is when the
                // minimap will actually take per-character color from
                // the editor's syntax — until then the gray palette
                // is the closest "respect editor text color" rendering
                // we can land without the highlight pipeline.
                //
                // Mirrored on the Python side as
                // `minimap_view.py::_INDENT_RGBA`. Drift detection in
                // `tests/test_minimap_view.py::test_indent_palette_matches_theme_qml`.
                // Strictly neutral (R == G == B), tracking the text ramp's
                // LUMINANCE rather than its exact hex — the flat palette's
                // text rungs carry a slight cool tint, and this silhouette
                // must not. Mirrored in `minimap_view.py::_INDENT_RGBA`.
                readonly property color level0: "#e4e4e4"    // text.emphasis luminance — top-level, brightest
                readonly property color level1: "#a8a8a8"    // text.normal luminance   — function-body level
                readonly property color level2: "#7e7e7e"    // mid-tone between normal and dim
                readonly property color level3: "#525252"    // deep nesting — quietest, just under text.dim (#6e6e6e)
            }

            // Viewport indicator — Phase 3 of docs/minimap-prd.md.
            // The "spotlight" overlay covering the rows currently
            // visible in the editor. Two tokens:
            //
            //   - viewportFill: translucent overlay painted across
            //     the visible-row range. Aliases the existing
            //     `accent.focus` semantic ("focus / where attention
            //     is") at ~10% alpha — bright enough to perceive
            //     the boundary, dim enough that indent silhouette
            //     reads through it. The bound was once stated as
            //     "just under the matte-pill border's 12% alpha";
            //     that border is now 8% (the flat move), and this
            //     fill deliberately did NOT follow it down. The two
            //     are unrelated surfaces: the border separates chrome
            //     from chrome, while this fill sits over the minimap's
            //     own darker background, where 10% is still quiet.
            //
            //   - viewportFrame: 1-px hairline drawn at the top
            //     AND bottom edges of the spotlight, ~40% white.
            //     Visual cue: where the viewport "starts" and
            //     "ends" — the eye reliably finds horizontal
            //     edges in a vertical silhouette. Aliasing
            //     `accent.focus` (40% white) ties this to the
            //     existing focus-indicator vocabulary.
            //
            // Mirrored on the Python side as
            // `minimap_view.py::_VIEWPORT_FILL_RGBA` /
            // `_VIEWPORT_FRAME_RGBA`. Drift detection in
            // `tests/test_minimap_view.py::test_viewport_palette_matches_theme_qml`.
            readonly property color viewportFill: "#1affffff"    // white @ ~10% alpha
            readonly property color viewportFrame: "#66ffffff"   // white @ ~40% alpha (== accent.focus)

            // Diagnostic gutter (Phase 4 of docs/minimap-prd.md).
            // 4-px left-edge column shows LSP diagnostic severity per line:
            // error rows pop red, warn rows pop amber, info+hint are
            // dimmer cues. Severity palette deliberately ALIASES editor
            // mode colors so the minimap's "this row has a problem"
            // semantic reads continuous with the editor's "this is a
            // stop / your-turn" semantic — wine_theme-derived state
            // grammar across both surfaces.
            //
            //   error  — mode.replace (wine_theme.error_red): "stop"
            //            red, also the diff.removedBg parent — same
            //            "something is wrong" cue
            //   warn   — mode.normal  (wine_theme.keyword): warm
            //            amber, "your turn" tone reused here for
            //            "your attention needed"
            //   info   — mode.command (wine_theme.accent_blue): cmdline
            //            blue, the "this is informational" cue family
            //   hint   — text.dim: the quietest neutral rung — hints
            //            are the lowest-urgency diagnostic level and
            //            should barely register at minimap scale
            //
            // Mirrored on the Python side as
            // `minimap_view.py::_DIAGNOSTIC_RGBA`. Drift detection in
            // `test_minimap_view.py::test_diagnostic_palette_matches_theme_qml`.
            readonly property QtObject diagnostic: QtObject {
                readonly property color error: theme.color.mode.replace
                readonly property color warn: theme.color.mode.normal
                readonly property color info: theme.color.mode.command
                readonly property color hint: theme.color.text.dim
            }

            // Git-diff gutter (Phase 4). Same 4-px column shows
            // gitsigns.nvim hunk status per line, BEHIND the diagnostic
            // dot when both are present (a row with both a diag and a
            // hunk shows the diag dot — more urgent — over the git
            // bar). Palette aliases the editor's diff.*Bg family so the
            // minimap's "this line changed since HEAD" semantic reads
            // continuous with the agent pane's "this line was added/
            // removed by the assistant" semantic.
            //
            //   added    — wine_theme.string (green, aliases mode.insert
            //              and diff.addedBg-family) — "new content"
            //   modified — accent.primary (warm amber, aliases the
            //              cmdline firstchar / branch glyph tone) —
            //              "changed content"
            //   deleted  — wine_theme.error_red (aliases mode.replace
            //              and diff.removedBg-family) — "removed content"
            //
            // Note: gitsigns reports a deleted hunk at the line WHERE
            // the deletion happened (the line that USED to follow the
            // gone block), so the bar reads as "this line marks a
            // deletion above" — same convention the editor's signs
            // column uses.
            //
            // Mirrored as `minimap_view.py::_GITDIFF_RGBA`.
            readonly property QtObject gitDiff: QtObject {
                readonly property color added: theme.color.mode.insert
                readonly property color modified: theme.color.accent.primary
                readonly property color deleted: theme.color.mode.replace
            }
        }

        readonly property QtObject terminal: QtObject {
            // The terminal's background IS the canvas. Zed's six dark themes
            // set `terminal.background == editor.background` without
            // exception, and the QMLTermWidget panes are the editor surface
            // here, so one value serves both.
            //
            // ⚠ This token has NO QML consumer — the panes take their colours
            // from the fork's `Symmetria.colorscheme`, a file in a different
            // repo shipped as a pacman package. So nothing here can enforce
            // the equality; the fork must be edited and `makepkg -sif` run for
            // a change to reach a launcher-started IDE. Keep the two in step
            // by hand. (It read "transparent" until 2026-08-13, describing a
            // wallpaper-blend contract that had already been retired — the
            // hazard of a declared canonical source that nothing reads.)
            readonly property color background: theme.color.bg.canvas
            // Foreground deliberately BRIGHTER than text.normal (#b0b0b0):
            // the quiet-gray ramp is a CHROME aesthetic (labels, badges,
            // status fields), but terminal foreground is CONTENT the user
            // reads all day — at the chrome ramp's `text.normal` over the
            // 0.6-alpha dark blend it read washed-out/thin next to a
            // reference terminal (Ghostty's
            // Material Darker fg is #eeffff). #dcdcdc keeps Symmetria's
            // restraint (no pure white) while restoring legibility.
            readonly property color foreground: "#dcdcdc"

            // Cursor block fill — `accent.bright` so the cursor
            // reads as a warm, attention-drawing element against
            // the neutral text ramp, matching the editor's
            // amber-family accent grammar.
            readonly property color cursor: theme.color.accent.bright

            // 16-slot ANSI palette. Aliased where a `mode.*` color
            // already covers the slot's tonal role; explicit hex
            // for the bright variants 9–14 that lack a mode equivalent.
            readonly property color color0: theme.color.mode.badgeLabel
            readonly property color color1: theme.color.mode.replace
            readonly property color color2: theme.color.mode.insert
            readonly property color color3: theme.color.mode.normal
            readonly property color color4: theme.color.mode.command
            readonly property color color5: theme.color.mode.visual
            readonly property color color6: theme.color.mode.terminal
            readonly property color color7: theme.color.text.normal
            readonly property color color8: theme.color.text.dim
            readonly property color color9: "#E58B5C"   // bright red — lighter than mode.replace
            readonly property color color10: "#86D666"  // bright green — lighter than mode.insert
            readonly property color color11: "#E5B142"  // bright yellow — lighter than mode.normal
            readonly property color color12: "#9CB6F0"  // bright blue — lighter than mode.command
            readonly property color color13: "#E69BF0"  // bright magenta — lighter than mode.visual
            readonly property color color14: "#8AE9E4"  // bright cyan — lighter than mode.terminal
            readonly property color color15: theme.color.text.selected
        }
    }

    // ─── Spacing ─────────────────────────────────────────────────
    // Four-rung scale. Use these for margins, gaps, and padding so
    // rhythm stays uniform across panes. Values are ~20% smaller than
    // a default terminal chrome would use.
    readonly property QtObject spacing: QtObject {
        readonly property int xxs: 2   // tighter internal header gaps
        readonly property int xs: 4
        readonly property int sm: 6
        readonly property int md: 10
        readonly property int lg: 14
        readonly property int xl: 20   // between distinct modules (e.g. the status
                                       // bar's agent groups), where the gap itself
                                       // is the separator — no pipe glyph needed
    }

    // ─── Radius ──────────────────────────────────────────────────
    readonly property QtObject radius: QtObject {
        readonly property int sm: 3
        readonly property int md: 5
        // Floating popup panels (agent spawn menu, session picker,
        // which-key overlay). Mirrors the File Manager's
        // `FmTheme.rounding.lg` (in
        // symmetria-file-manager/.../services/FmTheme.qml) so the IDE's
        // modal surfaces carry the same corner the FM uses for its
        // Miller columns + dialogs — one shared popup-corner language
        // across the Symmetria toolkit.
        //
        // Came down from 16 with the flat move: a generous corner is what made
        // an extruded clay card read as a soft physical object, and with the
        // depth gone the same 16px reads as a dated rounded widget on a large
        // panel. Change this and FmTheme.rounding.lg together.
        //
        // That left `md` (5) and `lg` (8) only 3px apart, so the old argument
        // for the split — "`lg` reads as a pill on a ~34px-tall bar, so thin
        // input chrome stays at `md`" — no longer carries it. They stay
        // separate for a weaker but real reason: `lg` is the PANEL corner and
        // `md` the CONTROL corner, and keeping the two nameable lets one move
        // without the other. If a future pass finds them still converging,
        // collapsing them into one token is the right call.
        readonly property int lg: 8
        // Badge pills use `radius: height / 2` directly — no token needed.

        // The central surface's corner. Deliberately OFF the sm/md/lg scale:
        // that scale sizes CONTROLS (a segment, a card, a popup), and this
        // sizes a window-scale region, so a fourth rung on the control scale
        // would have invited controls to reach for it.
        //
        // The value is Hyprland's `decoration:rounding` (24 as of 2026-08-13),
        // copied rather than read. `hyprctl getoption decoration:rounding -j`
        // would return it, so this is a choice, not an impossibility: that is
        // a subprocess on the startup path, for a value that changes about
        // once a year, which would also have to answer for a non-Hyprland
        // session. The point of matching it is that the canvas corner ECHOES
        // the window corner the compositor already draws around the IDE. If
        // Hyprland's rounding changes, this drifts silently and the only
        // symptom is the two corners disagreeing. Painted by
        // `qml/CanvasCorners.qml`, which explains why a corner here costs four
        // paths instead of one `radius:` property.
        readonly property int canvas: 24
    }

    // ─── Window transparency ─────────────────────────────────────
    // OFF by default (user decision 2026-08-13). One switch, because the
    // effect was never scoped to the terminal even though that is the only
    // place it was wanted: it came from the WINDOW being transparent
    // (`Main.qml`'s root `color`), so every surface that does not paint its
    // own opaque ground inherited it. What that actually looked like:
    //
    //   • the git surface's tab strip showed the wallpaper's bright sky as a
    //     pale band across the top, because GitHistoryView is a bare
    //     FocusScope with no background of its own;
    //   • OpenCode drew ITS own background inside an agent pane, which did
    //     not match, leaving a halo around the block. Claude looked fine only
    //     because it happens to paint a background we agree with -- that was
    //     luck, not design;
    //   • every seam and margin between panes leaked the desktop.
    //
    // Kept as a switch rather than deleted: the wallpaper blend may come back
    // deliberately, and if it does it must be scoped to the TERMINAL surface
    // alone -- an opaque window plus a translucent terminal pane, never a
    // translucent window.
    //
    // ⚠ Flipping `enabled` back to true does NOT restore the old look, and an
    // earlier version of this note wrongly claimed it restored it "exactly".
    // It cannot: the central-surface grounds (`mainContent`'s `bg.canvas`
    // Rectangle and GitHistoryView's) are UNCONDITIONAL, so the panes would
    // blend at 0.6 over an opaque canvas rather than over the wallpaper.
    // That is deliberate and is the scoping the paragraph above asks for —
    // the switch gives a translucent PANE over an opaque window, which is the
    // supported shape. Restoring the full wallpaper blend would additionally
    // mean gating those two grounds, and re-introducing every defect listed
    // above; do not do it by reflex when the switch "looks broken".
    readonly property QtObject transparency: QtObject {
        readonly property bool enabled: false
        // Applied to the QMLTermWidget panes via `backgroundOpacity` when
        // enabled. Deliberately a SECOND copy of the 0.6 baked into the fork's
        // `Symmetria` colorscheme (a different repo), with no drift detection:
        // this property is unread while `enabled` is false, so a drift could
        // only ever surface when someone flips the switch, and a test would
        // have to reach into a sibling checkout that is not guaranteed to
        // exist. Accepted risk, recorded rather than guarded.
        readonly property real terminalOpacity: 0.6
    }

    // ─── Animation ───────────────────────────────────────────────
    // Popup entrance motion — mirrors the File Manager's `Anim` component
    // + scale-pop (FmTheme.animDuration / animCurveStandard, plus the
    // Easing.OutBack overshoot FuzzyFinderPopup/ZoxidePopup use). Shared so
    // EVERY IDE popup gets the same signature "pop in": a quick scale from
    // small with a slight overshoot, paired with an opacity fade on the
    // standard curve. New popup surfaces should bind these rather than
    // hand-rolling per-component durations — keeps the toolkit's motion
    // language coherent with the FM.
    readonly property QtObject anim: QtObject {
        readonly property int duration: 400                                // == FmTheme.animDuration
        // `var`, not `list<real>`: a typed `list<real>` literal here crashes
        // qmllint (exit 255) on the BezierSpline consumer — the CI/pre-commit
        // qmllint hook would fail. `easing.bezierCurve` accepts the var array
        // identically at runtime. Do NOT "fix" this back to list<real>.
        readonly property var standardCurve: [0.2, 0, 0, 1, 1, 1]   // == FmTheme.animCurveStandard
        readonly property real popFromScale: 0.1                           // entrance start scale
        readonly property real popOvershoot: 1.5                           // Easing.OutBack overshoot
        // State-transition duration — elevation / hover / focus eases inside
        // PillSurface (the clay shadow + fill + border fades). Deliberately
        // much shorter than `duration` (the 400ms entrance pop): raising a
        // chip when focus lands on it should feel responsive, not laggy. A
        // 400ms fade on the surface switcher read sluggish on a rapid tap.
        readonly property int quick: 150
        // How long a surface must stay under the pointer (or under the
        // keyboard cursor) before an informational panel opens for it — the
        // thread rail's peek panel. Long enough that running down the list
        // with j/k does not fire a burst of panels, short enough that pausing
        // on a row feels like it answered immediately. Deliberately longer
        // than `quick` (which is a state FADE, not a decision to open).
        readonly property int peekDelay: 350
    }

    // ─── Depth (claymorphism — NEUTRALIZED) ──────────────────────
    // ⚠ EVERY ALPHA IN THIS BLOCK IS DELIBERATELY 0. The IDE and the File
    // Manager are moving to a flat aesthetic (Zed-like: flat fills, hairline
    // separation, state expressed by fill + text weight rather than by
    // extrusion). See `docs/flat-aesthetic-plan.md`. Do NOT "restore" these
    // to their historical values — the flatness is the intent, not a bug.
    //
    // What this block used to do: a matte fill + hairline border was raised
    // off the surface by TWO opposing outer shadows (a dark SE drop + a light
    // NW lift) plus a top rim-highlight gradient, faking an overhead light so
    // the surface read as a physically extruded chip. Zeroing the alphas is
    // the whole kill switch — it flattens all 13 consuming files without
    // touching one of them, because `PillSurface`/`PillCard` are the only
    // readers and every consumer takes their defaults.
    //
    // The OFFSETS and BLURS are left at their historical values on purpose.
    // They are inert while the alphas are 0 (a fully transparent shadow paints
    // nothing regardless of geometry), and keeping them makes the whole clay
    // look recoverable by editing four numbers if the flat direction is
    // reversed mid-flight. Phase 5 of the plan deletes this block, both
    // components, and the `RectangularShadow` machinery outright — at which
    // point the geometry goes with it.
    //
    // Consumed ONLY by `qml/PillSurface.qml` (chip preset) and
    // `qml/PillCard.qml` (card preset). Surfaces bind those components, never
    // these constants directly.
    readonly property QtObject depth: QtObject {
        // Top rim highlight — the "lit-from-above" warmth shared by both
        // presets. Was 0.08; the single cue that most distinguished clay from
        // a flat matte capsule, so it is the first thing to go.
        readonly property real highlightAlpha: 0.0

        // Chip preset — compact chrome capsules: the surface switcher
        // segments, the agent bubbles, the dialog buttons. Alphas were
        // dark 0.40 / light 0.10.
        readonly property QtObject chip: QtObject {
            readonly property real darkOffsetX: 1
            readonly property real darkOffsetY: 2
            readonly property real darkBlur: 8
            readonly property real darkAlpha: 0.0
            readonly property real lightOffsetX: -1
            readonly property real lightOffsetY: -1
            readonly property real lightBlur: 6
            readonly property real lightAlpha: 0.0
            readonly property real innerShadowAlpha: 0.0
        }

        // Card preset — framed surfaces: the modal panels (agent spawn menu,
        // session picker, close-confirm dialog, usage detail popup). Alphas
        // were dark 0.28 / light 0.07 / inner 0.03.
        readonly property QtObject card: QtObject {
            readonly property real darkOffsetX: 2
            readonly property real darkOffsetY: 4
            readonly property real darkBlur: 14
            readonly property real darkAlpha: 0.0
            readonly property real lightOffsetX: -2
            readonly property real lightOffsetY: -3
            readonly property real lightBlur: 11
            readonly property real lightAlpha: 0.0
            readonly property real innerShadowAlpha: 0.0
        }
    }

    // ─── Component sizing ────────────────────────────────────────
    // Repeated concrete dimensions that multiple chrome components
    // need in common. One-off pixel values that only appear in a
    // single file stay local to that file.
    readonly property QtObject size: QtObject {
        readonly property int statusBarHeight: 24     // was 30
        readonly property int modeBadgeHeight: 18     // was 22
        readonly property int popupRowHeight: 20      // was 24
        // Provider brand mark in the usage readout + its detail popup. Sized
        // to the `sm` text rung it sits beside rather than to a round number:
        // a logo taller than its neighbouring digits reads as a button.
        readonly property int usageProviderIcon: 11
        // Usage detail popup. Wide enough for "GPT-5.3-Codex-Spark" plus its
        // countdown and percentage on one line — the longest row the panel
        // can produce — without wrapping.
        readonly property int usagePopupWidth: 300
        // Agent thread rail (AgentThreadRail.qml), the left column. Narrower
        // than the 280px file tree on purpose: a tree row carries a path that
        // must not wrap, while a thread row carries a sparkle, a number and a
        // title that is allowed to elide. Wide enough for the indicator
        // cluster plus a readable stretch of title at the `xs` rung.
        readonly property int threadRailWidth: 220
        // The rail's peek panel — the hover/keyboard tooltip that shows a
        // thread's FULL title, which the row itself elides. Wider than the
        // rail it hangs off (220) because a panel no wider than the row would
        // only re-elide the same text; 300 matches `usagePopupWidth`, so the
        // IDE's two informational panels read as one surface family.
        readonly property int threadPeekWidth: 300
        // The rail's keyboard-cursor bar. Its own token rather than a
        // `spacing` rung because the scale jumps 2 -> 4 and neither works: at
        // 2 the capsule's 1px radius does not resolve and it renders as a hard
        // square-ended line, while 4 reads heavier than the cursor deserves.
        // 3 is the narrowest width whose rounding still resolves.
        readonly property int threadRailMarkerWidth: 3
        readonly property int whichKeyRowHeight: 18   // was 22
        readonly property int whichKeyFooterHeight: 24 // was 28

        // Minimap right-edge ribbon width (Phase 0 — editor minimap, see
        // docs/minimap-prd.md). 80px is wide enough for the Phase 2 indent
        // silhouette to read distinctly without crowding the editor grid;
        // Phase 5's 2×4 px glyph atlas will fit ~40 columns at this width
        // which matches typical line-length-at-a-glance. Tune-up to 100
        // for wider source files; tune-down to 60 once Phase 5 ships if
        // the 2x4 cell footprint reads as overkill.
        readonly property int minimapWidth: 80

        // Padding between a terminal pane's edge and its character grid,
        // applied via the fork's `margin` Q_PROPERTY on BOTH QMLTermWidget
        // panes (editor nvim + shell) so they share one breathing rhythm.
        // Reference is Ghostty's window-padding-x/y = 20; 16 follows the
        // Theme's documented "~20% smaller than default terminal chrome"
        // scale. The widget paints its translucent background across the
        // padded strip too — this is NOT achievable with QML anchor
        // margins, which would leave an unblended transparent gutter.
        readonly property int terminalPadding: 16
    }
}
