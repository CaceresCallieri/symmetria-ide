"""Qt key event → NeoVim key notation translator.

NeoVim's `nvim_input` takes strings in its own keycode notation:
`<CR>`, `<Esc>`, `<C-a>`, `<S-Tab>`, etc. This module converts Qt key
events to that format so the NvimView can pipe keystrokes through.

Kept separate from the view so it's trivially unit-testable without
spawning Qt.
"""

from __future__ import annotations

from PySide6.QtCore import Qt


# Qt.Key → NeoVim key name (wrapped in <...> when emitted).
_SPECIAL_KEYS: dict[int, str] = {
    int(Qt.Key.Key_Escape): "Esc",
    int(Qt.Key.Key_Tab): "Tab",
    int(Qt.Key.Key_Backtab): "S-Tab",
    int(Qt.Key.Key_Backspace): "BS",
    int(Qt.Key.Key_Return): "CR",
    int(Qt.Key.Key_Enter): "CR",
    int(Qt.Key.Key_Insert): "Insert",
    int(Qt.Key.Key_Delete): "Del",
    int(Qt.Key.Key_Pause): "Pause",
    int(Qt.Key.Key_Print): "Print",
    int(Qt.Key.Key_SysReq): "SysReq",
    int(Qt.Key.Key_Home): "Home",
    int(Qt.Key.Key_End): "End",
    int(Qt.Key.Key_Left): "Left",
    int(Qt.Key.Key_Up): "Up",
    int(Qt.Key.Key_Right): "Right",
    int(Qt.Key.Key_Down): "Down",
    int(Qt.Key.Key_PageUp): "PageUp",
    int(Qt.Key.Key_PageDown): "PageDown",
    int(Qt.Key.Key_F1): "F1",
    int(Qt.Key.Key_F2): "F2",
    int(Qt.Key.Key_F3): "F3",
    int(Qt.Key.Key_F4): "F4",
    int(Qt.Key.Key_F5): "F5",
    int(Qt.Key.Key_F6): "F6",
    int(Qt.Key.Key_F7): "F7",
    int(Qt.Key.Key_F8): "F8",
    int(Qt.Key.Key_F9): "F9",
    int(Qt.Key.Key_F10): "F10",
    int(Qt.Key.Key_F11): "F11",
    int(Qt.Key.Key_F12): "F12",
    int(Qt.Key.Key_Space): "Space",
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


def translate(key: int, text: str, modifiers: Qt.KeyboardModifier) -> str | None:
    """Translate a Qt key event to NeoVim key notation.

    Returns None when the event should be ignored (e.g. modifier-only
    presses, unmapped combinations). The caller passes the result
    directly to `nvim_input`.
    """
    if key in _MODIFIER_ONLY_KEYS:
        return None

    ctrl = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
    alt = bool(modifiers & Qt.KeyboardModifier.AltModifier)
    meta = bool(modifiers & Qt.KeyboardModifier.MetaModifier)
    shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)

    special = _SPECIAL_KEYS.get(key)

    if special is not None:
        parts: list[str] = []
        if ctrl:
            parts.append("C")
        if alt:
            parts.append("M")
        if meta:
            parts.append("D")
        # Shift is encoded explicitly on special keys (printable text
        # applies shift directly in `text`).
        if shift and special not in {"S-Tab"}:
            parts.append("S")
        parts.append(special)
        return "<" + "-".join(parts) + ">"

    # Printable path: prefer the already-shifted text from Qt.
    if text:
        char = text
        if len(char) == 1 and ord(char) < 0x20:
            # Qt gives control-char codepoints (e.g. Ctrl-A → \x01) in
            # `text`. Convert back to the letter + explicit C- prefix.
            # Special case: \x00 is Ctrl+@ (also Ctrl+Space on some
            # layouts). chr(0 + 0x60) = '`' which is wrong — NeoVim
            # wants "<C-Space>" for this combination.
            if ord(char) == 0:
                return "<C-Space>"
            letter = chr(ord(char) + 0x60)
            return f"<C-{letter}>"
        if ctrl or alt or meta:
            parts = []
            if ctrl:
                parts.append("C")
            if alt:
                parts.append("M")
            if meta:
                parts.append("D")
            parts.append(char)
            return "<" + "-".join(parts) + ">"
        if char == "<":
            return "<LT>"
        return char

    return None
