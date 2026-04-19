"""Tests for pooled QRectF + list buffers in the paint hot path.

Closes issues #1 (QRectF pool) and #2 (run_chars list pool). Both target
the gotcha #10 allocation class: any shiboken wrapper (or list/tuple
tracked by the cyclic GC) allocated inside paint() can SEGV the Qt
render thread when the pynvim worker thread simultaneously triggers a
cyclic GC pass on Python 3.14.

The fixes keep a single long-lived instance of each wrapper/list on
`NvimView` and mutate it in place inside the hot loop:

    - `_run_rect`     — reused per highlight run (via `_flush_run`)
    - `_clip_rect`    — reused per frame (paint()'s exact-grid clip)
    - `_cursor_rect`  — reused per frame (three cursor shape branches)
    - `_run_chars`    — reused per run (cleared + extended in _paint_row)

These tests are structural (source-inspection) rather than behavioral —
instantiating NvimView requires a full QGuiApplication + QFontDatabase,
which in turn needs a display. The existing test_transparent_mode.py
uses this same pattern for similar gotcha-guard tests (see that file's
module docstring for the rationale).

If any of these checks fail, the regression is: a future refactor
re-introduced a fresh QRectF / list allocation inside the paint hot
path, exposing us to the render-thread SEGV class of bug.
"""

from __future__ import annotations

import inspect

from conftest import construction_source


class TestPooledRectsInConstruction:
    """Construction must pre-allocate the three pooled QRectF instances.

    Without these pools, `paint()` / `_flush_run` / `_paint_cursor` would
    fall back to fresh `QRectF(...)` per call — re-introducing the gotcha
    #10 allocation hazard that this sprint eliminates.
    """

    def test_run_rect_is_pre_allocated(self):
        from symmetria_ide.nvim_view import NvimView

        src = construction_source(NvimView)
        assert "self._run_rect = QRectF()" in src, (
            "Construction must pre-allocate `self._run_rect = QRectF()`. "
            "_flush_run relies on receiving this pooled rect; if it's gone, "
            "the hot path will allocate per run again (issue #1)."
        )

    def test_clip_rect_is_pre_allocated(self):
        from symmetria_ide.nvim_view import NvimView

        src = construction_source(NvimView)
        assert "self._clip_rect = QRectF()" in src, (
            "Construction must pre-allocate `self._clip_rect = QRectF()`. "
            "paint() sets its bounds via setRect per frame (issue #1)."
        )

    def test_cursor_rect_is_pre_allocated(self):
        from symmetria_ide.nvim_view import NvimView

        src = construction_source(NvimView)
        assert "self._cursor_rect = QRectF()" in src, (
            "Construction must pre-allocate `self._cursor_rect = QRectF()`. "
            "All three _paint_cursor branches mutate it via setRect "
            "(issue #1)."
        )


class TestPooledRunCharsInConstruction:
    """Construction must pre-allocate the reusable run-chars list.

    Closes issue #2: the previous per-run `list[str] = [cell.char]` was
    the dominant python-list allocation inside the paint hot path. The
    pool is clear()-then-append'd at each new run.
    """

    def test_run_chars_is_pre_allocated(self):
        from symmetria_ide.nvim_view import NvimView

        src = construction_source(NvimView)
        # Match the literal type annotation used in the source so a
        # future rename (e.g., switching to a deque) is caught.
        assert "self._run_chars: list[str] = []" in src, (
            "Construction must pre-allocate `self._run_chars: list[str] = []`. "
            "Without this pool, _paint_row will fall back to fresh list "
            "allocation per run (issue #2)."
        )


class TestPaintHotPathHasNoFreshQRectF:
    """paint() / _paint_visible_rows / _paint_row / _flush_run / _paint_cursor must NOT allocate QRectF.

    Grep-style structural check — if any of these functions starts
    containing `QRectF(` again, a future refactor reintroduced the
    allocation hazard. This directly validates issue #1's acceptance
    criterion: "`grep -n 'QRectF(' src/symmetria_ide/nvim_view.py`
    returns zero hits inside `_paint_row` and `_paint_cursor` bodies."
    """

    def test_paint_does_not_allocate_qrectf(self):
        from symmetria_ide.nvim_view import NvimView

        src = inspect.getsource(NvimView.paint)
        assert "QRectF(" not in src, (
            "paint() must not construct QRectF(...). Use self._clip_rect.setRect() "
            "and rely on _paint_row / _paint_cursor for per-run / per-cursor rects. "
            "Fresh QRectF inside paint is a gotcha #10 hazard (issue #1)."
        )

    def test_paint_row_does_not_allocate_qrectf(self):
        # _paint_row itself has never contained a QRectF(...) allocation —
        # it delegates to _flush_run which received the rect.  This guard
        # exists to catch a future developer who might add a rect allocation
        # directly inside _paint_row instead of passing the pooled run_rect
        # through to _flush_run.
        from symmetria_ide.nvim_view import NvimView

        src = inspect.getsource(NvimView._paint_row)
        assert "QRectF(" not in src, (
            "_paint_row must not construct QRectF(...). It passes the pooled "
            "self._run_rect into _flush_run, which mutates it via setRect "
            "(issue #1)."
        )

    def test_flush_run_does_not_allocate_qrectf(self):
        from symmetria_ide.nvim_view import _flush_run

        src = inspect.getsource(_flush_run)
        assert "QRectF(" not in src, (
            "_flush_run must not construct QRectF(...). It receives a pooled "
            "rect from _paint_row and calls rect.setRect(...) in place "
            "(issue #1)."
        )

    def test_paint_cursor_does_not_allocate_qrectf(self):
        from symmetria_ide.nvim_view import NvimView

        src = inspect.getsource(NvimView._paint_cursor)
        assert "QRectF(" not in src, (
            "_paint_cursor must not construct QRectF(...). All three shape "
            "branches (vertical / horizontal / block) must mutate "
            "self._cursor_rect via setRect (issue #1)."
        )

    def test_paint_visible_rows_does_not_allocate_qrectf(self):
        # _paint_visible_rows is the extraction introduced in issue #6. It owns
        # the self._clip_rect.setRect() call (exact-grid clip invariant from
        # gotcha #11). This guard ensures a future refactor never replaces that
        # setRect call with a fresh QRectF(...) allocation — which would
        # reintroduce the render-thread SEGV class from gotcha #10.
        from symmetria_ide.nvim_view import NvimView

        src = inspect.getsource(NvimView._paint_visible_rows)
        assert "QRectF(" not in src, (
            "_paint_visible_rows must not construct QRectF(...). "
            "The exact-grid clip (gotcha #11) must use self._clip_rect.setRect() "
            "— not a fresh allocation — to avoid the render-thread GC race "
            "(gotcha #10 / issue #1)."
        )


class TestPaintRowDoesNotAllocateRunCharsList:
    """_paint_row must clear + append into `self._run_chars`, not re-create it.

    The prior code was `run_chars: list[str] = [cell.char]` inside the
    while loop — a fresh list per highlight run. Issue #2 requires that
    pattern be replaced with a pooled list. If the regex below finds a
    fresh list literal inside _paint_row, the pool was silently bypassed.
    """

    def test_no_list_literal_inside_paint_row_loop(self):
        from symmetria_ide.nvim_view import NvimView

        src = inspect.getsource(NvimView._paint_row)
        assert "list[str] = [cell.char]" not in src, (
            "_paint_row must not allocate a fresh `list[str] = [cell.char]` per "
            "run. Use `self._run_chars.clear()` + `.append(cell.char)` against "
            "the pooled list (issue #2)."
        )
        assert "run_chars = self._run_chars" in src, (
            "_paint_row must hoist `run_chars = self._run_chars` before the "
            "outer while loop. This is the load-bearing reference to the "
            "pool; without it, the append/clear pattern writes into the "
            "wrong place."
        )
        # `.clear()` on the pooled list is the reset step that makes the
        # pool correct across runs. Missing clear() would mean each run
        # accumulates across row iterations, producing painter.drawText
        # content from prior runs.
        assert "run_chars.clear()" in src, (
            "_paint_row must call `run_chars.clear()` at the start of each "
            "run. Without it, the pooled list accumulates chars across runs "
            "and _flush_run will paint stale content from earlier runs."
        )


class TestFlushRunAcceptsPooledRect:
    """_flush_run's signature must include `rect: QRectF` as a parameter.

    The module-level helper is the allocation site most likely to drift
    under future edits (it's simple, tempting to "clean up"). Keeping the
    parameter explicit locks in the pool contract.
    """

    def test_flush_run_signature_accepts_rect_parameter(self):
        from symmetria_ide.nvim_view import _flush_run

        sig = inspect.signature(_flush_run)
        assert "rect" in sig.parameters, (
            "_flush_run must accept a `rect` parameter. It's the pooled "
            "QRectF from NvimView._run_rect, mutated via setRect(...) "
            "inside the function body (issue #1)."
        )
        # Type annotation sanity check — if someone replaces QRectF with
        # QRect (int-coord), alignment with the rest of the paint path
        # breaks and this guard fires before any visual regression.
        # `from __future__ import annotations` stringifies annotations, so
        # the attribute is a `str` here, not a class.
        # Note: this assertion is intentionally strict about the literal
        # type name. A type alias (e.g. `Rect = QRectF`) would also fail
        # here — the intent is to keep the annotation as `QRectF` directly
        # so that readers and tools resolve it to the concrete Qt type.
        rect_param = sig.parameters["rect"]
        assert rect_param.annotation == "QRectF", (
            "_flush_run's `rect` parameter must be annotated as QRectF. "
            "QRect (integer coords) would break sub-pixel cursor bars."
        )
