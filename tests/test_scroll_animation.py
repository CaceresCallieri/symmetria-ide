"""Tests for the ScrollAnimation critically-damped spring.

Pure math, no Qt. Exercises convergence, boundary handling, far-jump
clamp, and compounding scrolls — the behaviors Neovide's
`CriticallyDampedSpringAnimation` documents.
"""

from __future__ import annotations

import math

from symmetria_ide.nvim_view import (
    SCROLL_ANIMATION_FAR_LINES,
    SCROLL_ANIMATION_LENGTH,
    ScrollAnimation,
)


FRAME_DT = 1.0 / 60.0  # 60 fps


def test_initial_state_is_idle() -> None:
    anim = ScrollAnimation()
    assert anim.position == 0.0
    assert anim.velocity == 0.0
    assert not anim.active


def test_shift_displaces_position_in_opposite_sign_of_delta() -> None:
    """Neovide sign convention: positive delta -> negative position.

    Scrolling content up in the buffer (viewport moves down) produces
    negative position so the old content visually stays "above" where
    it was until the spring pulls position back to zero.
    """
    anim = ScrollAnimation()
    anim.shift(3, max_delta=20)
    assert anim.position == -3.0
    assert anim.velocity == 0.0
    assert anim.active


def test_shift_zero_is_noop() -> None:
    anim = ScrollAnimation()
    anim.shift(0, max_delta=20)
    assert anim.position == 0.0
    assert not anim.active


def test_shift_zero_max_delta_is_noop() -> None:
    """With no scrollback headroom we cannot animate — stay snapped."""
    anim = ScrollAnimation()
    anim.shift(3, max_delta=0)
    assert anim.position == 0.0
    assert not anim.active


def test_far_jump_clamps_to_far_lines_and_sets_clear_flag() -> None:
    anim = ScrollAnimation()
    anim.shift(500, max_delta=20)
    # Should clamp to ±SCROLL_ANIMATION_FAR_LINES in the displacement
    # direction — opposite sign of the scroll delta, same rule as the
    # normal path.
    assert anim.position == -float(SCROLL_ANIMATION_FAR_LINES)
    assert anim._far_jump_clear_pending is True

    # Consumption is one-shot.
    assert anim.consume_far_jump_clear() is True
    assert anim.consume_far_jump_clear() is False


def test_far_jump_negative_direction() -> None:
    anim = ScrollAnimation()
    anim.shift(-500, max_delta=20)
    assert anim.position == float(SCROLL_ANIMATION_FAR_LINES)


def test_shift_clamps_to_max_delta() -> None:
    """Within-limits compounding still clamps to the buffer headroom."""
    anim = ScrollAnimation()
    anim.shift(15, max_delta=20)   # position = -15
    anim.shift(10, max_delta=20)   # would go to -25, should clamp to -20
    assert anim.position == -20.0


def test_tick_returns_false_when_idle() -> None:
    anim = ScrollAnimation()
    assert anim.tick(FRAME_DT) is False


def test_tick_snaps_when_dt_exceeds_animation_length() -> None:
    """Massively janky frame > SCROLL_ANIMATION_LENGTH — snap to done.

    Matches Neovide's early-return so the user doesn't see a wobbly
    settle after the app was backgrounded/stalled for seconds.
    """
    anim = ScrollAnimation()
    anim.shift(5, max_delta=20)
    still = anim.tick(SCROLL_ANIMATION_LENGTH + 0.1)
    assert still is False
    assert anim.position == 0.0
    assert anim.velocity == 0.0


def test_tick_converges_monotonically_to_zero() -> None:
    """After enough frames the spring reaches its 'done' threshold.

    SCROLL_ANIMATION_LENGTH is the ~2% convergence parameter (Neovide's
    documented semantics), not the full settle-to-idle time. Our idle
    threshold is |pos| < 0.01, tighter than 2%, so a 5-line displacement
    takes more than SCROLL_ANIMATION_LENGTH to fully settle — that's
    correct critically-damped behavior, not a bug.

    What we *do* assert here: monotonic decay (no overshoot) and that
    the animation terminates in a reasonable number of frames.
    """
    anim = ScrollAnimation()
    anim.shift(5, max_delta=100)
    previous_abs = abs(anim.position)
    ticks = 0
    # 120 frames at 60fps == 2s; comfortably above the ~0.85s we expect
    # for a 5-line displacement and still bounded so a runaway bug fails.
    max_ticks = 120
    while anim.active and ticks < max_ticks:
        still = anim.tick(FRAME_DT)
        ticks += 1
        # Monotonic decay — critically damped, no overshoot.
        assert abs(anim.position) <= previous_abs + 1e-9
        previous_abs = abs(anim.position)
        if not still:
            break
    assert not anim.active, (
        f"animation did not settle in {max_ticks} frames "
        f"(pos={anim.position}, vel={anim.velocity})"
    )


def test_tick_converges_for_small_displacement() -> None:
    """1-line displacement settles within a few animation lengths.

    The "animation_length" parameter is the ~2%-convergence time, not
    the full settle time. For a 1-line shift hitting |pos| < 0.01 (an
    absolute threshold tighter than 2% for displacements below ~0.5)
    takes roughly 2.5× animation_length. We bound at 4× for headroom
    while still catching a runaway bug.
    """
    anim = ScrollAnimation()
    anim.shift(1, max_delta=100)
    ticks = 0
    max_ticks = int(SCROLL_ANIMATION_LENGTH * 4.0 / FRAME_DT) + 1
    while anim.active and ticks < max_ticks:
        anim.tick(FRAME_DT)
        ticks += 1
    assert not anim.active, (
        f"1-line shift did not settle in {max_ticks} frames "
        f"(pos={anim.position})"
    )


def test_tick_no_overshoot_for_small_dt() -> None:
    """Critically damped => strictly signed-monotonic; never crosses 0."""
    anim = ScrollAnimation()
    anim.shift(-7, max_delta=50)   # position = +7
    assert anim.position > 0
    for _ in range(200):
        if not anim.tick(FRAME_DT):
            break
        assert anim.position >= 0  # never crosses to negative
    assert not anim.active


def test_compounding_shifts_add_to_existing_displacement() -> None:
    """A scroll arriving mid-animation adds to the current offset."""
    anim = ScrollAnimation()
    anim.shift(5, max_delta=50)
    # Let it partially decay.
    for _ in range(5):
        anim.tick(FRAME_DT)
    mid_pos = anim.position
    assert -5.0 < mid_pos < 0.0  # decayed but not settled

    anim.shift(3, max_delta=50)
    # Expect current position - 3 (Neovide: scroll_offset -= scroll_delta).
    assert math.isclose(anim.position, mid_pos - 3.0, abs_tol=1e-9)


def test_reset_zeroes_state() -> None:
    anim = ScrollAnimation()
    anim.shift(4, max_delta=50)
    anim.tick(FRAME_DT)
    anim.reset()
    assert anim.position == 0.0
    assert anim.velocity == 0.0
    assert not anim.active
    assert anim.consume_far_jump_clear() is False


def test_active_is_true_when_only_velocity_nonzero() -> None:
    """active checks both position and velocity — zero position alone is not settled."""
    anim = ScrollAnimation()
    # Manually inject a nonzero velocity with zero position (transient
    # spring state that can occur mid-integration near the zero crossing).
    anim.position = 0.0
    anim.velocity = 0.5
    assert anim.active is True


def test_compounding_reverse_shift_clamps_to_negative_max_delta() -> None:
    """Compounding a backward shift after a forward one clamps to -max_delta."""
    anim = ScrollAnimation()
    anim.shift(-15, max_delta=20)   # position = +15
    anim.shift(-10, max_delta=20)   # would go to +25, should clamp to +20
    assert anim.position == 20.0


def test_reset_clears_far_jump_flag() -> None:
    """reset() must clear _far_jump_clear_pending set by a far-jump shift."""
    anim = ScrollAnimation()
    anim.shift(500, max_delta=20)   # triggers far-jump path
    assert anim._far_jump_clear_pending is True
    anim.reset()
    assert anim._far_jump_clear_pending is False
    # consume_far_jump_clear should also return False after reset.
    assert anim.consume_far_jump_clear() is False
