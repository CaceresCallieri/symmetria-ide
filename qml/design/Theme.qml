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
// the actual `QFontDatabase` cascade via `NvimView._default_font()` and
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

    // ─── Color ───────────────────────────────────────────────────
    readonly property QtObject color: QtObject {
        // Backgrounds — matte pill palette.
        readonly property QtObject bg: QtObject {
            readonly property color chrome: "#201F1F"      // mattePill(m3surfaceContainerHigh, 0.3)
            readonly property color selected: "#282728"    // mattePill(m3surfaceContainerHigh, 0.7)
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

        // Warm accents — amber family. Both derive from wine_theme
        // highlights so they feel continuous with the editor palette.
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

        // Terminal pane palette — pyte cell rendering for the native
        // PTY terminal (Phase 2.5 deliverable 2). The 16-slot ANSI
        // palette deliberately aliases editor `mode.*` and `text.*`
        // tokens where they line up tonally, so the terminal surface
        // reads continuous with the editor — both panes are
        // wine_theme-derived chrome on the same wallpaper-blend
        // backdrop. Background is "transparent" (alpha=0) per the
        // Q2-d topology decision: the terminal is the persistent home
        // surface and shares the editor's ambient wallpaper tint
        // instead of standing out as its own opaque pane.
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
        // The 256-color cube (slots 16–255) is computed at runtime
        // in `terminal_view.py`'s memoized color resolver, NOT
        // exposed here — it's a derived xterm-256 lookup, not a
        // design token.
        readonly property QtObject terminal: QtObject {
            // Background: transparent so the wallpaper ambient tint
            // shows through, mirroring NvimView's wallpaper-blend
            // treatment. The terminal pane sits as a sibling to
            // NvimView in `Main.qml::mainContent`, sharing the same
            // visual surface contract.
            readonly property color background: "transparent"
            readonly property color foreground: theme.color.text.normal

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
        readonly property int xs: 4
        readonly property int sm: 6
        readonly property int md: 10
        readonly property int lg: 14
    }

    // ─── Radius ──────────────────────────────────────────────────
    readonly property QtObject radius: QtObject {
        readonly property int sm: 3
        readonly property int md: 5
        // Badge pills use `radius: height / 2` directly — no token needed.
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
    }
}
