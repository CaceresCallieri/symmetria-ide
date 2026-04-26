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
        readonly property QtObject accent: QtObject {
            readonly property color primary: "#c8a37a"     // cmdline firstchar, branch glyph
            readonly property color bright: "#e8ab6f"      // which-key group headers
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
