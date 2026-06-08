"""Qt key event → terminal escape sequence translator.

Terminals consume raw bytes, not nvim-keycode strings. This module
converts Qt key events to the byte sequences that xterm-class
terminals send to their child PTY. `TerminalView.keyPressEvent`
calls this and forwards the result to `TerminalBackend.write()`.

Kept separate from the view so it's trivially unit-testable without
spawning Qt — same shape as `keys.py` for the nvim side.

Reference: xterm Control Sequences,
https://invisible-island.net/xterm/ctlseqs/ctlseqs.html
The sequences here are the "normal cursor mode" set — application
mode (DECCKM enabled by TUIs like vim/less) sends slightly different
codes for arrows + Home/End. A mode-aware variant is a v2 follow-up
once we observe how often application mode actually matters in
practice (most shells stay in normal mode; vim flips on entry and
back on exit, so its own arrow handling masks the difference).
"""

from __future__ import annotations

from PySide6.QtCore import Qt


# Qt.Key → terminal escape sequence bytes for non-printable keys.
# xterm "normal cursor mode" set. Function keys use the
# `ESC O <letter>` (F1-F4) / `ESC [ <n> ~` (F5+) conventions xterm
# uses by default — gnome-terminal, kitty, alacritty all match.
_SPECIAL_KEY_BYTES: dict[int, bytes] = {
    int(Qt.Key.Key_Escape): b"\x1b",
    int(Qt.Key.Key_Tab): b"\t",
    # Shift+Tab — xterm's "back-tab" code.
    int(Qt.Key.Key_Backtab): b"\x1b[Z",
    # Backspace sends DEL (0x7f) — what modern shells (bash, zsh, fish)
    # expect by default. Sending BS (0x08) was the convention on hardware
    # terminals but causes "^H" to echo in default-configured shells.
    int(Qt.Key.Key_Backspace): b"\x7f",
    int(Qt.Key.Key_Return): b"\r",
    int(Qt.Key.Key_Enter): b"\r",
    int(Qt.Key.Key_Insert): b"\x1b[2~",
    int(Qt.Key.Key_Delete): b"\x1b[3~",
    int(Qt.Key.Key_PageUp): b"\x1b[5~",
    int(Qt.Key.Key_PageDown): b"\x1b[6~",
    int(Qt.Key.Key_F1): b"\x1bOP",
    int(Qt.Key.Key_F2): b"\x1bOQ",
    int(Qt.Key.Key_F3): b"\x1bOR",
    int(Qt.Key.Key_F4): b"\x1bOS",
    int(Qt.Key.Key_F5): b"\x1b[15~",
    int(Qt.Key.Key_F6): b"\x1b[17~",
    int(Qt.Key.Key_F7): b"\x1b[18~",
    int(Qt.Key.Key_F8): b"\x1b[19~",
    int(Qt.Key.Key_F9): b"\x1b[20~",
    int(Qt.Key.Key_F10): b"\x1b[21~",
    int(Qt.Key.Key_F11): b"\x1b[23~",
    int(Qt.Key.Key_F12): b"\x1b[24~",
}


# Cursor keys (arrows + Home/End) get mode- and modifier-aware encoding,
# so they're handled separately from `_SPECIAL_KEY_BYTES`. The value is the
# final byte of the escape sequence; the prefix is chosen at translate time:
#   - normal mode (DECCKM off):  ESC [ <final>        (CSI)
#   - application mode (DECCKM): ESC O <final>        (SS3) — set by nvim/less
#   - any modifier held:         ESC [ 1 ; <n> <final>  (xterm modifyCursorKeys)
# nvim flips DECCKM on entry, so without the SS3 form its arrow keys misbehave
# in insert mode; the modifier form carries <C-Left>/<S-Up>/<M-Down> etc.
_CURSOR_KEY_FINAL: dict[int, bytes] = {
    int(Qt.Key.Key_Up): b"A",
    int(Qt.Key.Key_Down): b"B",
    int(Qt.Key.Key_Right): b"C",
    int(Qt.Key.Key_Left): b"D",
    int(Qt.Key.Key_Home): b"H",
    int(Qt.Key.Key_End): b"F",
}


_MODIFIER_ONLY_KEYS = {
    int(Qt.Key.Key_Shift),
    int(Qt.Key.Key_Control),
    int(Qt.Key.Key_Alt),
    int(Qt.Key.Key_Meta),
    int(Qt.Key.Key_AltGr),
    int(Qt.Key.Key_CapsLock),
    int(Qt.Key.Key_NumLock),
    int(Qt.Key.Key_ScrollLock),
}


def translate(
    key: int,
    text: str,
    modifiers: Qt.KeyboardModifier,
    app_cursor_keys: bool = False,
) -> bytes | None:
    """Translate a Qt key event to terminal-bound bytes.

    Returns None when the event should be ignored (modifier-only
    press, unmapped combination). The caller passes the result
    directly to `TerminalBackend.write()`.

    `app_cursor_keys` reflects the child's DECCKM state (private mode 1),
    read from `TerminalBackend.application_cursor_keys`. When True, the
    arrow keys + Home/End use SS3 (`ESC O x`) instead of CSI (`ESC [ x`)
    — nvim, less, and most full-screen TUIs flip DECCKM on entry and
    expect SS3, so without this their arrow keys break in insert mode.

    Qt already encodes Ctrl+letter into `text` as the corresponding
    control character (Ctrl+A → '\\x01', Ctrl+C → '\\x03', etc.) —
    we forward it unchanged. That's exactly what the terminal expects,
    so no special handling is needed.

    Alt+key uses the xterm "meta sends escape" convention: prepend
    `ESC` (0x1b) to the key's normal bytes. Cursor keys with ANY
    modifier (Ctrl/Shift/Alt) instead use the xterm modifyCursorKeys
    CSI form `ESC [ 1 ; n x` (n = 1 + shift + 2·alt + 4·ctrl), which is
    what nvim decodes as <C-Left>/<S-Up>/<M-Down> etc.
    """
    if key in _MODIFIER_ONLY_KEYS:
        return None

    alt = bool(modifiers & Qt.KeyboardModifier.AltModifier)
    ctrl = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
    shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)

    cursor_final = _CURSOR_KEY_FINAL.get(key)
    if cursor_final is not None:
        if shift or ctrl or alt:
            n = 1 + (1 if shift else 0) + (2 if alt else 0) + (4 if ctrl else 0)
            return b"\x1b[1;" + str(n).encode("ascii") + cursor_final
        if app_cursor_keys:
            return b"\x1bO" + cursor_final
        return b"\x1b[" + cursor_final

    special = _SPECIAL_KEY_BYTES.get(key)
    if special is not None:
        # Alt+special: ESC prefix per xterm meta convention.
        if alt:
            return b"\x1b" + special
        return special

    if text:
        encoded = text.encode("utf-8")
        if alt:
            return b"\x1b" + encoded
        return encoded

    return None
