"""Shared editor font resolution.

A single resolved `QFont` with a Nerd-Font / emoji per-glyph fallback
cascade, exposed to QML as the `editorFontFamily` / `editorFontPointSize`
context properties so the QMLTermWidget editor + shell panes and the
chrome overlays all track the same primary family. Lives in its own module
(rather than on a view class) so it has no rendering dependencies — the
historical home, `NvimView._default_font`, went away with the custom grid
renderer.

`default_font()` is `functools.cache`d: the family probe + fallback
assembly run once per process. Requires a live `QGuiApplication`
(`QFontDatabase.families()` is undefined without one); in tests the
session-scoped `qt_app` fixture must run first.
"""

from __future__ import annotations

import functools

from PySide6.QtGui import QFont, QFontDatabase


# Default cell point size. One point above the user's Ghostty config
# (font-size 8.5) — explicitly requested 2026-06-09 after the sharpness
# pass; font configuration is not yet user-exposed. Float — consumers
# must use setPointSizeF, and the QML context prop is already exposed as
# a real.
DEFAULT_FONT_POINT_SIZE = 9.5


@functools.cache
def default_font() -> QFont:
    """Pick the first installed monospace font we recognize, with a
    Nerd-Font + emoji per-glyph fallback cascade.

    ### Nerd Font glyph fallback

    Plugins (fff.nvim, which-key, gitsigns, dap-ui, …) emit Private-Use-Area
    codepoints for icons. A plain `QFont(name)` only renders glyphs the
    FAMILY provides; `StyleHint.Monospace` is a family-substitution hint,
    NOT a per-glyph fallback. So we build an explicit `setFamilies([primary,
    *fallbacks])` cascade — the same thing a terminal config does.

    Strategy: prefer Nerd-Font-patched monospace variants (self-sufficient),
    then append `Symbols Nerd Font` (PUA icons the patched-mono variant may
    lack) and `Noto Color Emoji` (e.g. U+1FABF the goose fff.nvim uses) as
    ordered fallbacks. `setFamilies` is ordered-and-exclusive, so emoji must
    be listed explicitly — fontconfig's system fallback will NOT fire.
    """
    preferred = [
        # Non-"Mono" variant, deliberately — it is literally the family the
        # user's Ghostty config names, and Nerd Fonts' "Mono" flavor shrinks
        # every icon glyph to single-cell width, which made all TUI icons
        # (file tree, git, powerline) visibly smaller than Ghostty's.
        # A/B-verified in the terminal pane (2026-06-09): full-size icons,
        # no clipping (icons bleed into the conventional trailing space
        # cell), identical ASCII metrics so the cell grid is unaffected.
        "JetBrainsMono Nerd Font",
        "CaskaydiaCove Nerd Font Mono",
        # Plain families below — glyph fallback kicks in for icons.
        "Iosevka",
        "JetBrains Mono",
        "Fira Code",
        "Cascadia Code",
        "Source Code Pro",
        "Hack",
        "DejaVu Sans Mono",
    ]
    families = QFontDatabase.families()
    # Ordered per-glyph fallbacks, mirroring what Ghostty actually resolves
    # on this system so shared glyphs render identically in both terminals:
    #   - "FreeSerif" supplies the dingbat/star glyphs JetBrains Mono lacks
    #     — notably the Claude Code spinner family U+2733 ✳ / U+273B ✻ /
    #     U+273D ✽. Verified via `ghostty +show-face --cp=10043` →
    #     “FreeSerif” (NOT fc-match's first candidate, Adwaita Mono — the
    #     two discovery algorithms disagree; trust the renderer's answer).
    #   - "Adwaita Mono" (pacman: adwaita-fonts) stays as a mono-friendly
    #     backstop for codepoints FreeSerif lacks.
    #   - Both MUST precede "Noto Color Emoji": U+2733 has an emoji
    #     presentation, and with the emoji font first that codepoint
    #     renders as a color emoji sticker instead of monochrome text.
    fallbacks = [
        c
        for c in ("Symbols Nerd Font", "FreeSerif", "Adwaita Mono", "Noto Color Emoji")
        if c in families
    ]

    for name in preferred:
        if name in families:
            font = QFont(name)
            font.setPointSizeF(DEFAULT_FONT_POINT_SIZE)
            font.setStyleHint(QFont.StyleHint.Monospace)
            # FULL hinting (3-way A/B at device res, 2026-06-09: no-hinting
            # soft, vertical-only leaves vertical stems smeared, full +
            # the v35 FreeType interpreter set in app.py::run is
            # Ghostty-class sharp). Full hinting was the ORIGINAL
            # uneven-rendering bug, but only because run glyphs drifted off
            # the cell grid at the font's natural fractional advance — the
            # qmltermwidget fork's letter-spacing cell snap fixed that, so
            # the worst case now is ±0.5px per-glyph blit rounding with no
            # accumulation. Main.qml mirrors this via
            # `font.hintingPreference` on the terminal panes (the
            # family/pointSize context props don't carry it) — keep both
            # sites in sync.
            font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
            if fallbacks:
                font.setFamilies([name, *fallbacks])
            return font

    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    font.setPointSizeF(DEFAULT_FONT_POINT_SIZE)
    if fallbacks:
        # systemFont's first family is whatever fontconfig picked; preserve
        # it and append our cascade. `families()` can be [] on some Qt
        # builds — fall back to `family()` so we never seed an empty string.
        current_families = font.families() or [font.family()]
        font.setFamilies([*current_families, *fallbacks])
    return font
