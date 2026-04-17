"""Tests for the pure-Python Grid model.

The Grid is the part of the backend most likely to regress silently —
incorrect scroll or grid_line handling produces wrong cells without
crashing, and that's only visible at runtime. These unit tests lock in
the expected behavior for the redraw operations Phase 0 relies on.
"""

from __future__ import annotations

from symmetria_ide.grid import Cell, Grid


def make_grid(cols: int = 5, rows: int = 3) -> Grid:
    g = Grid()
    g.resize(cols, rows)
    return g


def test_resize_initializes_blank_cells():
    g = make_grid(3, 2)
    assert g.cols == 3
    assert g.rows == 2
    assert all(cell.char == " " for row in g.cells for cell in row)


def test_resize_preserves_overlap():
    g = make_grid(4, 2)
    g.apply_line(0, 0, [["A", 1], ["B", 1]])
    g.resize(2, 1)
    assert g.cells[0][0].char == "A"
    assert g.cells[0][1].char == "B"


def test_apply_line_writes_cells_with_repeat():
    g = make_grid(5, 1)
    g.apply_line(0, 0, [["x", 2, 3]])
    assert [c.char for c in g.cells[0]] == ["x", "x", "x", " ", " "]
    assert g.cells[0][0].hl_id == 2


def test_apply_line_reuses_previous_hl_id_when_repeat_follows():
    g = make_grid(5, 1)
    g.apply_line(0, 0, [["a", 7], ["b"], ["c"]])
    assert [c.char for c in g.cells[0]] == ["a", "b", "c", " ", " "]
    assert [c.hl_id for c in g.cells[0][:3]] == [7, 7, 7]


def test_scroll_up_moves_content_up():
    g = make_grid(3, 4)
    for r, ch in enumerate("ABCD"):
        g.apply_line(r, 0, [[ch, 0, 3]])
    g.scroll(top=0, bot=4, left=0, right=3, rows=1)
    assert g.cells[0][0].char == "B"
    assert g.cells[1][0].char == "C"
    assert g.cells[2][0].char == "D"


def test_scroll_down_moves_content_down():
    g = make_grid(3, 4)
    for r, ch in enumerate("ABCD"):
        g.apply_line(r, 0, [[ch, 0, 3]])
    g.scroll(top=0, bot=4, left=0, right=3, rows=-1)
    assert g.cells[1][0].char == "A"
    assert g.cells[2][0].char == "B"
    assert g.cells[3][0].char == "C"


def test_clear_resets_all_cells():
    g = make_grid(2, 2)
    g.apply_line(0, 0, [["x", 1, 2]])
    g.clear()
    assert all(cell == Cell() for row in g.cells for cell in row)


def test_set_cursor_clamps_to_bounds():
    g = make_grid(3, 2)
    g.set_cursor(99, 99)
    assert g.cursor_row == 1
    assert g.cursor_col == 2


def test_define_hl_stores_rgb_and_flags():
    g = make_grid()
    g.define_hl(5, {"foreground": 0xFF0000, "bold": True, "italic": False})
    attr = g.hl_attrs[5]
    assert attr.foreground == 0xFF0000
    assert attr.bold is True
    assert attr.italic is False
