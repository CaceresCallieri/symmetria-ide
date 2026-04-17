"""Unit tests for CmdlineState, CompletionModel, and PopupmenuModel.

These models carry non-trivial signal-emission logic (change-guarded
property setters, `beginResetModel`/`endResetModel` lifecycle) that is
easy to break silently. Tests here cover the happy path and the
hide-branch edge cases.

Note: PySide6 requires a QCoreApplication to exist before instantiating
QObject subclasses. A module-level fixture creates one for the session.
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


# ---------------------------------------------------------------------------
# CmdlineState
# ---------------------------------------------------------------------------

class TestCmdlineState:
    def _make(self):
        from symmetria_ide.app import CmdlineState
        return CmdlineState()

    def test_initial_state_is_hidden(self):
        state = self._make()
        assert state.visible is False
        assert state.text == ""
        assert state.cursorPos == 0
        assert state.firstchar == ""
        assert state.prompt == ""
        assert state.level == 0

    def test_show_sets_visible_and_fields(self):
        state = self._make()
        signals = []
        state.visibleChanged.connect(lambda: signals.append("visible"))
        state.textChanged.connect(lambda: signals.append("text"))

        state.apply({
            "kind": "show",
            "text": "e!",
            "pos": 2,
            "firstchar": ":",
            "prompt": "",
            "level": 1,
        })

        assert state.visible is True
        assert state.text == "e!"
        assert state.cursorPos == 2
        assert state.firstchar == ":"
        assert state.level == 1
        assert "visible" in signals
        assert "text" in signals

    def test_show_does_not_re_emit_unchanged_fields(self):
        state = self._make()
        state.apply({"kind": "show", "text": "x", "pos": 0, "firstchar": ":", "prompt": "", "level": 0})

        signals = []
        state.textChanged.connect(lambda: signals.append("text"))
        state.visibleChanged.connect(lambda: signals.append("visible"))

        # Apply again with same data
        state.apply({"kind": "show", "text": "x", "pos": 0, "firstchar": ":", "prompt": "", "level": 0})

        assert "text" not in signals
        assert "visible" not in signals

    def test_pos_event_updates_cursor(self):
        state = self._make()
        state.apply({"kind": "show", "text": "hello", "pos": 0, "firstchar": ":", "prompt": "", "level": 0})
        signals = []
        state.cursorPosChanged.connect(lambda: signals.append("pos"))

        state.apply({"kind": "pos", "pos": 3, "level": 0})

        assert state.cursorPos == 3
        assert "pos" in signals

    def test_hide_clears_all_fields(self):
        state = self._make()
        state.apply({"kind": "show", "text": "write", "pos": 5, "firstchar": ":", "prompt": "", "level": 2})

        state.apply({"kind": "hide", "level": 2})

        assert state.visible is False
        assert state.text == ""
        assert state.cursorPos == 0
        assert state.firstchar == ""
        assert state.level == 0

    def test_hide_emits_level_changed_signal(self):
        state = self._make()
        state.apply({"kind": "show", "text": "", "pos": 0, "firstchar": "=", "prompt": "", "level": 1})

        signals = []
        state.levelChanged.connect(lambda: signals.append("level"))
        state.apply({"kind": "hide", "level": 1})

        assert "level" in signals
        assert state.level == 0

    def test_hide_does_not_double_emit_when_already_hidden(self):
        state = self._make()
        signals = []
        state.visibleChanged.connect(lambda: signals.append("visible"))

        state.apply({"kind": "hide", "level": 0})

        assert "visible" not in signals


# ---------------------------------------------------------------------------
# CompletionModel
# ---------------------------------------------------------------------------

class TestCompletionModel:
    def _make(self):
        from symmetria_ide.app import CompletionModel
        return CompletionModel()

    def test_initial_state(self):
        model = self._make()
        assert model.rowCount() == 0
        assert model.visible is False
        assert model.selected == -1

    def test_apply_with_items_shows_model(self):
        model = self._make()
        model.apply({"items": ["edit", "enew", "echo"], "selected": -1})

        assert model.rowCount() == 3
        assert model.visible is True
        assert model.selected == -1

    def test_apply_empty_hides_model(self):
        model = self._make()
        model.apply({"items": ["edit"], "selected": -1})
        model.apply({"items": [], "selected": -1})

        assert model.rowCount() == 0
        assert model.visible is False

    def test_apply_updates_selected(self):
        model = self._make()
        model.apply({"items": ["edit", "enew"], "selected": -1})

        signals = []
        model.selectedChanged.connect(lambda: signals.append("selected"))
        model.apply({"items": ["edit", "enew"], "selected": 0})

        assert model.selected == 0
        assert "selected" in signals

    def test_apply_same_items_skips_model_reset(self):
        """When only selected changes, no beginResetModel should fire.

        We verify this indirectly: rowCount stays correct and no crash occurs.
        """
        model = self._make()
        model.apply({"items": ["edit", "enew"], "selected": -1})
        # Same items, only selected changes — should not reset model
        model.apply({"items": ["edit", "enew"], "selected": 1})

        assert model.rowCount() == 2
        assert model.selected == 1

    def test_word_role_data(self):
        from PySide6.QtCore import QModelIndex
        from symmetria_ide.app import CompletionModel
        model = CompletionModel()
        model.apply({"items": ["edit", "enew"], "selected": -1})

        idx = model.index(0)
        assert model.data(idx, CompletionModel.WordRole) == "edit"

        idx = model.index(1)
        assert model.data(idx, CompletionModel.WordRole) == "enew"

    def test_data_invalid_index_returns_none(self):
        model = self._make()
        model.apply({"items": ["edit"], "selected": -1})

        from PySide6.QtCore import QModelIndex
        assert model.data(QModelIndex(), model.WordRole) is None


# ---------------------------------------------------------------------------
# PopupmenuModel
# ---------------------------------------------------------------------------

class TestPopupmenuModel:
    def _make(self):
        from symmetria_ide.app import PopupmenuModel
        return PopupmenuModel()

    def test_initial_state(self):
        model = self._make()
        assert model.rowCount() == 0
        assert model.visible is False
        assert model.selected == -1

    def test_show_populates_and_shows(self):
        model = self._make()
        model.apply({
            "kind": "show",
            "items": [{"word": "foo", "kind": "f", "menu": ""},
                      {"word": "bar", "kind": "f", "menu": ""}],
            "selected": 0,
        })

        assert model.rowCount() == 2
        assert model.visible is True
        assert model.selected == 0

    def test_select_updates_selected(self):
        model = self._make()
        model.apply({
            "kind": "show",
            "items": [{"word": "a", "kind": "", "menu": ""},
                      {"word": "b", "kind": "", "menu": ""}],
            "selected": -1,
        })
        signals = []
        model.selectedChanged.connect(lambda: signals.append("selected"))

        model.apply({"kind": "select", "selected": 1})

        assert model.selected == 1
        assert "selected" in signals

    def test_hide_clears_model(self):
        model = self._make()
        model.apply({
            "kind": "show",
            "items": [{"word": "x", "kind": "", "menu": ""}],
            "selected": 0,
        })
        model.apply({"kind": "hide"})

        assert model.rowCount() == 0
        assert model.visible is False
        assert model.selected == -1

    def test_word_kind_menu_roles(self):
        from symmetria_ide.app import PopupmenuModel
        model = PopupmenuModel()
        model.apply({
            "kind": "show",
            "items": [{"word": "fn", "kind": "function", "menu": "mod"}],
            "selected": -1,
        })
        idx = model.index(0)
        assert model.data(idx, PopupmenuModel.WordRole) == "fn"
        assert model.data(idx, PopupmenuModel.KindRole) == "function"
        assert model.data(idx, PopupmenuModel.MenuRole) == "mod"
