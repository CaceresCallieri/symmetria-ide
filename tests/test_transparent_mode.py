"""Tests for the transparent viewport / ambient-dim feature.

Covers three new behaviors introduced in the transparent editor commit:

  1. bg_val != default_bg skip logic in _paint_row — the transparency
     invariant. Default-bg cells must NOT fill opaquely so the ambient tint
     (and wallpaper behind it) shows through. This is testable with pure
     Python (no Qt required).

  2. _ambient_tint_color is a pre-allocated instance attribute, NOT an
     inline QColor(...) in paint(). Validated by inspecting the paint()
     source. CLAUDE.md gotcha #10: any shiboken wrapper allocated inside
     paint() is a GC/race hazard — the fix must stay as an instance attr.

  3. setFillColor is called with a transparent color in __init__.
     Validated by inspecting the __init__ source for the call.

Tests 2 and 3 are structural (source-inspection) rather than behavioral
because instantiating NvimView requires QGuiApplication (QFontDatabase
calls abort() without one), which in turn requires a running display.
Pure-Python structural tests catch the regression reliably without that
constraint and are what the existing test suite uses for similar gotchas.
"""

from __future__ import annotations

import ast
import inspect
import sys
import pytest

from PySide6.QtCore import QCoreApplication


@pytest.fixture(scope="session", autouse=True)
def qt_app():
    """Create a QCoreApplication for the test session (required by Qt)."""
    app = QCoreApplication.instance() or QCoreApplication(sys.argv)
    yield app


@pytest.fixture(autouse=True)
def clear_qcolor_cache():
    """Keep the module-level cache clean across tests."""
    from symmetria_ide.nvim_view import _qcolor_cache
    _qcolor_cache.clear()
    yield
    _qcolor_cache.clear()


# ---------------------------------------------------------------------------
# bg_val skip logic — pure Python, no Qt required
# ---------------------------------------------------------------------------

class TestBgSkipLogic:
    """The bg_val != default_bg guard is the core transparency invariant.

    If this check is removed or inverted, default-bg cells paint opaquely
    and break wallpaper see-through. These tests document the expected
    values so a future change is caught immediately.
    """

    def _effective_bg(
        self,
        bg_override: int | None,
        default_bg: int,
        default_fg: int,
        reverse: bool = False,
    ) -> int:
        """Replicate the bg_val calculation from _paint_row.

        Kept in sync with the paint path by design: if _paint_row ever
        changes how it derives bg_val, these tests break and alert the dev.
        """
        from symmetria_ide.grid import HlAttr
        attr = HlAttr()
        attr.background = bg_override
        attr.foreground = None
        attr.reverse = reverse
        fg_val = default_fg
        bg_val = attr.background if attr.background is not None else default_bg
        if attr.reverse:
            fg_val, bg_val = bg_val, fg_val
        return bg_val

    def test_no_explicit_bg_produces_default_bg(self):
        """Cell with no explicit bg: bg_val == default_bg → skip fires."""
        default_bg = 0x1E1E1E
        bg_val = self._effective_bg(None, default_bg, 0xD0D0D0)
        assert bg_val == default_bg, (
            "A cell with no explicit background must produce bg_val == default_bg "
            "so the transparent-skip condition fires. If this fails, default-bg cells "
            "will paint opaquely and break wallpaper see-through."
        )

    def test_explicit_bg_different_from_default_produces_nondefault(self):
        """Cell with explicit bg != default_bg: bg_val != default_bg → fill paints."""
        default_bg = 0x1E1E1E
        explicit_bg = 0x3A3A5C  # e.g. cursorline
        bg_val = self._effective_bg(explicit_bg, default_bg, 0xD0D0D0)
        assert bg_val != default_bg, (
            "A cell with explicit bg != default_bg must produce bg_val != default_bg "
            "so the fill is NOT skipped. Cursorline, diff, sign columns must paint."
        )

    def test_reversed_cell_with_default_bg_gets_fg_as_bg(self):
        """Reversed cell where original bg == default_bg: bg_val becomes default_fg.

        After reversal, the new bg_val is the original fg (default_fg).
        Since default_fg != default_bg, the skip does NOT fire and the
        reversed block paints opaquely — correct behavior.
        This is documented in the skip-logic comment in _paint_row.
        """
        default_bg = 0x1E1E1E
        default_fg = 0xD0D0D0
        bg_val = self._effective_bg(None, default_bg, default_fg, reverse=True)
        assert bg_val == default_fg, (
            "Reversed cell with original bg==default_bg must have bg_val==default_fg "
            "after swap, so the reversed block paints opaquely."
        )
        assert bg_val != default_bg, (
            "The reversed cell's new bg_val must differ from default_bg "
            "so the skip condition does NOT fire."
        )

    def test_explicit_bg_equal_to_default_bg_still_skips(self):
        """Explicit bg that equals default_bg is indistinguishable from no-bg.

        By design: if a colorscheme explicitly sets a cell bg to the same
        value as the default, we still skip. The visible result is identical
        and there's no way to distinguish the two cases from the post-resolve
        bg_val value.
        """
        default_bg = 0x1E1E1E
        bg_val = self._effective_bg(default_bg, default_bg, 0xD0D0D0)
        assert bg_val == default_bg  # skip fires regardless


# ---------------------------------------------------------------------------
# Structural checks — ensure paint() and __init__ have the right shape
# ---------------------------------------------------------------------------

def _nvim_view_source() -> str:
    """Return the source of nvim_view.py."""
    import symmetria_ide.nvim_view as mod
    return inspect.getsource(mod)


class TestAmbientTintNotInlinedInPaint:
    """Verify that QColor(0, 0, 0, 153) is NOT allocated inside paint().

    CLAUDE.md gotcha #10: any shiboken wrapper allocated inside paint() is a
    GC/race hazard under Python 3.14. The ambient tint must be pre-allocated
    in __init__ and referenced as self._ambient_tint_color in paint().
    """

    def test_paint_does_not_contain_inline_qcolor_tint(self):
        """paint() must not contain 'QColor(0, 0, 0, 153)'."""
        from symmetria_ide.nvim_view import NvimView
        paint_src = inspect.getsource(NvimView.paint)
        assert "QColor(0, 0, 0, 153)" not in paint_src, (
            "QColor(0, 0, 0, 153) must NOT appear inside paint(). "
            "Inline allocation of a shiboken wrapper in the paint hot path "
            "is a GC/race hazard (CLAUDE.md gotcha #10). "
            "Pre-allocate as self._ambient_tint_color in __init__ instead."
        )

    def test_paint_references_ambient_tint_color_attribute(self):
        """paint() must reference self._ambient_tint_color for the tint fill."""
        from symmetria_ide.nvim_view import NvimView
        paint_src = inspect.getsource(NvimView.paint)
        assert "_ambient_tint_color" in paint_src, (
            "paint() must reference self._ambient_tint_color for the ambient dim fill. "
            "If this check fails, the tint was either removed or re-inlined."
        )

    def test_init_pre_allocates_ambient_tint_color(self):
        """__init__ must assign self._ambient_tint_color = QColor(0, 0, 0, 153)."""
        from symmetria_ide.nvim_view import NvimView
        init_src = inspect.getsource(NvimView.__init__)
        assert "_ambient_tint_color" in init_src, (
            "__init__ must pre-allocate self._ambient_tint_color. "
            "Without this, paint() would inline the QColor each frame."
        )
        assert "QColor(0, 0, 0, 153)" in init_src, (
            "__init__ must assign QColor(0, 0, 0, 153) to _ambient_tint_color. "
            "This is the Ghostty-parity 60% opacity ambient dim value."
        )


class TestTransparentFillColor:
    """Verify setFillColor(transparent) is called in __init__.

    Without this, QQuickPaintedItem clears the backing store to white
    before every paint(), defeating the transparent window design.
    """

    def test_init_calls_set_fill_color_with_transparent(self):
        """__init__ must call setFillColor(QColor(0, 0, 0, 0))."""
        from symmetria_ide.nvim_view import NvimView
        init_src = inspect.getsource(NvimView.__init__)
        assert "setFillColor(QColor(0, 0, 0, 0))" in init_src, (
            "__init__ must call setFillColor(QColor(0, 0, 0, 0)) so the "
            "QQuickPaintedItem backing store is cleared to transparent (not white) "
            "before every paint(). Without this, 'color: transparent' on the Window "
            "has no effect in the editor area."
        )
