"""Tests for CursorAnimation + CursorBlink.

Pure math / pure state-machine. No Qt. Exercises:

- Position spring convergence and the "store remaining delta" seeding
  convention (cursor spring's unique twist relative to the scroll one).
- Short-jump speedup: ≤2-cell horizontal AND 0 vertical → 0.048s window.
- First-destination snap (no animate-in-from-origin on startup).
- Mid-flight re-seed preserves velocity (Neovide's "chained move feels
  continuous" behavior).
- Blink state machine: wait → on → off → on cycle with correct opacities
  at phase boundaries.
- Static-cursor suppression when any timing is zero.
"""

from __future__ import annotations

from symmetria_ide.nvim_view import (
    CURSOR_ANIMATION_LENGTH,
    CURSOR_SHORT_ANIMATION_LENGTH,
    CursorAnimation,
    CursorBlink,
)


FRAME_DT = 1.0 / 60.0  # 60 fps
CELL_W = 8.0
CELL_H = 16.0


# --- CursorAnimation ------------------------------------------------------


def test_initial_state_is_idle_and_unseeded() -> None:
    anim = CursorAnimation()
    assert anim.current_x == 0.0
    assert anim.current_y == 0.0
    assert not anim.active


def test_first_destination_snaps_no_animation() -> None:
    """Avoid sliding from (0,0) to first real position on startup."""
    anim = CursorAnimation()
    anim.set_destination(80.0, 160.0, CELL_W, CELL_H)
    # Snapped: current == destination, no motion.
    assert anim.current_x == 80.0
    assert anim.current_y == 160.0
    assert not anim.active


def test_second_destination_starts_animation() -> None:
    anim = CursorAnimation()
    anim.set_destination(0.0, 0.0, CELL_W, CELL_H)  # seed
    anim.set_destination(CELL_W * 5, 0.0, CELL_W, CELL_H)
    assert anim.active
    # current still at old position until first tick.
    assert anim.current_x == 0.0


def test_tick_converges_monotonically_toward_destination() -> None:
    anim = CursorAnimation()
    anim.set_destination(0.0, 0.0, CELL_W, CELL_H)
    anim.set_destination(CELL_W * 5, 0.0, CELL_W, CELL_H)
    dest_x = CELL_W * 5
    prev_gap = abs(dest_x - anim.current_x)
    for _ in range(30):
        anim.tick(FRAME_DT)
        gap = abs(dest_x - anim.current_x)
        assert gap <= prev_gap + 1e-9, "spring should not overshoot (critically damped)"
        prev_gap = gap
    # Settled within epsilon.
    assert not anim.active
    assert abs(anim.current_x - dest_x) < 1e-6


def test_short_jump_speedup_applied_for_one_cell_type() -> None:
    """Typing a letter is a 1-cell horizontal, 0 vertical jump — qualifies."""
    anim = CursorAnimation()
    anim.set_destination(0.0, 0.0, CELL_W, CELL_H)
    anim.set_destination(CELL_W, 0.0, CELL_W, CELL_H)
    # Direct check: the spring's active animation_length should be the
    # short constant. We assert on the constants rather than settle time
    # because settle time is dominated by the 0.01px epsilon, not by
    # animation_length alone.
    assert anim._animation_length == CURSOR_SHORT_ANIMATION_LENGTH  # noqa: SLF001
    assert CURSOR_SHORT_ANIMATION_LENGTH < CURSOR_ANIMATION_LENGTH


def test_two_cell_horizontal_jump_is_still_short() -> None:
    """Exactly 2 cells horizontal is on the inclusive boundary."""
    anim = CursorAnimation()
    anim.set_destination(0.0, 0.0, CELL_W, CELL_H)
    anim.set_destination(CELL_W * 2, 0.0, CELL_W, CELL_H)
    assert anim._animation_length == CURSOR_SHORT_ANIMATION_LENGTH  # noqa: SLF001


def test_three_cell_horizontal_jump_is_not_short() -> None:
    """Past 2 cells → full animation length."""
    anim = CursorAnimation()
    anim.set_destination(0.0, 0.0, CELL_W, CELL_H)
    anim.set_destination(CELL_W * 3, 0.0, CELL_W, CELL_H)
    assert anim._animation_length == CURSOR_ANIMATION_LENGTH  # noqa: SLF001


def test_long_jump_uses_full_animation_length() -> None:
    """Jumping > 2 cells horizontally should use the full animation_length.

    We can't time this precisely without a frame clock, but we can check
    it takes MORE frames to settle than a 1-cell jump does.
    """
    short = CursorAnimation()
    short.set_destination(0.0, 0.0, CELL_W, CELL_H)
    short.set_destination(CELL_W, 0.0, CELL_W, CELL_H)
    short_frames = 0
    while short.active and short_frames < 200:
        short.tick(FRAME_DT)
        short_frames += 1

    long = CursorAnimation()
    long.set_destination(0.0, 0.0, CELL_W, CELL_H)
    long.set_destination(CELL_W * 10, 0.0, CELL_W, CELL_H)
    long_frames = 0
    while long.active and long_frames < 200:
        long.tick(FRAME_DT)
        long_frames += 1

    assert long_frames > short_frames


def test_vertical_jump_uses_full_animation_length() -> None:
    """Moving to a different row (j/k, gg/G) is not a short jump."""
    anim = CursorAnimation()
    anim.set_destination(0.0, 0.0, CELL_W, CELL_H)
    anim.set_destination(CELL_W, CELL_H, CELL_W, CELL_H)  # 1-cell diag
    # Direct check — consistent with test_three_cell_horizontal_jump_is_not_short.
    assert anim._animation_length == CURSOR_ANIMATION_LENGTH  # noqa: SLF001
    frames = 0
    while anim.active and frames < 200:
        anim.tick(FRAME_DT)
        frames += 1
    # Full-length animation should take at least ~10 frames @ 60fps
    # to settle below 0.01px (spring decay).
    assert frames >= 8


def test_vertical_only_jump_uses_full_animation_length() -> None:
    """Purely vertical move (j/k with no column change) is not a short jump.

    This is the most common non-short cursor move: moving between rows
    with the cursor staying in the same column.
    """
    anim = CursorAnimation()
    anim.set_destination(0.0, 0.0, CELL_W, CELL_H)
    anim.set_destination(0.0, CELL_H, CELL_W, CELL_H)  # 0 cols, 1 row
    assert anim._animation_length == CURSOR_ANIMATION_LENGTH  # noqa: SLF001


def test_mid_flight_redirect_preserves_velocity() -> None:
    """New destination mid-animation should NOT zero velocity.

    Neovide's analytic spring step uses both position and velocity as
    initial conditions; re-seeding position alone keeps the live velocity
    rolling into the new target. The practical effect: typing rapid
    characters feels like one continuous glide rather than discrete jerks.
    """
    anim = CursorAnimation()
    anim.set_destination(0.0, 0.0, CELL_W, CELL_H)
    anim.set_destination(CELL_W * 5, 0.0, CELL_W, CELL_H)
    # Tick partway.
    for _ in range(3):
        anim.tick(FRAME_DT)
    v_before_redirect = anim._velocity_x  # noqa: SLF001
    # Redirect to a slightly further target.
    anim.set_destination(CELL_W * 8, 0.0, CELL_W, CELL_H)
    v_after_redirect = anim._velocity_x  # noqa: SLF001
    # Redirect should not touch velocity; we preserved it intentionally.
    assert v_before_redirect == v_after_redirect


def test_idempotent_redirect_to_same_destination_is_noop() -> None:
    """A second set_destination to the exact same point should not zero velocity."""
    anim = CursorAnimation()
    anim.set_destination(0.0, 0.0, CELL_W, CELL_H)
    anim.set_destination(CELL_W * 5, 0.0, CELL_W, CELL_H)
    for _ in range(3):
        anim.tick(FRAME_DT)
    v = anim._velocity_x  # noqa: SLF001
    anim.set_destination(CELL_W * 5, 0.0, CELL_W, CELL_H)  # same
    assert anim._velocity_x == v  # noqa: SLF001


def test_per_frame_retarget_does_not_reclassify_mid_flight() -> None:
    """Per-frame scroll-target retargets must not flip animation_length mid-flight.

    `_update_cursor_destination` calls `set_destination` every frame while
    a scroll animation is active, chasing a moving target. As the cursor
    approaches that target the remaining delta shrinks below the short-jump
    threshold (`|pos_y| < cell_h * 0.5`). Without the `not self.active`
    guard, `_animation_length` would flip from CURSOR_ANIMATION_LENGTH to
    CURSOR_SHORT_ANIMATION_LENGTH mid-flight, speeding up the spring decay
    and biasing the trajectory by up to ~4px for ~300ms.

    Regression guard for the fix introduced alongside the per-frame retarget
    feature (`_update_cursor_destination` called from `_on_frame_swapped`).
    """
    anim = CursorAnimation()
    anim.set_destination(0.0, 0.0, CELL_W, CELL_H)
    # Large vertical jump (8 rows) — full animation length expected.
    anim.set_destination(0.0, CELL_H * 8, CELL_W, CELL_H)
    assert anim._animation_length == CURSOR_ANIMATION_LENGTH  # noqa: SLF001

    # Simulate a few frames of a scroll-driven retarget: the destination
    # drifts as the scroll spring decays, and the remaining delta shrinks.
    # Each call mimics `_update_cursor_destination` with a slightly lower
    # dest_y as scroll_anim.position approaches 0.
    for frame in range(10):
        anim.tick(FRAME_DT)
        # Shrink dest_y toward the final row position (scroll decaying).
        shrinking_dest_y = CELL_H * 8 * (1.0 - frame / 20.0)
        anim.set_destination(0.0, shrinking_dest_y, CELL_W, CELL_H)
        # animation_length must remain at CURSOR_ANIMATION_LENGTH throughout
        # — the spring is still active, so no reclassification should occur.
        assert anim._animation_length == CURSOR_ANIMATION_LENGTH, (  # noqa: SLF001
            f"frame {frame}: animation_length was reclassified mid-flight "
            f"(dest_y={shrinking_dest_y:.1f}, current_y={anim.current_y:.1f})"
        )


def test_reset_clears_state() -> None:
    anim = CursorAnimation()
    anim.set_destination(10.0, 20.0, CELL_W, CELL_H)
    anim.set_destination(50.0, 60.0, CELL_W, CELL_H)
    anim.reset()
    assert anim.current_x == 0.0
    assert anim.current_y == 0.0
    assert not anim.active
    # After reset, next destination snaps (unseeded).
    anim.set_destination(100.0, 200.0, CELL_W, CELL_H)
    assert anim.current_x == 100.0
    assert anim.current_y == 200.0


# --- CursorBlink ----------------------------------------------------------


def test_initial_state_is_static() -> None:
    blink = CursorBlink()
    assert blink.is_static
    assert not blink.active
    assert blink.opacity_at(0.0) == 1.0


def test_zero_wait_makes_cursor_static() -> None:
    """Any zero in wait/on/off disables blinking per :h guicursor."""
    blink = CursorBlink()
    blink.set_timings(0, 500, 500, 0.0)
    assert blink.is_static
    assert blink.opacity_at(1.0) == 1.0


def test_zero_on_makes_cursor_static() -> None:
    blink = CursorBlink()
    blink.set_timings(1000, 0, 500, 0.0)
    assert blink.is_static


def test_zero_off_makes_cursor_static() -> None:
    blink = CursorBlink()
    blink.set_timings(1000, 500, 0, 0.0)
    assert blink.is_static


def test_blink_phases_transition_correctly() -> None:
    """Wait 1s -> On 0.5s (fade out) -> Off 0.5s (fade in) -> On..."""
    blink = CursorBlink()
    blink.set_timings(1000, 500, 500, 0.0)
    assert blink.active
    # During WAITING — opacity is 1.0.
    assert blink.opacity_at(0.5) == 1.0
    # ON starts at 1.0 (fading out). t=1.0 is the start of ON.
    op_on_start = blink.opacity_at(1.001)
    assert 0.99 < op_on_start <= 1.0
    # Halfway through ON (t=1.25) — opacity ~0.5.
    assert 0.4 < blink.opacity_at(1.25) < 0.6
    # End of ON (t=1.499) — opacity near 0.
    assert blink.opacity_at(1.499) < 0.1
    # Middle of OFF (t=1.75) — opacity ~0.5, rising.
    assert 0.4 < blink.opacity_at(1.75) < 0.6
    # End of OFF (t=1.999) — opacity near 1.
    assert blink.opacity_at(1.999) > 0.9


def test_blink_continues_cycling_past_first_off() -> None:
    """After On → Off, should go back to On (not Waiting)."""
    blink = CursorBlink()
    blink.set_timings(1000, 500, 500, 0.0)
    # t=2.25 → middle of 2nd ON phase (1.0 wait + 0.5 on + 0.5 off + 0.25)
    # → fading out from 1.0, should be around 0.5.
    assert 0.4 < blink.opacity_at(2.25) < 0.6


def test_set_timings_idempotent_when_unchanged() -> None:
    """Same timings twice should not restart the phase clock."""
    blink = CursorBlink()
    blink.set_timings(1000, 500, 500, 0.0)
    op_before = blink.opacity_at(1.25)
    # Second call with same timings but "later" now — should be ignored.
    blink.set_timings(1000, 500, 500, 5.0)
    # Phase still measured from t=0.0; at t=1.25 opacity is the same.
    op_after = blink.opacity_at(1.25)
    assert op_before == op_after


def test_set_timings_resets_when_changed() -> None:
    """Different timings should restart the waiting phase."""
    blink = CursorBlink()
    blink.set_timings(1000, 500, 500, 0.0)
    blink.set_timings(500, 200, 200, 3.0)  # restarts from t=3.0
    # At t=3.25 we should be in the new WAITING phase (wait=500ms).
    assert blink.opacity_at(3.25) == 1.0
    # At t=3.6 we've passed the new wait and are in ON (fading out).
    assert 0.0 < blink.opacity_at(3.6) < 1.0


def test_reset_phase_restarts_waiting() -> None:
    blink = CursorBlink()
    blink.set_timings(1000, 500, 500, 0.0)
    # Advance into ON phase.
    _ = blink.opacity_at(1.25)
    blink.reset_phase(10.0)
    # At t=10.5 we should still be in the new WAITING phase.
    assert blink.opacity_at(10.5) == 1.0


def test_reset_phase_on_static_cursor_has_no_visible_effect() -> None:
    """reset_phase on a static cursor doesn't break opacity_at.

    _static is still True so opacity_at still returns 1.0 regardless
    of the internal phase / phase_start values.
    """
    blink = CursorBlink()
    # No set_timings — stays static.
    blink.reset_phase(10.0)
    assert blink.is_static
    assert blink.opacity_at(10.5) == 1.0
    assert blink.opacity_at(12.0) == 1.0


def test_opacity_at_clamped_to_one_for_clock_skew_before_phase_start() -> None:
    """Backward clock (now < phase_start) should not return opacity > 1.

    elapsed = now - phase_start < 0 satisfies 'elapsed < duration'
    immediately so the phase-advance loop does not run. Opacity formula
    for PHASE_ON is remaining/on_s = (on_s - negative)/on_s > 1,
    clamped to 1.0 by min(1.0, ...).
    """
    blink = CursorBlink()
    blink.set_timings(1000, 500, 500, 5.0)  # phase_start = 5.0
    # Query with now < phase_start (clock skew scenario).
    opacity = blink.opacity_at(4.0)
    assert opacity == 1.0, f"expected 1.0, got {opacity}"
