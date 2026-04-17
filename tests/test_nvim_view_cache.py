"""Unit tests for _rgb_to_qcolor memoization in nvim_view.

The QColor cache is the decisive fix for the Python 3.14 GC/render-thread
SIGSEGV (see CLAUDE.md gotcha #10). These tests act as a regression net:
if the cache is accidentally removed or its key logic is changed, the
behavior tests below will fail before the race can resurface.

Note: QColor requires a QCoreApplication to exist. A module-level fixture
creates one for the session (same pattern as test_app_models.py).
"""

from __future__ import annotations

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
    """Clear the module-level QColor cache before each test.

    The cache is a module singleton — tests pollute each other without this.
    """
    from symmetria_ide.nvim_view import _qcolor_cache
    _qcolor_cache.clear()
    yield
    _qcolor_cache.clear()


class TestRgbToQcolorCache:
    def test_same_key_returns_identical_object(self):
        """Cache hit: identical (value, fallback) returns the same QColor instance.

        This is the load-bearing property: same object = no new shiboken wrapper
        = no GC pressure from repeated paint calls.
        """
        from symmetria_ide.nvim_view import _rgb_to_qcolor
        c1 = _rgb_to_qcolor(0xFF0000, 0x000000)
        c2 = _rgb_to_qcolor(0xFF0000, 0x000000)
        assert c1 is c2

    def test_none_value_uses_fallback_rgb(self):
        """When value is None, the fallback int is used to construct the QColor."""
        from symmetria_ide.nvim_view import _rgb_to_qcolor
        color = _rgb_to_qcolor(None, 0x00FF00)
        assert color.red() == 0
        assert color.green() == 255
        assert color.blue() == 0

    def test_value_wins_over_fallback(self):
        """When value is not None, it determines the color and fallback is ignored."""
        from symmetria_ide.nvim_view import _rgb_to_qcolor
        color = _rgb_to_qcolor(0xFF0000, 0x0000FF)
        assert color.red() == 255
        assert color.green() == 0
        assert color.blue() == 0

    def test_distinct_keys_produce_distinct_objects(self):
        """Different (value, fallback) pairs cache independently."""
        from symmetria_ide.nvim_view import _rgb_to_qcolor
        red = _rgb_to_qcolor(0xFF0000, 0x000000)
        blue = _rgb_to_qcolor(0x0000FF, 0x000000)
        assert red is not blue
        assert red.red() == 255
        assert blue.blue() == 255

    def test_cache_is_populated_after_first_call(self):
        """The cache dict contains an entry after the first unique call."""
        from symmetria_ide.nvim_view import _rgb_to_qcolor, _qcolor_cache
        assert len(_qcolor_cache) == 0
        _rgb_to_qcolor(0x123456, 0x000000)
        assert len(_qcolor_cache) == 1

    def test_24bit_rgb_decomposition_correct(self):
        """Verify R/G/B channel extraction for a known 24-bit value."""
        from symmetria_ide.nvim_view import _rgb_to_qcolor
        # 0xAABBCC: R=0xAA=170, G=0xBB=187, B=0xCC=204
        color = _rgb_to_qcolor(0xAABBCC, 0x000000)
        assert color.red() == 0xAA
        assert color.green() == 0xBB
        assert color.blue() == 0xCC
