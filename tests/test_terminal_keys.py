"""Tests for the Qt → terminal escape-sequence key translator.

Pure-function tests — no Qt event loop, no instantiation. Each row in
the parametrize table is one (key, text, modifiers) → expected-bytes
contract. If a future xterm-compat issue surfaces (a key behaving
differently in vim than expected), update the table here first; the
test failure will guide the fix in `terminal_keys.translate`.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt

from symmetria_ide.terminal_keys import translate


# ---------------------------------------------------------------------------
# Special keys — Escape, Tab, navigation, function keys.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key, expected",
    [
        (Qt.Key.Key_Escape, b"\x1b"),
        (Qt.Key.Key_Tab, b"\t"),
        (Qt.Key.Key_Backtab, b"\x1b[Z"),
        (Qt.Key.Key_Backspace, b"\x7f"),
        (Qt.Key.Key_Return, b"\r"),
        (Qt.Key.Key_Enter, b"\r"),
        (Qt.Key.Key_Insert, b"\x1b[2~"),
        (Qt.Key.Key_Delete, b"\x1b[3~"),
        (Qt.Key.Key_Home, b"\x1b[H"),
        (Qt.Key.Key_End, b"\x1b[F"),
        (Qt.Key.Key_Up, b"\x1b[A"),
        (Qt.Key.Key_Down, b"\x1b[B"),
        (Qt.Key.Key_Right, b"\x1b[C"),
        (Qt.Key.Key_Left, b"\x1b[D"),
        (Qt.Key.Key_PageUp, b"\x1b[5~"),
        (Qt.Key.Key_PageDown, b"\x1b[6~"),
    ],
)
def test_special_keys_no_modifier(key, expected):
    """Bare special-key press produces the xterm normal-mode sequence."""
    assert translate(int(key), "", Qt.KeyboardModifier.NoModifier) == expected


@pytest.mark.parametrize(
    "key, expected",
    [
        (Qt.Key.Key_F1, b"\x1bOP"),
        (Qt.Key.Key_F2, b"\x1bOQ"),
        (Qt.Key.Key_F3, b"\x1bOR"),
        (Qt.Key.Key_F4, b"\x1bOS"),
        (Qt.Key.Key_F5, b"\x1b[15~"),
        (Qt.Key.Key_F6, b"\x1b[17~"),
        (Qt.Key.Key_F7, b"\x1b[18~"),
        (Qt.Key.Key_F8, b"\x1b[19~"),
        (Qt.Key.Key_F9, b"\x1b[20~"),
        (Qt.Key.Key_F10, b"\x1b[21~"),
        (Qt.Key.Key_F11, b"\x1b[23~"),
        (Qt.Key.Key_F12, b"\x1b[24~"),
    ],
)
def test_function_keys(key, expected):
    """F1-F12 emit the xterm function-key sequences (OP/OQ/OR/OS for
    F1-F4, then CSI <n> ~ for F5+)."""
    assert translate(int(key), "", Qt.KeyboardModifier.NoModifier) == expected


# ---------------------------------------------------------------------------
# Printable characters — including the shifted forms.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("a", b"a"),
        ("Z", b"Z"),  # Shift handled by Qt — `text` already has the shifted form
        ("1", b"1"),
        ("!", b"!"),
        (" ", b" "),
        ("é", "é".encode("utf-8")),
        ("漢", "漢".encode("utf-8")),
    ],
)
def test_printable_chars(text, expected):
    """Printable chars (including UTF-8 multi-byte) pass through as
    their UTF-8 encoding. Shift modifier doesn't change the result
    because Qt already produces the shifted character in `text`."""
    assert (
        translate(int(Qt.Key.Key_unknown), text, Qt.KeyboardModifier.NoModifier)
        == expected
    )


# ---------------------------------------------------------------------------
# Control codes — Qt encodes Ctrl+letter into `text` as the corresponding
# C0 control character. The translator forwards it unchanged.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("\x01", b"\x01"),  # Ctrl+A
        ("\x03", b"\x03"),  # Ctrl+C (SIGINT)
        ("\x04", b"\x04"),  # Ctrl+D (EOF)
        ("\x1a", b"\x1a"),  # Ctrl+Z (SIGTSTP)
    ],
)
def test_control_codes_passthrough(text, expected):
    """Ctrl+letter combinations: Qt populates `text` with the control
    character; we forward it as-is. The terminal does the right thing
    because that's the same byte the shell expects on a real tty."""
    assert (
        translate(
            int(Qt.Key.Key_A),
            text,
            Qt.KeyboardModifier.ControlModifier,
        )
        == expected
    )


# ---------------------------------------------------------------------------
# Alt / Meta — xterm "meta sends escape" convention.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("a", b"\x1ba"),
        ("z", b"\x1bz"),
        (".", b"\x1b."),
    ],
)
def test_alt_printable_prepends_escape(text, expected):
    """Alt+printable sends ESC prefix per xterm meta convention."""
    assert (
        translate(int(Qt.Key.Key_A), text, Qt.KeyboardModifier.AltModifier) == expected
    )


def test_alt_special_prepends_escape():
    """Alt+special (e.g. Alt+Up) also prepends ESC to the sequence."""
    assert (
        translate(int(Qt.Key.Key_Up), "", Qt.KeyboardModifier.AltModifier)
        == b"\x1b\x1b[A"
    )


# ---------------------------------------------------------------------------
# Modifier-only keys and unmapped events — return None.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        Qt.Key.Key_Shift,
        Qt.Key.Key_Control,
        Qt.Key.Key_Alt,
        Qt.Key.Key_Meta,
        Qt.Key.Key_CapsLock,
        Qt.Key.Key_NumLock,
        Qt.Key.Key_ScrollLock,
    ],
)
def test_modifier_only_press_returns_none(key):
    """Bare modifier presses should be ignored — the terminal sees
    only the eventual combined keystroke, not the modifier-down event."""
    assert translate(int(key), "", Qt.KeyboardModifier.NoModifier) is None


def test_empty_text_no_special_returns_none():
    """Unmapped key with no text payload — translator can't produce
    anything sensible. Caller (`keyPressEvent`) should `event.ignore()`."""
    assert (
        translate(int(Qt.Key.Key_unknown), "", Qt.KeyboardModifier.NoModifier) is None
    )
