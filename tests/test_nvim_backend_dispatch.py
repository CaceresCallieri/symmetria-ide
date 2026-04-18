"""Dispatch-path tests for NvimBackend.

Covers the three incident-documented gotchas that `nvim_backend.py` is
responsible for but which had zero test coverage before this module:

  * **gotcha #1** — GUI-thread-facing API (`input`, `resize`) must
    marshal every RPC call through `nvim.async_call`. A direct call
    would raise `NvimError: request from non-main thread`.

  * **gotcha #9** — `_h_cmdline_show` / `_h_cmdline_pos` /
    `_h_cmdline_hide` / `_h_popupmenu_show` must accept both the
    NeoVim 0.9 arity (6 args for cmdline_show) and the 0.10+ arity
    (7 args, adds firstchar `hl_id`) without raising. The handler's
    forward-compat is `*_rest: Any` to swallow whatever future
    versions add.

  * **gotcha #10** — GC must be suspended (`gc.isenabled() == False`)
    for the duration of BOTH `_on_notification` (the outer entrypoint)
    AND `_dispatch_redraw` (the inner redraw batch loop). A
    regression that re-enabled GC inside either scope resurrects the
    Python 3.14 cyclic-collector × `QSGRenderThread` paint SEGV.

Does NOT spawn a real nvim process — tests monkeypatch pynvim-facing
state so they run headless on any CI runner without the nvim binary
on PATH. Mirrors the pattern established by
`tests/test_nvim_backend_shutdown.py`.

Event-routing tests verify both the happy path (correct signal fires
with correct payload) AND the malformed-input path (bad shape is
logged and dropped without emitting or raising).
"""

from __future__ import annotations

import gc
import os
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication  # noqa: E402

from symmetria_ide.nvim_backend import NvimBackend  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def qapp():
    """Shared QCoreApplication for Signal emission to work.

    `autouse=True` so every test in this module gets the application
    without having to accept it as a parameter — cuts unused-arg noise
    in ~20 test signatures.
    """
    app = QCoreApplication.instance() or QCoreApplication([])
    yield app


# ---------------------------------------------------------------------------
# Helpers — lightweight signal recorder
# ---------------------------------------------------------------------------


class _SignalRecorder:
    """Capture emitted payloads from a Qt Signal without a running event loop.

    Qt signals invoked via `.emit(...)` fire their connected slots
    synchronously on the calling thread when the connection is
    `AutoConnection` and the receiver lives in the same thread (our
    recorder is a plain Python object — no thread affinity, so Qt
    treats it as same-thread). That makes it safe to assert on
    `.payloads` immediately after the call that caused the emit.
    """

    def __init__(self) -> None:
        self.payloads: list = []

    def __call__(self, *args) -> None:
        # Single-arg signals (most of ours) pass one positional; for the
        # parameterless `redraw_flushed` we record an empty marker.
        if len(args) == 1:
            self.payloads.append(args[0])
        elif not args:
            self.payloads.append(None)
        else:
            self.payloads.append(args)


# ---------------------------------------------------------------------------
# Gotcha #1 — RPC thread marshalling
# ---------------------------------------------------------------------------


class TestRpcThreadMarshalling:
    """`input()` and `resize()` must go through `nvim.async_call`.

    A direct call to `nvim.input(...)` from the GUI thread raises
    `NvimError: request from non-main thread`. These tests fail if a
    future refactor accidentally bypasses the marshal.
    """

    def test_input_marshals_through_async_call(self) -> None:
        backend = NvimBackend()
        fake_nvim = MagicMock()
        backend._nvim = fake_nvim  # type: ignore[assignment]

        backend.input("iHello<Esc>")

        # Must go via async_call; direct input() is forbidden.
        assert fake_nvim.async_call.called, (
            "input() must marshal via nvim.async_call — direct RPC raises "
            "cross-thread NvimError (gotcha #1)."
        )
        fake_nvim.input.assert_not_called()

    def test_input_no_ops_when_nvim_is_none(self) -> None:
        """No nvim handle → silent no-op, not an exception."""
        backend = NvimBackend()
        assert backend._nvim is None
        backend.input("abc")  # must not raise

    def test_input_no_ops_on_empty_string(self) -> None:
        """Empty keys are skipped before any marshal attempt."""
        backend = NvimBackend()
        fake_nvim = MagicMock()
        backend._nvim = fake_nvim  # type: ignore[assignment]

        backend.input("")

        fake_nvim.async_call.assert_not_called()

    def test_resize_marshals_through_async_call(self) -> None:
        backend = NvimBackend()
        fake_nvim = MagicMock()
        backend._nvim = fake_nvim  # type: ignore[assignment]

        backend.resize(160, 48)

        assert fake_nvim.async_call.called
        fake_nvim.ui_try_resize.assert_not_called()
        # Internal dimensions updated.
        assert backend._cols == 160
        assert backend._rows == 48

    def test_resize_no_op_when_unchanged(self) -> None:
        """Same dimensions = no round-trip (rate-limit optimization)."""
        backend = NvimBackend(cols=120, rows=30)
        fake_nvim = MagicMock()
        backend._nvim = fake_nvim  # type: ignore[assignment]

        backend.resize(120, 30)

        fake_nvim.async_call.assert_not_called()


# ---------------------------------------------------------------------------
# Gotcha #9 — cmdline / popupmenu arity forward-compat
# ---------------------------------------------------------------------------


class TestCmdlineArityForwardCompat:
    """`cmdline_show` arg count varies by NeoVim version.

    0.9 sends 6 positional args; 0.10+ sends 7 (adds firstchar `hl_id`).
    The handler must accept BOTH without raising, or we regress on any
    user running a different nvim version than the CI runner.
    """

    def test_cmdline_show_accepts_9_arity_six_args(self) -> None:
        """NeoVim 0.9: content, pos, firstc, prompt, indent, level."""
        backend = NvimBackend()
        recorder = _SignalRecorder()
        backend.cmdline_updated.connect(recorder)

        # Positional form — exactly what pynvim unpacks from the wire.
        backend._h_cmdline_show(
            [[{}, ":"]],  # content
            1,  # pos
            ":",  # firstc
            "",  # prompt
            0,  # indent
            0,  # level
        )

        assert len(recorder.payloads) == 1
        payload = recorder.payloads[0]
        assert payload["kind"] == "show"
        assert payload["text"] == ":"
        assert payload["firstchar"] == ":"

    def test_cmdline_show_accepts_10plus_arity_seven_args(self) -> None:
        """NeoVim 0.10+: adds hl_id as the 7th arg."""
        backend = NvimBackend()
        recorder = _SignalRecorder()
        backend.cmdline_updated.connect(recorder)

        backend._h_cmdline_show(
            [[{}, "s/foo"]],
            5,
            ":",
            "",
            0,
            0,
            42,  # hl_id (0.10+)
        )

        assert len(recorder.payloads) == 1
        assert recorder.payloads[0]["text"] == "s/foo"

    def test_cmdline_pos_accepts_trailing_args(self) -> None:
        """Future NeoVim versions may append; `*_rest` must absorb them."""
        backend = NvimBackend()
        recorder = _SignalRecorder()
        backend.cmdline_updated.connect(recorder)

        # Call with an extra arg that doesn't exist today — `*_rest`
        # must swallow without raising.
        backend._h_cmdline_pos(3, 0, "future_field")

        assert len(recorder.payloads) == 1
        assert recorder.payloads[0]["kind"] == "pos"
        assert recorder.payloads[0]["pos"] == 3

    def test_cmdline_hide_accepts_zero_and_trailing_args(self) -> None:
        """`level` defaults to 0; `*_rest` absorbs the 0.9 `abort` flag."""
        backend = NvimBackend()
        recorder = _SignalRecorder()
        backend.cmdline_updated.connect(recorder)

        # Pre-0.9 zero-arg form.
        backend._h_cmdline_hide()
        # Post-0.9 with `abort` trailer.
        backend._h_cmdline_hide(1, False)

        assert len(recorder.payloads) == 2
        assert recorder.payloads[0]["kind"] == "hide"
        assert recorder.payloads[1]["level"] == 1

    def test_popupmenu_show_accepts_grid_and_trailing_args(self) -> None:
        """`_grid` (default -1) plus `*_rest` guards future arg growth."""
        backend = NvimBackend()
        recorder = _SignalRecorder()
        backend.popupmenu_updated.connect(recorder)

        # 4-arg legacy form (no grid).
        backend._h_popupmenu_show(
            [["foo", "v", ""]],
            0,
            5,
            10,
        )
        # 6-arg future form (grid + trailing extra).
        backend._h_popupmenu_show(
            [["bar", "f", ""]],
            1,
            5,
            10,
            2,
            "future_field",
        )

        assert len(recorder.payloads) == 2
        assert recorder.payloads[0]["items"][0]["word"] == "foo"
        assert recorder.payloads[1]["items"][0]["word"] == "bar"
        assert recorder.payloads[1]["selected"] == 1

    def test_cmdline_show_chunks_without_hl_id_still_flatten(self) -> None:
        """0.9 chunks are `[attrs, text]`; 0.10+ chunks can be `[attrs, text, hl_id]`."""
        backend = NvimBackend()
        recorder = _SignalRecorder()
        backend.cmdline_updated.connect(recorder)

        backend._h_cmdline_show(
            [[{}, "foo"], [{}, "bar", 7]],  # mixed 2/3-tuple chunks
            6,
            ":",
            "",
            0,
            0,
        )

        assert recorder.payloads[0]["text"] == "foobar"


# ---------------------------------------------------------------------------
# Gotcha #10 — GC suspension around notification + redraw dispatch
# ---------------------------------------------------------------------------


class TestGcSuspensionInvariants:
    """GC must be disabled for the duration of `_on_notification`
    AND `_dispatch_redraw`.

    Python 3.14's incremental cyclic collector can fire from ANY
    allocation on the worker thread — not just `Cell.__init__`.
    Re-enabling GC inside either scope races with `QSGRenderThread`
    running `paint()`, producing the documented SEGV.
    """

    def test_gc_is_disabled_during_on_notification(self) -> None:
        backend = NvimBackend()
        gc_states: list[bool] = []

        # Inject a synthetic dispatch path that records gc state when
        # invoked — the outer `_on_notification` must have already
        # disabled GC before `_dispatch_notification` is called.
        def capture_state(_name: str, _args) -> None:
            gc_states.append(gc.isenabled())

        backend._dispatch_notification = capture_state  # type: ignore[assignment]

        was_enabled = gc.isenabled()
        assert was_enabled, "test precondition: GC must start enabled"

        backend._on_notification("capsule", [{}])

        assert gc_states == [False], (
            "GC must be disabled inside _on_notification (gotcha #10). "
            "The outer wrapper is what closes the race window that "
            "covers pynvim's own allocations before _dispatch_redraw."
        )
        # Post-condition: GC restored to whatever it was before.
        assert gc.isenabled() is was_enabled

    def test_gc_is_disabled_during_dispatch_redraw(self) -> None:
        backend = NvimBackend()
        gc_states: list[bool] = []

        # A redraw handler that inspects GC state at call time.
        original_flush = backend._h_flush

        def record_and_call() -> None:
            gc_states.append(gc.isenabled())
            original_flush()

        backend._h_flush = record_and_call  # type: ignore[assignment]

        # `_h_flush` is called via the _REDRAW_HANDLERS dispatch table
        # which looks up the bound method by name on the class — so we
        # need to monkeypatch the module-level table, not the instance.
        from symmetria_ide import nvim_backend as mod

        saved = mod._REDRAW_HANDLERS["flush"]
        mod._REDRAW_HANDLERS["flush"] = lambda self: record_and_call()
        try:
            # NeoVim wire format: `[event_name, [args], [args], ...]` — for a
            # zero-arg event like `flush`, the batch is `["flush", []]`
            # (one empty call-tuple), NOT `["flush"]` (which would iterate
            # zero calls and never invoke the handler).
            backend._dispatch_redraw([["flush", []]])
        finally:
            mod._REDRAW_HANDLERS["flush"] = saved

        assert gc_states == [False], (
            "GC must be disabled inside _dispatch_redraw (gotcha #10). "
            "The inner guard is defence-in-depth — some tests invoke "
            "_dispatch_redraw directly without going through _on_notification."
        )

    def test_gc_restored_after_on_notification_exception(self) -> None:
        """Exceptions raised inside dispatch MUST NOT leak GC=disabled
        across the handler boundary. The try/finally in the backend
        guarantees this.
        """
        backend = NvimBackend()

        def raising_dispatch(_name: str, _args) -> None:
            raise RuntimeError("synthetic")

        backend._dispatch_notification = raising_dispatch  # type: ignore[assignment]

        was_enabled = gc.isenabled()
        with pytest.raises(RuntimeError):
            backend._on_notification("capsule", [{}])

        assert gc.isenabled() is was_enabled, (
            "GC state must survive an exception inside dispatch — "
            "otherwise a crash here leaves GC disabled forever."
        )

    def test_gc_not_re_enabled_if_it_was_already_disabled(self) -> None:
        """Nested disable → no re-enable on exit (don't clobber outer scope).

        If the pynvim worker already disabled GC for some outer reason
        (unlikely today, but the guard exists), the backend must not
        "re-enable" it at the end of dispatch.
        """
        backend = NvimBackend()
        gc.disable()
        try:
            backend._on_notification("capsule", [{}])
            assert gc.isenabled() is False, (
                "Outer caller disabled GC; backend must not re-enable it."
            )
        finally:
            gc.enable()


# ---------------------------------------------------------------------------
# Notification routing — payload shape validation + signal emission
# ---------------------------------------------------------------------------


class TestNotificationRouting:
    """Each named channel must route to the right signal with the right
    payload, and malformed shapes must be dropped (logged, not raised).
    """

    def test_capsule_routes_to_capsule_updated(self) -> None:
        backend = NvimBackend()
        recorder = _SignalRecorder()
        backend.capsule_updated.connect(recorder)

        payload = {"id": "mode", "label": "N", "value": "NORMAL"}
        backend._dispatch_notification("capsule", [payload])

        assert recorder.payloads == [payload]

    def test_capsule_drops_malformed_payload(self) -> None:
        backend = NvimBackend()
        recorder = _SignalRecorder()
        backend.capsule_updated.connect(recorder)

        # Not a dict → must be logged and dropped, NOT emitted.
        backend._dispatch_notification("capsule", ["not-a-dict"])
        backend._dispatch_notification("capsule", [])

        assert recorder.payloads == []

    def test_completions_routes_to_completions_updated(self) -> None:
        backend = NvimBackend()
        recorder = _SignalRecorder()
        backend.completions_updated.connect(recorder)

        payload = {"items": ["edit", "echo"], "line": "e", "selected": -1}
        backend._dispatch_notification("completions", [payload])

        assert recorder.payloads == [payload]

    def test_scroll_routes_to_viewport_scrolled_with_delta(self) -> None:
        """Scroll payload unpacks `delta` and emits as int."""
        backend = NvimBackend()
        recorder = _SignalRecorder()
        backend.viewport_scrolled.connect(recorder)

        backend._dispatch_notification("scroll", [{"delta": 7}])

        assert recorder.payloads == [7]

    def test_scroll_drops_zero_delta(self) -> None:
        """`delta == 0` must NOT emit — no visual change, don't wake the spring."""
        backend = NvimBackend()
        recorder = _SignalRecorder()
        backend.viewport_scrolled.connect(recorder)

        backend._dispatch_notification("scroll", [{"delta": 0}])

        assert recorder.payloads == []

    def test_scroll_drops_non_int_delta(self) -> None:
        """String or None delta is logged and dropped — never crashes the loop."""
        backend = NvimBackend()
        recorder = _SignalRecorder()
        backend.viewport_scrolled.connect(recorder)

        backend._dispatch_notification("scroll", [{"delta": "bogus"}])
        backend._dispatch_notification("scroll", [{"delta": None}])

        assert recorder.payloads == []

    def test_whichkey_routes_to_whichkey_event(self) -> None:
        backend = NvimBackend()
        recorder = _SignalRecorder()
        backend.whichkey_event.connect(recorder)

        payload = {
            "op": "show",
            "mode": "n",
            "trail": "<leader>",
            "can_go_back": False,
            "items": [],
        }
        backend._dispatch_notification("whichkey", [payload])

        assert recorder.payloads == [payload]

    def test_unknown_notification_is_silent(self) -> None:
        """Unrecognized channel names must not raise — just log & drop."""
        backend = NvimBackend()
        backend._dispatch_notification("never-seen-before", [{}])
        # No assertion needed — absence of exception IS the assertion.


# ---------------------------------------------------------------------------
# Redraw dispatch — unknown events, handler exceptions, batching
# ---------------------------------------------------------------------------


class TestRedrawDispatch:
    """`_dispatch_redraw` iterates batches, calls handlers per-call, and
    must swallow per-call handler exceptions so one bad event doesn't
    kill the redraw loop.
    """

    def test_unknown_event_is_skipped(self) -> None:
        backend = NvimBackend()
        # Just confirms no exception — unknown events have no handler
        # and `handler is None` short-circuits.
        backend._dispatch_redraw([["not_an_event", [1, 2, 3]]])

    def test_handler_exception_does_not_stop_batch(self) -> None:
        """A broken handler on one event must not abort subsequent events."""
        backend = NvimBackend()
        flush_recorder = _SignalRecorder()
        backend.redraw_flushed.connect(flush_recorder)

        # Inject a handler that raises for grid_clear; flush must still fire.
        from symmetria_ide import nvim_backend as mod

        saved = mod._REDRAW_HANDLERS["grid_clear"]

        def broken(_self, *_args, **_kw) -> None:
            raise RuntimeError("synthetic handler failure")

        mod._REDRAW_HANDLERS["grid_clear"] = broken
        try:
            backend._dispatch_redraw(
                [
                    ["grid_clear", [1]],  # will raise — logged, not propagated
                    ["flush", []],  # zero-arg events wrap args as `[]`, not omitted
                ]
            )
        finally:
            mod._REDRAW_HANDLERS["grid_clear"] = saved

        assert flush_recorder.payloads == [None], (
            "Subsequent events in the batch must still fire even when an "
            "earlier handler raises — the try/except inside _dispatch_redraw "
            "is what keeps one bad event from killing the redraw loop."
        )

    def test_flush_emits_redraw_flushed(self) -> None:
        backend = NvimBackend()
        recorder = _SignalRecorder()
        backend.redraw_flushed.connect(recorder)

        # Zero-arg events arrive as `[event, []]` on the wire — see the
        # matching comment in `test_gc_is_disabled_during_dispatch_redraw`.
        backend._dispatch_redraw([["flush", []]])

        assert recorder.payloads == [None]

    def test_batched_calls_apply_multiple_times(self) -> None:
        """A single batch entry can contain many same-event call tuples."""
        backend = NvimBackend()
        # grid_resize is a lightweight redraw handler — use it to count calls.
        call_count = [0]

        from symmetria_ide import nvim_backend as mod

        saved = mod._REDRAW_HANDLERS["grid_resize"]

        def counting(_self, *_args) -> None:
            call_count[0] += 1

        mod._REDRAW_HANDLERS["grid_resize"] = counting
        try:
            backend._dispatch_redraw(
                [["grid_resize", [1, 80, 24], [1, 120, 30], [1, 160, 48]]]
            )
        finally:
            mod._REDRAW_HANDLERS["grid_resize"] = saved

        assert call_count[0] == 3


# ---------------------------------------------------------------------------
# Mode resolution — edge cases that `_resolved_mode_info` guards
# ---------------------------------------------------------------------------


class TestModeInfoResolution:
    """`_resolved_mode_info` returns best-effort — empty dict on any
    ambiguity (no info yet, index out of range, malformed entry).
    """

    def test_empty_mode_info_returns_empty_dict(self) -> None:
        backend = NvimBackend()
        assert backend._resolved_mode_info() == {}

    def test_out_of_range_idx_returns_empty_dict(self) -> None:
        backend = NvimBackend()
        backend._mode_info = [{"cursor_shape": "block"}]
        backend._mode_idx = 99
        assert backend._resolved_mode_info() == {}

    def test_malformed_entry_returns_empty_dict(self) -> None:
        backend = NvimBackend()
        backend._mode_info = ["not-a-dict"]  # type: ignore[list-item]
        backend._mode_idx = 0
        assert backend._resolved_mode_info() == {}

    def test_valid_idx_returns_shallow_copy(self) -> None:
        """Mutations to the returned dict must NOT leak into the worker-owned list."""
        backend = NvimBackend()
        original = {"cursor_shape": "block", "blinkon": 500}
        backend._mode_info = [original]
        backend._mode_idx = 0

        resolved = backend._resolved_mode_info()
        resolved["cursor_shape"] = "mutated"

        assert original["cursor_shape"] == "block", (
            "_resolved_mode_info must return a shallow copy — GUI-side "
            "mutations must not leak back into worker-thread state."
        )

    def test_mode_change_emits_cursor_mode_updated(self) -> None:
        backend = NvimBackend()
        recorder = _SignalRecorder()
        backend.cursor_mode_updated.connect(recorder)
        backend._mode_info = [
            {"cursor_shape": "block"},
            {"cursor_shape": "vertical"},
        ]

        backend._h_mode_change("i", 1)

        assert backend.grid.mode == "i"
        assert recorder.payloads == [{"cursor_shape": "vertical"}]

    def test_mode_info_set_emits_cursor_mode_updated(self) -> None:
        """Re-emit on mode_info_set even if mode_change hasn't landed yet.

        Covers both orderings: mode_info_set before first mode_change
        (startup), and mode_info_set after (`:set guicursor=...`).
        """
        backend = NvimBackend()
        recorder = _SignalRecorder()
        backend.cursor_mode_updated.connect(recorder)

        backend._h_mode_info_set(True, [{"cursor_shape": "block"}])

        assert recorder.payloads == [{"cursor_shape": "block"}]
