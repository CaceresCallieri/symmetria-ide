"""Tests for editor_font.default_font — the shared editor font resolver.

`default_font()` calls `QFontDatabase.families()`, which requires a live
`QGuiApplication`, so we use attribute-inspection here (mirrors the old
`NvimView._default_font` tests) rather than invoking it. The function was
relocated out of the deleted `NvimView` when nvim moved into the terminal.
"""

from __future__ import annotations

from symmetria_ide.editor_font import DEFAULT_FONT_POINT_SIZE, default_font


def test_default_font_is_cached():
    """@functools.cache dedups the family probe — the double call from
    TerminalView.__init__ and _build_engine must hit the cache, not
    re-run QFontDatabase.families() twice."""
    assert hasattr(default_font, "cache_clear")
    assert hasattr(default_font, "cache_info")


def test_default_point_size_constant():
    """9.5 = the user's Ghostty font-size (8.5) + 1pt, requested after the
    sharpness pass (2026-06-09)."""
    assert DEFAULT_FONT_POINT_SIZE == 9.5
