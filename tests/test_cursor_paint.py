"""Structural guards for the `_paint_cursor` split (issue #9).

`_paint_cursor` used to be a ~84-line monolith that inlined four
distinct audit surfaces:

    1. Bootstrap seeding of the cursor spring.
    2. Shape + cell_percentage extraction from `mode_info_set`.
    3. Wall-clock blink opacity sampling (gotcha #13).
    4. Three-way shape dispatch with pool-disciplined rects (gotcha #10).

It was split into:

    - `_paint_cursor`            — thin orchestrator (≤40 lines).
    - `_compute_blink_opacity`   — gotcha #13 enforcement.
    - `_resolve_cursor_shape`    — shape + cell_percentage.
    - `_draw_cursor_shape`       — gotcha #10 pooled-rect dispatch.

These tests are structural (source-inspection) rather than behavioural —
the cursor-spring math itself lives in ``test_cursor_animation.py`` and
full paint-path behaviour requires a live QQuickWindow + render thread.
The structural checks here exist so that a future refactor that "cleans
up" the cursor paint cannot silently collapse the helpers back into the
parent or drop the load-bearing gotcha annotations.

If any guard here fails, the regression is: either a gotcha #10 pooled
`QRectF` discipline or gotcha #13 wall-clock blink invariant has been
weakened, and a visible cursor artifact (stair-stepped blink) or
render-thread SEGV class of bug is a single edit away.
"""

from __future__ import annotations

import inspect


class TestPaintCursorIsThinOrchestrator:
    """`_paint_cursor` must stay a thin dispatcher over the helpers.

    The whole point of the split is that each gotcha audit surface is
    reviewable in isolation. If the orchestrator starts inlining the
    blink sample or the shape-dispatch branches again, that isolation
    is gone and the 84-line hairball returns.
    """

    def test_paint_cursor_calls_blink_opacity_helper(self):
        from symmetria_ide.nvim_view import NvimView

        src = inspect.getsource(NvimView._paint_cursor)
        assert "self._compute_blink_opacity(" in src, (
            "_paint_cursor must delegate blink sampling to "
            "_compute_blink_opacity(). Inlining `self._cursor_blink.opacity_at"
            "(time.perf_counter())` here recombines the gotcha #13 audit "
            "surface with the shape-dispatch one (issue #9)."
        )

    def test_paint_cursor_calls_shape_resolve_helper(self):
        from symmetria_ide.nvim_view import NvimView

        src = inspect.getsource(NvimView._paint_cursor)
        assert "self._resolve_cursor_shape(" in src, (
            "_paint_cursor must delegate shape + cell_percentage "
            "extraction to _resolve_cursor_shape(). This is one of the "
            "two helpers named in issue #9's acceptance criteria."
        )

    def test_paint_cursor_calls_draw_shape_helper(self):
        from symmetria_ide.nvim_view import NvimView

        src = inspect.getsource(NvimView._paint_cursor)
        assert "self._draw_cursor_shape(" in src, (
            "_paint_cursor must delegate the three-way shape dispatch "
            "to _draw_cursor_shape(). Inlining the vertical/horizontal/"
            "block branches here reintroduces the 84-line monolith "
            "(issue #9)."
        )

    def test_paint_cursor_does_not_inline_shape_branches(self):
        from symmetria_ide.nvim_view import NvimView

        src = inspect.getsource(NvimView._paint_cursor)
        # The three shape branches must live in _draw_cursor_shape —
        # NOT in the orchestrator — so gotcha #10's pool discipline is
        # co-located with the draw calls it protects.
        assert 'shape == "vertical"' not in src, (
            "_paint_cursor must not branch on cursor shape directly. "
            "Route through _draw_cursor_shape so the pooled-rect "
            "contract (gotcha #10) stays in one place (issue #9)."
        )
        assert 'shape == "horizontal"' not in src, (
            "_paint_cursor must not branch on cursor shape directly. "
            "Route through _draw_cursor_shape so the pooled-rect "
            "contract (gotcha #10) stays in one place (issue #9)."
        )

    def test_paint_cursor_does_not_inline_blink_sample(self):
        from symmetria_ide.nvim_view import NvimView

        src = inspect.getsource(NvimView._paint_cursor)
        assert "self._cursor_blink.opacity_at(" not in src, (
            "_paint_cursor must not call self._cursor_blink.opacity_at "
            "directly. Route through _compute_blink_opacity so the "
            "gotcha #13 wall-clock invariant has one enforcement site "
            "(issue #9)."
        )

    def test_paint_cursor_body_is_under_forty_lines(self):
        """Acceptance gate from issue #9: ≤40 lines total.

        The orchestrator's whole reason to exist is to BE small. If a
        future refactor grows it back, this guard fires before review.
        Counting lines via the source string (includes docstring) —
        that's the most defensible interpretation of the acceptance.
        """
        from symmetria_ide.nvim_view import NvimView

        src = inspect.getsource(NvimView._paint_cursor)
        line_count = src.count("\n")
        assert line_count <= 40, (
            f"_paint_cursor is {line_count} lines — issue #9 acceptance "
            f"requires ≤40. Extract more into helpers or trim docstring."
        )


class TestBlinkOpacityHelperHonoursGotcha13:
    """gotcha #13: blink is a wall-clock ramp, NOT per-frame `dt` accumulation.

    Ported from Neovide's `cursor_renderer/blink.rs`. Accumulating
    per-frame stair-steps the opacity when the frame clock stalls
    (tab-away/tab-back, compositor hiccup). `time.perf_counter()` is
    the only acceptable input.
    """

    def test_blink_opacity_samples_perf_counter(self):
        from symmetria_ide.nvim_view import NvimView

        src = inspect.getsource(NvimView._compute_blink_opacity)
        assert "time.perf_counter()" in src, (
            "_compute_blink_opacity must sample time.perf_counter(). "
            "Using a per-frame dt accumulator stair-steps the opacity "
            "when the frame clock stalls (gotcha #13)."
        )

    def test_blink_opacity_calls_opacity_at(self):
        from symmetria_ide.nvim_view import NvimView

        src = inspect.getsource(NvimView._compute_blink_opacity)
        assert "self._cursor_blink.opacity_at(" in src, (
            "_compute_blink_opacity must delegate the ramp math to "
            "self._cursor_blink.opacity_at(...). That's where the "
            ":h guicursor zero-disables-blinking semantics live."
        )


class TestDrawCursorShapeHonoursGotcha10:
    """gotcha #10: every shape branch must mutate the pooled rect in place.

    A fresh `QRectF(...)` per paint re-introduces the shiboken-wrapper
    allocation hazard that interacts with Python 3.14's cyclic GC to
    SEGV the Qt render thread. Issue #1 fixed this class by pooling
    `_cursor_rect`; this guard ensures the cursor split didn't drop
    the pool discipline on the floor.
    """

    def test_draw_shape_mutates_pooled_rect(self):
        from symmetria_ide.nvim_view import NvimView

        src = inspect.getsource(NvimView._draw_cursor_shape)
        assert "self._cursor_rect" in src, (
            "_draw_cursor_shape must reference self._cursor_rect — the "
            "pooled QRectF from _init_buffers. Without it, a fresh "
            "QRectF(...) per paint reintroduces the gotcha #10 hazard."
        )
        assert "rect.setRect(" in src, (
            "_draw_cursor_shape must call rect.setRect(...) to mutate "
            "the pooled rect in place. Any assignment like `rect = "
            "QRectF(...)` defeats the pool (gotcha #10 / issue #1)."
        )

    def test_draw_shape_does_not_allocate_qrectf(self):
        from symmetria_ide.nvim_view import NvimView

        src = inspect.getsource(NvimView._draw_cursor_shape)
        assert "QRectF(" not in src, (
            "_draw_cursor_shape must not construct QRectF(...). All "
            "three shape branches mutate self._cursor_rect via setRect "
            "(gotcha #10 / issue #1)."
        )

    def test_draw_shape_does_not_read_cursor_mode(self):
        from symmetria_ide.nvim_view import NvimView

        src = inspect.getsource(NvimView._draw_cursor_shape)
        # Shape resolution lives in _resolve_cursor_shape; _draw_cursor_shape
        # receives `shape` as a parameter. If someone adds a
        # self._cursor_mode.get(...) call here, the separation silently
        # collapses and the gotcha #10 audit surface acquires gotcha-state
        # entanglement — shape resolution and pooled-rect dispatch become
        # intermixed again (issue #9).
        assert "self._cursor_mode" not in src, (
            "_draw_cursor_shape must not access self._cursor_mode. Shape "
            "resolution is the responsibility of _resolve_cursor_shape(); "
            "this helper receives `shape` as a parameter. Direct access "
            "here re-entangles the audit surfaces split by issue #9."
        )

    def test_draw_shape_covers_three_branches(self):
        from symmetria_ide.nvim_view import NvimView

        src = inspect.getsource(NvimView._draw_cursor_shape)
        # All three NeoVim cursor shapes must be handled. If a future
        # refactor collapses them (e.g., drops horizontal), cmdline and
        # replace modes would silently fall back to block.
        assert 'shape == "vertical"' in src, (
            "_draw_cursor_shape must handle the `vertical` branch "
            "(insert-mode ver25). Missing this branch collapses "
            "insert-mode cursors to blocks."
        )
        assert 'shape == "horizontal"' in src, (
            "_draw_cursor_shape must handle the `horizontal` branch "
            "(cmdline/replace hor20). Missing this branch collapses "
            "cmdline/replace cursors to blocks."
        )
        # The block branch (the `else`) reverses-video the full cell and
        # overlays the glyph via `drawText`. If this glyph-draw call were
        # removed, the cursor would paint a solid fg-colored rectangle with
        # no character visible — a silent visual regression in normal mode.
        assert "painter.drawText(" in src, (
            "_draw_cursor_shape must call painter.drawText in the block "
            "branch to overlay the glyph on the reverse-video background. "
            "Without it, the block cursor paints a solid rectangle with no "
            "character visible (normal-mode cursor shows a blank box)."
        )


class TestResolveCursorShapeReadsModeInfo:
    """Shape helper must read from `_cursor_mode` (populated by `mode_info_set`).

    Hard-coding a shape here would bypass NeoVim's mode-driven cursor
    semantics entirely (user would see `guicursor` settings ignored).
    """

    def test_resolve_reads_cursor_mode_dict(self):
        from symmetria_ide.nvim_view import NvimView

        src = inspect.getsource(NvimView._resolve_cursor_shape)
        assert 'self._cursor_mode.get("cursor_shape"' in src, (
            "_resolve_cursor_shape must read shape from "
            "self._cursor_mode — that dict is populated by mode_info_set."
        )
        assert 'self._cursor_mode.get("cell_percentage"' in src, (
            "_resolve_cursor_shape must read cell_percentage from "
            "self._cursor_mode — drives vertical-bar width and "
            "horizontal-underline thickness."
        )

    def test_resolve_defaults_to_block_full_cell(self):
        from symmetria_ide.nvim_view import NvimView

        src = inspect.getsource(NvimView._resolve_cursor_shape)
        # Before mode_info_set arrives (very early bootstrap), defaults
        # must produce a visible block cursor so the user sees SOMETHING.
        # `"block"` and `100` are the fallbacks.
        assert '"block"' in src, (
            "_resolve_cursor_shape must default shape to 'block' when "
            "mode_info_set hasn't arrived yet — otherwise early paint "
            "falls through to a hidden cursor."
        )
        assert "100" in src, (
            "_resolve_cursor_shape must default cell_percentage to 100 "
            "(full cell) so the block-cursor fallback paints the full "
            "cell, not a 0-width sliver."
        )
