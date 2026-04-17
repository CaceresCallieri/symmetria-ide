"""Grid state model.

Holds a 2-D array of `Cell`s and applies NeoVim `redraw` events:
`grid_resize`, `grid_line`, `grid_clear`, `grid_cursor_goto`, `grid_scroll`.
Also tracks `hl_attr_define` so that cells carry a resolved highlight id
the renderer can look up without re-walking attribute definitions.

Pure Python; no Qt dependency — unit-testable in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass
class Cell:
    """Single grid cell: one displayed character and an attribute id.

    `char` is the displayed glyph (may be multi-codepoint grapheme).
    `hl_id` references `Grid.hl_attrs[hl_id]` for colors and style flags.
    """

    char: str = " "
    hl_id: int = 0


@dataclass
class HlAttr:
    """Resolved highlight attributes.

    Mirrors the rgb_attr dict NeoVim emits in `hl_attr_define`.
    """

    foreground: int | None = None
    background: int | None = None
    special: int | None = None
    bold: bool = False
    italic: bool = False
    underline: bool = False
    undercurl: bool = False
    reverse: bool = False


@dataclass
class Grid:
    """Grid of cells + highlight table + cursor position.

    `apply(event_name, args)` mutates state in place based on one redraw
    event. The outer event loop pulls the `flush` event as the signal to
    repaint — Grid itself doesn't know about rendering.
    """

    cols: int = 0
    rows: int = 0
    cells: list[list[Cell]] = field(default_factory=list)
    hl_attrs: dict[int, HlAttr] = field(default_factory=lambda: {0: HlAttr()})
    default_fg: int = 0xD0D0D0
    default_bg: int = 0x1E1E1E
    default_sp: int = 0xFFFFFF
    cursor_row: int = 0
    cursor_col: int = 0
    mode: str = "normal"

    def resize(self, cols: int, rows: int) -> None:
        """Grow/shrink to (cols, rows), preserving overlapping content."""
        new_cells = [[Cell() for _ in range(cols)] for _ in range(rows)]
        for r in range(min(rows, self.rows)):
            for c in range(min(cols, self.cols)):
                new_cells[r][c] = self.cells[r][c]
        self.cols = cols
        self.rows = rows
        self.cells = new_cells

    def clear(self) -> None:
        """Reset every cell to a blank with default highlight."""
        for r in range(self.rows):
            for c in range(self.cols):
                self.cells[r][c] = Cell()

    def apply_line(self, row: int, col_start: int, cells: Iterable[list[Any]]) -> None:
        """Apply a single `grid_line` update.

        Each `cells` entry is `[text, hl_id?, repeat?]` per NeoVim's
        redraw protocol. Repeat applies the previous hl_id if omitted,
        which is why we track `last_hl_id`.
        """
        if row >= self.rows:
            return
        col = col_start
        last_hl_id = 0
        for entry in cells:
            text = entry[0]
            hl_id = entry[1] if len(entry) >= 2 else last_hl_id
            repeat = entry[2] if len(entry) >= 3 else 1
            last_hl_id = hl_id
            for _ in range(repeat):
                if col >= self.cols:
                    break
                self.cells[row][col] = Cell(char=text, hl_id=hl_id)
                col += 1

    def scroll(self, top: int, bot: int, left: int, right: int, rows: int) -> None:
        """Scroll the rectangle `[top, bot) × [left, right)` by `rows`.

        Positive `rows` scrolls content UP (content at `top+rows` moves
        to `top`); negative scrolls DOWN. Cells scrolled out of the
        region are discarded; cells scrolled in are left untouched
        (NeoVim sends a `grid_line` to repaint them afterward).

        NeoVim's `grid_scroll` event also carries a `cols` arg that is
        always `right - left`; we drop it rather than re-pass it.
        """
        if rows > 0:
            for r in range(top, bot - rows):
                for c in range(left, right):
                    self.cells[r][c] = self.cells[r + rows][c]
        elif rows < 0:
            for r in range(bot - 1, top - rows - 1, -1):
                for c in range(left, right):
                    self.cells[r][c] = self.cells[r + rows][c]

    def set_cursor(self, row: int, col: int) -> None:
        self.cursor_row = max(0, min(row, self.rows - 1 if self.rows else 0))
        self.cursor_col = max(0, min(col, self.cols - 1 if self.cols else 0))

    def define_hl(self, hl_id: int, rgb_attr: dict) -> None:
        self.hl_attrs[hl_id] = HlAttr(
            foreground=rgb_attr.get("foreground"),
            background=rgb_attr.get("background"),
            special=rgb_attr.get("special"),
            bold=bool(rgb_attr.get("bold", False)),
            italic=bool(rgb_attr.get("italic", False)),
            underline=bool(rgb_attr.get("underline", False)),
            undercurl=bool(rgb_attr.get("undercurl", False)),
            reverse=bool(rgb_attr.get("reverse", False)),
        )

    def set_default_colors(self, fg: int, bg: int, sp: int) -> None:
        self.default_fg = fg
        self.default_bg = bg
        self.default_sp = sp
