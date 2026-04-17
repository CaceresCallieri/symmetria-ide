"""Tests for the Qt → NeoVim key translator.

Covers the common paths: printable letters, special keys, Ctrl-letter,
Shift-Tab, the `<` escape to `<LT>`, and modifier-only events returning
None.
"""

from __future__ import annotations

from PySide6.QtCore import Qt

from symmetria_ide.keys import translate


NO_MOD = Qt.KeyboardModifier.NoModifier
CTRL = Qt.KeyboardModifier.ControlModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier
ALT = Qt.KeyboardModifier.AltModifier


def test_printable_letter_passes_through():
    assert translate(int(Qt.Key.Key_A), "a", NO_MOD) == "a"


def test_shifted_letter_uses_text():
    assert translate(int(Qt.Key.Key_A), "A", SHIFT) == "A"


def test_enter_becomes_cr():
    assert translate(int(Qt.Key.Key_Return), "\r", NO_MOD) == "<CR>"


def test_escape_becomes_esc():
    assert translate(int(Qt.Key.Key_Escape), "", NO_MOD) == "<Esc>"


def test_backspace_becomes_bs():
    assert translate(int(Qt.Key.Key_Backspace), "\b", NO_MOD) == "<BS>"


def test_ctrl_letter_emits_c_prefix():
    # Qt reports control-a with text == "\x01"; we should round-trip to
    # <C-a>, NOT the raw control character.
    assert translate(int(Qt.Key.Key_A), "\x01", CTRL) == "<C-a>"


def test_shift_tab_emits_s_tab():
    assert translate(int(Qt.Key.Key_Backtab), "", SHIFT) == "<S-Tab>"


def test_arrow_keys():
    assert translate(int(Qt.Key.Key_Left), "", NO_MOD) == "<Left>"
    assert translate(int(Qt.Key.Key_Right), "", NO_MOD) == "<Right>"
    assert translate(int(Qt.Key.Key_Up), "", NO_MOD) == "<Up>"
    assert translate(int(Qt.Key.Key_Down), "", NO_MOD) == "<Down>"


def test_ctrl_shift_arrow_carries_modifiers():
    assert translate(int(Qt.Key.Key_Left), "", CTRL | SHIFT) == "<C-S-Left>"


def test_less_than_is_escaped():
    assert translate(int(Qt.Key.Key_Less), "<", NO_MOD) == "<LT>"


def test_modifier_only_event_returns_none():
    assert translate(int(Qt.Key.Key_Shift), "", SHIFT) is None
    assert translate(int(Qt.Key.Key_Control), "", CTRL) is None


def test_alt_letter():
    assert translate(int(Qt.Key.Key_X), "x", ALT) == "<M-x>"
