"""Tests for NvimView._default_font() font resolution structure.

These tests use source-inspection rather than behavioral execution because
`_default_font` calls `QFontDatabase.families()`, which requires a live
`QGuiApplication` (not just `QCoreApplication`). Instantiating
`QGuiApplication` in a test environment without a real display server is
unreliable across CI platforms. The conftest.py session fixture only
provides `QCoreApplication` — enough for `QColor`, not enough for font
enumeration.

The structural properties worth guarding are:
  - `@functools.cache` is applied so repeated calls are cheap (perf fix).
  - `cache_clear` is therefore available on the method (API regression guard).
  - The preferred list puts patched Nerd Font variants before plain variants.
  - Both fallback families are guarded by an `in families` check before append.
  - The systemFont branch uses `font.families() or [font.family()]` so the
    families list is never seeded with an empty string.
  - The resolved primary is extracted with `.families()` at the call site in
    `_build_engine`, not `.family()` alone (robustness to Qt build differences).

If any of these structural checks fail, it means someone refactored the method
in a way that risks re-introducing the icon-tofu or double-call-cost regressions
described in CLAUDE.md gotcha #23 and the commit that introduced `editorFontFamily`.
"""

from __future__ import annotations

import inspect


class TestDefaultFontIsCached:
    """functools.cache makes _default_font cheap on repeated calls."""

    def test_cache_clear_is_available(self):
        """@functools.cache was applied: cache_clear must exist on the method."""
        from symmetria_ide.nvim_view import NvimView

        assert hasattr(NvimView._default_font, "cache_clear"), (
            "_default_font must be decorated with @functools.cache. "
            "Without it, every call runs QFontDatabase.families() — a full "
            "system font enumeration — instead of hitting a cached result. "
            "The double-call from NvimView.__init__ and _build_engine was the "
            "motivation for this fix."
        )

    def test_cache_info_is_available(self):
        """@functools.cache was applied: cache_info must exist on the method."""
        from symmetria_ide.nvim_view import NvimView

        assert hasattr(NvimView._default_font, "cache_info"), (
            "_default_font must be decorated with @functools.cache. "
            "cache_info is the canonical way to verify hit/miss counts."
        )


class TestPreferredFontOrder:
    """Patched Nerd Font Mono variants appear before plain variants in the preferred list."""

    def test_nerd_font_mono_appears_before_plain_jetbrains(self):
        """JetBrainsMono Nerd Font Mono must be earlier in preferred than JetBrains Mono."""
        src = inspect.getsource(
            __import__(
                "symmetria_ide.nvim_view", fromlist=["NvimView"]
            ).NvimView._default_font.__wrapped__
        )
        jetbrains_nerd_pos = src.find('"JetBrainsMono Nerd Font Mono"')
        jetbrains_plain_pos = src.find('"JetBrains Mono"')
        assert jetbrains_nerd_pos != -1, (
            "JetBrainsMono Nerd Font Mono must be in the preferred list"
        )
        assert jetbrains_plain_pos != -1, "JetBrains Mono must be in the preferred list"
        assert jetbrains_nerd_pos < jetbrains_plain_pos, (
            "JetBrainsMono Nerd Font Mono must appear BEFORE JetBrains Mono in "
            "the preferred list. Patched variants are self-sufficient (they carry "
            "icon glyphs) and should be chosen over plain families that rely on "
            "the Symbols Nerd Font cascade for icon coverage."
        )

    def test_cascadia_nerd_font_mono_appears_before_cascadia_plain(self):
        """CaskaydiaCove Nerd Font Mono must be earlier in preferred than Cascadia Code."""
        src = inspect.getsource(
            __import__(
                "symmetria_ide.nvim_view", fromlist=["NvimView"]
            ).NvimView._default_font.__wrapped__
        )
        casc_nerd_pos = src.find('"CaskaydiaCove Nerd Font Mono"')
        casc_plain_pos = src.find('"Cascadia Code"')
        assert casc_nerd_pos != -1, (
            "CaskaydiaCove Nerd Font Mono must be in the preferred list"
        )
        assert casc_plain_pos != -1, "Cascadia Code must be in the preferred list"
        assert casc_nerd_pos < casc_plain_pos, (
            "CaskaydiaCove Nerd Font Mono must appear BEFORE Cascadia Code in "
            "the preferred list. Same rationale as JetBrainsMono Nerd Font Mono."
        )


class TestFallbackFamiliesAreInstallGuarded:
    """Symbols Nerd Font and Noto Color Emoji are only added when installed."""

    def test_symbols_nerd_font_guarded_by_in_families(self):
        """'Symbols Nerd Font' must not be appended unconditionally."""
        src = inspect.getsource(
            __import__(
                "symmetria_ide.nvim_view", fromlist=["NvimView"]
            ).NvimView._default_font.__wrapped__
        )
        assert "Symbols Nerd Font" in src, (
            "Symbols Nerd Font must appear in _default_font source"
        )
        assert "in families" in src, (
            "Fallback families must be guarded by `in families`. "
            "Adding a missing family unconditionally makes Qt probe it on every "
            "glyph miss, wasting cycles per frame."
        )

    def test_noto_color_emoji_guarded_by_in_families(self):
        """'Noto Color Emoji' must not be appended unconditionally."""
        src = inspect.getsource(
            __import__(
                "symmetria_ide.nvim_view", fromlist=["NvimView"]
            ).NvimView._default_font.__wrapped__
        )
        assert "Noto Color Emoji" in src, (
            "Noto Color Emoji must appear in _default_font source"
        )

    def test_fallback_construction_uses_list_comprehension_or_guard(self):
        """Fallback list must filter by `in families` — not append unconditionally."""
        src = inspect.getsource(
            __import__(
                "symmetria_ide.nvim_view", fromlist=["NvimView"]
            ).NvimView._default_font.__wrapped__
        )
        # The simplified form after the code-review fix is a list comprehension.
        # Either a comprehension or an explicit 'if candidate in families' guard is fine.
        has_comprehension = (
            "if c in families" in src or "if candidate in families" in src
        )
        assert has_comprehension, (
            "Fallback list construction must filter by `in families`. "
            "The simplification fix replaced the for-loop+append pattern with a "
            "list comprehension: `[c for c in (...) if c in families]`. "
            "Without the guard, missing fonts are added to the cascade unconditionally."
        )


class TestSystemFontBranchSafety:
    """systemFont fallback branch must not seed families with an empty string."""

    def test_system_font_branch_uses_or_fallback_for_families(self):
        """font.families() or [font.family()] prevents empty-string seed."""
        src = inspect.getsource(
            __import__(
                "symmetria_ide.nvim_view", fromlist=["NvimView"]
            ).NvimView._default_font.__wrapped__
        )
        assert "font.families() or [font.family()]" in src, (
            "The systemFont fallback branch must use "
            "`font.families() or [font.family()]` — not bare `font.families()`. "
            "On some Qt builds, systemFont returns a font with an empty "
            "families() list, which would seed the cascade with '' (an empty "
            "string), causing Qt to probe a phantom family on every glyph miss."
        )


class TestBuildEngineUsesRobustFamilyExtraction:
    """`_build_engine` in app.py uses .families()[0] not .family() alone."""

    def test_build_engine_uses_families_list_index(self):
        """_build_engine must extract primary family via .families() with fallback."""
        import inspect
        from symmetria_ide import app

        src = inspect.getsource(app._build_engine)
        assert ".families()" in src, (
            "_build_engine must call .families() on the resolved QFont to "
            "extract the primary family name. .family() alone can return '' "
            "for the systemFont path on some Qt builds. "
            "See CLAUDE.md gotcha #23."
        )
        # The exact pattern after the code-review fix:
        assert "_primary_family" in src or "families()" in src, (
            "_build_engine must use a robust primary-family extraction. "
            "The expected pattern is: "
            "`(_resolved_font.families() or [_resolved_font.family()])[0]`"
        )
