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


# Default cell point size. Conservative Phase-0 value; font configuration
# is not yet user-exposed.
DEFAULT_FONT_POINT_SIZE = 9


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
        # Patched variants first — self-sufficient, and what the user's
        # Ghostty config likely also resolves to.
        "JetBrainsMono Nerd Font Mono",
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
    fallbacks = [c for c in ("Symbols Nerd Font", "Noto Color Emoji") if c in families]

    for name in preferred:
        if name in families:
            font = QFont(name)
            font.setPointSize(DEFAULT_FONT_POINT_SIZE)
            font.setStyleHint(QFont.StyleHint.Monospace)
            font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
            if fallbacks:
                font.setFamilies([name, *fallbacks])
            return font

    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    font.setPointSize(DEFAULT_FONT_POINT_SIZE)
    if fallbacks:
        # systemFont's first family is whatever fontconfig picked; preserve
        # it and append our cascade. `families()` can be [] on some Qt
        # builds — fall back to `family()` so we never seed an empty string.
        current_families = font.families() or [font.family()]
        font.setFamilies([*current_families, *fallbacks])
    return font
