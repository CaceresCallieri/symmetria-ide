"""Structural guards for the smooth-scroll orchestrator split (issue #8).

`_maybe_apply_scroll_delta` used to inline rotate + clamp + snapshot in a
73-line method. It was split into:

    - `_clamp_scroll_spring(delta, grid_rows)` — gotcha #11 enforcement:
      ``max_delta = slot_start`` (NOT ``scrollback_rows - grid_rows``).
    - `_snapshot_viewport_into_center_slot(grid)` — gotcha #10 pool
      discipline: in-place writes, no per-row list churn.

These tests are structural (source-inspection) rather than behavioural —
the spring math itself is covered by ``test_scroll_animation.py`` and the
integration behaviour requires a full QGuiApplication + worker thread.
The structural checks here exist so that a future refactor that "cleans
up" the orchestrator cannot silently collapse the helpers back into the
parent or drop the load-bearing invariant annotations.

If any guard here fails, the regression is: the gotcha #11 `max_delta`
clamp or the gotcha #10 pool discipline has been weakened, and a visible
blank-band / render-thread SEGV class of bug is a single edit away.
"""

from __future__ import annotations

import inspect


class TestOrchestratorDelegatesToHelpers:
    """`_maybe_apply_scroll_delta` must stay a thin dispatcher.

    The whole point of the split is that each gotcha audit surface
    (clamp vs snapshot) is reviewable in isolation.  If the orchestrator
    starts inlining the spring.shift() call again, that isolation is
    gone and the 73-line hairball returns.
    """

    def test_orchestrator_calls_clamp_helper(self):
        from symmetria_ide.nvim_view import NvimView

        src = inspect.getsource(NvimView._maybe_apply_scroll_delta)
        assert "self._clamp_scroll_spring(" in src, (
            "_maybe_apply_scroll_delta must delegate spring clamping to "
            "_clamp_scroll_spring(...). Inlining the shift() call here "
            "recombines the rotate and clamp audit surfaces (issue #8)."
        )

    def test_orchestrator_calls_snapshot_helper(self):
        from symmetria_ide.nvim_view import NvimView

        src = inspect.getsource(NvimView._maybe_apply_scroll_delta)
        assert "self._snapshot_viewport_into_center_slot(" in src, (
            "_maybe_apply_scroll_delta must delegate the viewport snapshot "
            "to _snapshot_viewport_into_center_slot(...). The snapshot's "
            "IndexError-race tolerance belongs in the helper, not inline "
            "(issue #8)."
        )

    def test_orchestrator_does_not_inline_shift_or_far_jump_clear(self):
        from symmetria_ide.nvim_view import NvimView

        src = inspect.getsource(NvimView._maybe_apply_scroll_delta)
        # The shift and far-jump clear call pattern must live in the
        # clamp helper — NOT in the orchestrator — so the gotcha #11
        # invariant is co-located with its enforcement.
        assert "self._scroll_anim.shift(" not in src, (
            "_maybe_apply_scroll_delta must not call self._scroll_anim.shift "
            "directly. Route through _clamp_scroll_spring so the "
            "max_delta invariant (gotcha #11) has one enforcement site."
        )
        assert "consume_far_jump_clear" not in src, (
            "_maybe_apply_scroll_delta must not call consume_far_jump_clear "
            "directly. It belongs in _clamp_scroll_spring alongside the "
            "shift() call that produces the flag (issue #8)."
        )


class TestClampScrollSpringEnforcesMaxDeltaInvariant:
    """gotcha #11: `max_delta = slot_start`, NOT `scrollback_rows - grid.rows`.

    This was the exact bug fixed in commit 7282f68 — using the full
    buffer width for max_delta allowed compound scrolls to drift to
    positions the paint loop cannot render, producing a blank band at
    the leading edge. The regression guard here is literal: the helper
    must use `_scrollback_center_slot` (which returns slot_start), not
    the longer formula.
    """

    def test_clamp_uses_slot_start_as_max_delta(self):
        from symmetria_ide.nvim_view import NvimView

        src = inspect.getsource(NvimView._clamp_scroll_spring)
        assert "self._scrollback_center_slot(" in src, (
            "_clamp_scroll_spring must compute max_delta via "
            "self._scrollback_center_slot(grid_rows) — that's slot_start "
            "(gotcha #11). Using scrollback_rows - grid_rows allows "
            "position to drift to 2× what paint() can render."
        )

    def test_clamp_does_not_use_scrollback_rows_minus_grid_rows(self):
        from symmetria_ide.nvim_view import NvimView

        src = inspect.getsource(NvimView._clamp_scroll_spring)
        # The textual form of the wrong expression. If this ever shows
        # up, a future dev re-derived max_delta from first principles
        # and got it wrong (the same way the original bug did).
        assert "self._scrollback_rows - grid" not in src, (
            "_clamp_scroll_spring must NOT compute max_delta as "
            "`self._scrollback_rows - grid_rows`. That's 2× slot_start — "
            "compound scrolls land past the renderable range and show "
            "a blank band at the leading edge (gotcha #11)."
        )

    def test_clamp_calls_shift_and_consumes_far_jump(self):
        from symmetria_ide.nvim_view import NvimView

        src = inspect.getsource(NvimView._clamp_scroll_spring)
        assert "self._scroll_anim.shift(" in src, (
            "_clamp_scroll_spring must call self._scroll_anim.shift(delta, "
            "max_delta). This is the one site where the clamp is enforced "
            "(issue #8)."
        )
        assert "consume_far_jump_clear" in src, (
            "_clamp_scroll_spring must consume the far-jump clear flag "
            "and zero revealed scrollback rows, otherwise the `gg`/`G` "
            "decorative flourish would leak stale buffer content."
        )


class TestSnapshotHelperIsAllocationDisciplined:
    """gotcha #10: snapshot must write in place, not allocate fresh rows.

    Per-row list churn raises allocation pressure on Python 3.14's GC,
    which interacts with pynvim's greenlet RPC on the worker thread
    and puts the render thread's C++ access at risk during paint.
    """

    def test_snapshot_does_not_allocate_fresh_list_per_row(self):
        from symmetria_ide.nvim_view import NvimView

        src = inspect.getsource(NvimView._snapshot_viewport_into_center_slot)
        # A fresh-list pattern would look like `= [...]` or `= list(...)`
        # on the destination side. The correct pattern mutates
        # `dst_row[c] = src_row[c]` in place.
        assert "self._scrollback[slot_start + r] = " not in src, (
            "_snapshot_viewport_into_center_slot must NOT reassign "
            "self._scrollback[slot_start + r] = [...] — that churns "
            "a fresh list per row and defeats the pool discipline "
            "(gotcha #10, issue #8)."
        )
        assert "dst_row[c] = src_row[c]" in src, (
            "_snapshot_viewport_into_center_slot must write "
            "`dst_row[c] = src_row[c]` in place against the pre-allocated "
            "scrollback row (gotcha #10)."
        )

    def test_snapshot_returns_false_on_index_error(self):
        """The resize-race bail-out must stay as a return-False signal.

        If a future refactor swallows the IndexError silently, the
        orchestrator will go on to start the frame driver on a
        half-populated scrollback. The snapshot's bool return is the
        contract that lets the orchestrator bail cleanly.
        """
        from symmetria_ide.nvim_view import NvimView

        src = inspect.getsource(NvimView._snapshot_viewport_into_center_slot)
        assert "except IndexError" in src, (
            "_snapshot_viewport_into_center_slot must catch the "
            "resize-race IndexError. Without it, the worker thread's "
            "mid-snapshot grid.cells reassignment would raise into the "
            "GUI thread's flush handler."
        )
        assert "return False" in src, (
            "_snapshot_viewport_into_center_slot must return False on "
            "the IndexError race path. The orchestrator uses the return "
            "value to bail before starting the frame driver."
        )
