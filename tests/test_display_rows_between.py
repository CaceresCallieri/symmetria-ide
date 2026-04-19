"""Integration tests for `runtime/init.lua`'s `display_rows_between`.

The original scroll-animation bug that motivated this helper was a unit
mismatch: `WinScrolled` emitted logical-line deltas but Python's
scrollback rotation + spring displacement + paint-time pixel offset all
operate on display rows. On wrap=true buffers these two units diverge
dramatically (up to 1:9 amplification observed on CLAUDE.md's wrapped
gotcha block), producing "lines shifting / abrupt jump / animation
doesn't play the full distance."

`display_rows_between` is the Lua helper that now converts a logical
range to an approximate display-row count before emission. It has
meaningful algorithmic complexity (fold detection via `foldclosedend`,
`strdisplaywidth`-based wrap estimation, scan-cap fallback,
`textoff`-corrected text width) and the review that landed it explicitly
flagged it as needing tests.

These tests spawn a real headless nvim (`--clean` for isolation) with
the runtime loaded, set up controlled buffers, and call the helper via
`_G.symmetria_display_rows_between` (exposed as an IPC boundary per
CLAUDE.md gotcha #2 — same pattern as `_G.symmetria_push_state` and the
whichkey test hooks). `exec_lua` returns the value synchronously, so no
notification-pumping is required.

If this file starts failing after a refactor, the smooth-scroll
animation is once again emitting wrong-unit deltas and the user-visible
symptom (lines shifting on wrapped buffers) is one commit away.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

pynvim = pytest.importorskip("pynvim")

pytestmark = pytest.mark.skipif(
    shutil.which("nvim") is None,
    reason="nvim binary not on PATH; integration tests skipped",
)


RUNTIME_DIR = Path(__file__).parent.parent / "runtime"
INIT_LUA = RUNTIME_DIR / "init.lua"


@pytest.fixture
def nvim():
    """Spawn a headless `nvim --embed --clean` with our runtime loaded.

    `--clean` skips user config so these tests are not at the mercy of
    whatever plugins the developer has installed. Runtime is injected
    via `rtp^=` + `luafile` — identical to how `nvim_backend.py` boots
    nvim in production.
    """
    argv = [
        "nvim",
        "--embed",
        "-n",
        "--clean",
        "--cmd",
        f"set rtp^={RUNTIME_DIR}",
        "--cmd",
        f"luafile {INIT_LUA}",
    ]
    handle = pynvim.attach("child", argv=argv)
    try:
        # Attach a UI so `winwidth(0)`, `getwininfo()`, `strdisplaywidth`
        # all have a sane backing window size. 80×24 is the classic
        # terminal grid; tests reason about text_width relative to it.
        handle.ui_attach(80, 24, rgb=True, ext_linegrid=True)
        # Guard: the helper must be exposed as a global. If this assert
        # fires, the production code removed the `_G.` line that makes
        # these tests reachable.
        exists = handle.exec_lua(
            "return type(_G.symmetria_display_rows_between) == 'function'"
        )
        assert exists, (
            "_G.symmetria_display_rows_between missing — "
            "runtime/init.lua must expose it as an IPC boundary "
            "(gotcha #2 pattern) for these tests to reach the helper."
        )
        yield handle
    finally:
        try:
            handle.quit()
        except Exception:  # noqa: BLE001
            pass
        handle.close()


def _display_rows(nvim, lnum_from: int, lnum_to: int, text_width: int) -> int:
    """Invoke the helper under test with explicit args and return the result.

    Bypasses the WinScrolled handler's `text_width` derivation so tests
    can parameterize it directly — keeps assertions about the algorithm
    independent of the ambient window geometry. `exec_lua` is variadic
    (`exec_lua(code, *args)`), so args must be unpacked — passing a
    single list would arrive in Lua as one table argument, not three
    scalars, and the helper would then compare nil to numbers.
    """
    return nvim.exec_lua(
        "return _G.symmetria_display_rows_between(...)",
        lnum_from,
        lnum_to,
        text_width,
    )


class TestDegenerateCases:
    """Zero-range + invalid-width early exits."""

    def test_same_line_returns_zero(self, nvim) -> None:
        assert _display_rows(nvim, 5, 5, 80) == 0

    def test_text_width_zero_returns_zero(self, nvim) -> None:
        """Defensive early-out: without a positive text width the ceil
        division would produce infinities / NaN. The handler should
        never call with <1 in practice (WinScrolled only fires on
        rendered windows) but the guard keeps a startup-race bug from
        propagating into the scroll pipeline."""
        nvim.current.buffer[:] = ["a" * 100]
        assert _display_rows(nvim, 1, 2, 0) == 0

    def test_text_width_negative_returns_zero(self, nvim) -> None:
        assert _display_rows(nvim, 1, 5, -10) == 0


class TestWrapOffEquivalentToLogical:
    """With no wrap and no folds, display rows == logical lines (abs)."""

    def test_short_lines_forward_delta(self, nvim) -> None:
        """4 short lines → 4 display rows for a range spanning them."""
        nvim.current.buffer[:] = ["aaa", "bbb", "ccc", "ddd", "eee"]
        # [1, 5) = lines 1,2,3,4 — each one row; expect 4.
        assert _display_rows(nvim, 1, 5, 80) == 4

    def test_short_lines_reverse_delta_is_negative(self, nvim) -> None:
        """Sign restoration: reverse range should mirror forward result."""
        nvim.current.buffer[:] = ["aaa", "bbb", "ccc", "ddd", "eee"]
        assert _display_rows(nvim, 5, 1, 80) == -4

    def test_blank_lines_count_as_one_each(self, nvim) -> None:
        """Blank-line handling — this is the branch the review cleanup
        simplified via `math.max(1, math.ceil(0 / n))`. Regressing to
        `if disp == 0 then continue` would make blank lines contribute
        0 rows and break scroll animations through docstring gaps."""
        nvim.current.buffer[:] = ["a", "", "", "", "b"]
        assert _display_rows(nvim, 1, 5, 80) == 4


class TestWrapAmplification:
    """Long lines in `wrap=true` mode contribute multiple display rows.

    This is the whole reason the helper exists. `delta_logical=1` across
    a wrapped paragraph was producing `delta_display=9` on CLAUDE.md
    during the bug reproduction — that amplification is what broke
    the original scroll animation.
    """

    def test_long_line_produces_multiple_display_rows(self, nvim) -> None:
        """A 240-char line in a 80-wide window wraps to 3 rows."""
        long_line = "x" * 240
        nvim.current.buffer[:] = [long_line, "short"]
        # Just line 1 in the range: [1, 2).
        assert _display_rows(nvim, 1, 2, 80) == 3

    def test_partial_wrap_ceilings_up(self, nvim) -> None:
        """A 100-char line in 80-wide wraps to 2 rows (ceil(100/80))."""
        nvim.current.buffer[:] = ["y" * 100, "short"]
        assert _display_rows(nvim, 1, 2, 80) == 2

    def test_exact_multiple_does_not_add_an_empty_row(self, nvim) -> None:
        """A 160-char line at 80 wide is exactly 2 rows, not 3.

        `ceil(160/80) = 2` — not `floor + 1`. Off-by-one here was the
        class of thinking error that originally underestimated deltas."""
        nvim.current.buffer[:] = ["z" * 160]
        assert _display_rows(nvim, 1, 2, 80) == 2

    def test_mixed_short_and_long_lines_sum(self, nvim) -> None:
        """Algorithm must accumulate per-line display rows, not average."""
        nvim.current.buffer[:] = [
            "short",  # 1 row
            "x" * 240,  # 3 rows
            "mid" * 40,  # 120 chars -> 2 rows (ceil(120/80))
            "end",  # 1 row
        ]
        # [1, 5) covers all four lines: 1 + 3 + 2 + 1 = 7.
        assert _display_rows(nvim, 1, 5, 80) == 7


class TestFoldCollapsing:
    """Closed folds must collapse to one display row regardless of size."""

    def test_closed_fold_counts_as_one_row(self, nvim) -> None:
        """20-line manual fold collapses to 1 display row.

        This matches what nvim itself does when rendering the grid: a
        closed fold occupies exactly one line of screen space. Without
        this branch, scrolling past a fold would animate by the fold's
        line count rather than the visible travel."""
        nvim.current.buffer[:] = [f"line {i}" for i in range(1, 26)]
        # Set foldmethod=manual and create a fold over lines 5-20.
        nvim.command("setlocal foldmethod=manual")
        nvim.command("5,20fold")
        # Sanity: the fold is closed.
        assert nvim.exec_lua("return vim.fn.foldclosedend(5)") == 20
        # [1, 25): lines 1-4 = 4 rows, fold (5-20) = 1 row,
        # lines 21-24 = 4 rows → total 9. Without the fold branch
        # we would incorrectly sum to 24.
        assert _display_rows(nvim, 1, 25, 80) == 9


class TestScanCapFallback:
    """Beyond MAX_SCAN_LINES=500 the helper must degrade to abs(logical).

    This avoids blocking the event loop on gg/G across thousands of
    lines. The fallback is the pre-fix behavior (slightly short
    animation) rather than a hang.
    """

    def test_small_range_below_cap_uses_real_computation(self, nvim) -> None:
        """Well below the 500-line cap, the scan runs normally."""
        nvim.current.buffer[:] = ["line"] * 400
        # 400-line range: 400 short lines × 1 row each = 400.
        assert _display_rows(nvim, 1, 401, 80) == 400

    def test_range_above_cap_returns_logical_delta(self, nvim) -> None:
        """Above the cap the helper falls back to the logical delta.

        The buffer contains lines that are 240 chars each — 3 display rows
        per line — so the scan branch would return 600 × 3 = 1800. The
        fallback branch returns the logical delta of 600. Using long lines
        makes the two branches return distinguishably different values, so
        the test actually proves the fallback was taken rather than
        coincidentally matching a scan result on short lines.
        """
        nvim.current.buffer[:] = ["x" * 240] * 600
        # 600 long lines → hi - lo = 600 > 500 cap → fallback → returns 600.
        # If the cap were raised above 600, the scan would run and return
        # ~1800 (3 display rows × 600 lines), causing this assertion to fail.
        assert _display_rows(nvim, 1, 601, 80) == 600

    def test_range_at_exact_cap_uses_scan(self, nvim) -> None:
        """At exactly MAX_SCAN_LINES (500), the scan still runs (> not >=).

        hi - lo == 500 is NOT > 500, so the scan branch fires. With long
        lines the scan returns ~1500 (3 × 500) while the fallback would
        return 500 — the distinguishable values confirm which branch ran.
        """
        nvim.current.buffer[:] = ["x" * 240] * 500
        # hi - lo = 500, which is NOT > 500, so scan runs → 3 × 500 = 1500.
        assert _display_rows(nvim, 1, 501, 80) == 1500

    def test_range_one_over_cap_triggers_fallback(self, nvim) -> None:
        """At MAX_SCAN_LINES + 1 (501), the fallback fires for the first time.

        hi - lo == 501 IS > 500, so fallback activates and returns the
        logical delta (501). A scan on 501 long lines would return ~1503,
        so the 501 assertion proves the fallback was reached.
        """
        nvim.current.buffer[:] = ["x" * 240] * 501
        assert _display_rows(nvim, 1, 502, 80) == 501

    def test_cap_preserves_sign_on_reverse(self, nvim) -> None:
        """Fallback still negates for reverse ranges."""
        nvim.current.buffer[:] = ["x" * 240] * 600
        assert _display_rows(nvim, 601, 1, 80) == -600
