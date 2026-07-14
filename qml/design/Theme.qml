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
//   - Chrome bg/border: Symmetria Shell mattePill() at intensity 0.3
//     ("subtle" — the canonical value used by the shell's bar pills).
//     Source: `~/.config/quickshell/symmetria/services/Colours.qml`.
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
    }

    // ─── Color ───────────────────────────────────────────────────
    readonly property QtObject color: QtObject {
        // Backgrounds — matte pill palette.
        readonly property QtObject bg: QtObject {
            readonly property color chrome: "#201F1F"      // mattePill(m3surfaceContainerHigh, 0.3)
            readonly property color selected: "#282728"    // mattePill(m3surfaceContainerHigh, 0.7)
            // Modal backdrop (AgentSpawnMenu). Black @ 45% — dims the
            // surface enough to read "modal" without hiding context;
            // sits over the already-translucent terminal panes, so a
            // heavier alpha would read as a full blackout on Hyprland.
            readonly property color scrim: "#73000000"
        }

        // Borders.
        readonly property QtObject border: QtObject {
            readonly property color hairline: "#1fffffff"  // white @ 12% alpha — matte pill border
        }

        // Neutral text ramp. Five rungs cover the useful range from
        // "almost invisible" (dim) to "selected row foreground"
        // (selected). Most chrome text uses `normal` or `strong`.
        readonly property QtObject text: QtObject {
            readonly property color dim: "#7a7a7a"
            readonly property color normal: "#b0b0b0"
            readonly property color strong: "#e0e0e0"
            readonly property color emphasis: "#e8e8e8"
            readonly property color selected: "#f5f5f5"
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
            readonly property color contextFg: "#b0b0b0"     // text.normal
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
                readonly property color level0: "#e8e8e8"    // text.emphasis — top-level, brightest
                readonly property color level1: "#b0b0b0"    // text.normal   — function-body level
                readonly property color level2: "#888888"    // mid-tone between normal and dim
                readonly property color level3: "#5a5a5a"    // deep nesting — quietest, slightly darker than text.dim (#7a7a7a)
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
            //     reads through it. The matte-pill border at 12%
            //     alpha is the upper bound; viewport fill goes
            //     just under so frame + fill don't fight.
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
            // Background: transparent so the wallpaper ambient tint
            // shows through. The editor + shell QMLTermWidget panes sit
            // as siblings in `Main.qml::mainContent`, sharing the same
            // transparent wallpaper-blend surface contract.
            readonly property color background: "transparent"
            // Foreground deliberately BRIGHTER than text.normal (#b0b0b0):
            // the quiet-gray ramp is a CHROME aesthetic (labels, badges,
            // status fields), but terminal foreground is CONTENT the user
            // reads all day — at #b0b0b0 over the 0.6-alpha dark blend it
            // read washed-out/thin next to a reference terminal (Ghostty's
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
        // `FmTheme.rounding.lg` (== 16, in
        // symmetria-file-manager/.../services/FmTheme.qml) so the IDE's
        // modal surfaces carry the same soft corner the FM uses for its
        // Miller columns + dialogs — one shared popup-corner language
        // across the Symmetria toolkit. Thin input chrome (the cmdline
        // box) deliberately stays at `md` — `lg` reads as a pill on a
        // ~34px-tall bar.
        readonly property int lg: 16
        // Badge pills use `radius: height / 2` directly — no token needed.
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
    }

    // ─── Depth (claymorphism) ────────────────────────────────────
    // The "clay" recipe — the Symmetria toolkit's shared depth language,
    // ported from the File Manager / Symmetria Shell `PillSurface`. A matte
    // fill + hairline border is raised off the surface by TWO opposing outer
    // shadows (a dark SE drop + a light NW lift) plus a top rim-highlight
    // gradient. Together they fake an overhead light so the surface reads as
    // a physically extruded chip rather than a flat rect — the same look the
    // Shell's bar pills and the FM's active tab / dialogs use.
    //
    // Consumed ONLY by `qml/PillSurface.qml` (chip preset) and
    // `qml/PillCard.qml` (card preset). Surfaces bind those components, never
    // these constants directly — the recipe stays in one place so a retune
    // here propagates to every clay surface at once.
    //
    // Provenance: symmetria-file-manager/.../components/PillSurface.qml
    // (chip defaults) + PillCard.qml (card overrides). Offsets/blur are
    // nudged ~tighter than the FM's because most IDE clay sits on compact
    // 24px chrome bars (see the typography "~20% smaller" note above), where
    // the FM's wider blur would bleed past the bar's margins; the FM's own
    // values are roomier surfaces.
    readonly property QtObject depth: QtObject {
        // Top rim highlight — the "lit-from-above" warmth shared by both
        // presets. White at low alpha along the top edge, fading out by
        // mid-height. The single cue that most distinguishes clay from a
        // flat matte capsule.
        readonly property real highlightAlpha: 0.08

        // Chip preset — raised capsules on compact chrome: the surface
        // switcher segment, the agent bubbles, the dialog buttons. Tight
        // offsets + modest blur so the shadow stays mostly within a 24px
        // bar's vertical margins instead of bleeding onto the content below.
        readonly property QtObject chip: QtObject {
            readonly property real darkOffsetX: 1
            readonly property real darkOffsetY: 2
            readonly property real darkBlur: 8
            readonly property real darkAlpha: 0.40
            readonly property real lightOffsetX: -1
            readonly property real lightOffsetY: -1
            readonly property real lightBlur: 6
            readonly property real lightAlpha: 0.10
            // Bottom inner-shadow off for chips (a heavy bottom-inner band
            // makes a small chip feel dated); cards turn it on.
            readonly property real innerShadowAlpha: 0.0
        }

        // Card preset — framed surfaces: the modal panels (agent spawn menu,
        // session picker, close-confirm dialog). Wider, softer shadows for
        // an "embedded panel" feel + a faint bottom inner-shadow. Modals are
        // centered in the full window with ample room around them, so the
        // larger blur reads as depth rather than bleeding into neighbours.
        readonly property QtObject card: QtObject {
            readonly property real darkOffsetX: 2
            readonly property real darkOffsetY: 4
            readonly property real darkBlur: 14
            readonly property real darkAlpha: 0.28
            readonly property real lightOffsetX: -2
            readonly property real lightOffsetY: -3
            readonly property real lightBlur: 11
            readonly property real lightAlpha: 0.07
            readonly property real innerShadowAlpha: 0.03
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
