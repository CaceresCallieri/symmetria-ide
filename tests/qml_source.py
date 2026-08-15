"""Shared source-level helpers for structural QML tests."""

from __future__ import annotations


def extract_braced_body(source: str, declaration_start: int) -> str:
    """Return a QML declaration body without relying on a character window.

    Braces inside line comments and JavaScript string literals do not affect
    the nesting depth. Raises ``AssertionError`` when the body is unbalanced.
    """
    opening = source.index("{", declaration_start)
    depth = 0
    quote: str | None = None
    index = opening
    while index < len(source):
        character = source[index]
        if quote is not None:
            if character == "\\":
                index += 2
                continue
            if character == quote:
                quote = None
        elif character in "'\"`":
            quote = character
        elif character == "/" and source[index + 1 : index + 2] == "/":
            newline = source.find("\n", index)
            index = len(source) if newline < 0 else newline
            continue
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return source[opening : index + 1]
        index += 1
    raise AssertionError(
        f"unbalanced QML braces from declaration at index {declaration_start}"
    )
