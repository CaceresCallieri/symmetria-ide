"""Tests for the startup-phase tracer (`src/symmetria_ide/trace.py`).

The tracer is env-gated by `SYMMETRIA_IDE_TRACE`. These tests verify:
- when disabled, `trace()` is a zero-side-effect no-op
- when enabled, `trace()` writes `[TRACE] <ms> <name>` to stderr
- `T0` is captured at module import and `elapsed_ms()` is monotonic-positive

We deliberately do NOT test the integration with `__main__.py` / `app.py`
here — the bench harness covers that path end-to-end via the `--trace`
flag, and the unit tests would have to re-execute the module imports
in a subprocess to faithfully exercise it. Keeping the unit tests
focused on the tracer's own contract.
"""

from __future__ import annotations

import importlib
import io
import re
import sys
from contextlib import redirect_stderr


def _fresh_trace_module(monkeypatch, enable: bool):
    """Re-import `symmetria_ide.trace` with the env var set as specified.

    `T0` is captured at module import — to test the disabled path
    without polluting prior imports we forcibly remove the cached
    module from `sys.modules` first.
    """
    if enable:
        monkeypatch.setenv("SYMMETRIA_IDE_TRACE", "1")
    else:
        monkeypatch.delenv("SYMMETRIA_IDE_TRACE", raising=False)
    sys.modules.pop("symmetria_ide.trace", None)
    return importlib.import_module("symmetria_ide.trace")


def test_trace_disabled_is_silent(monkeypatch):
    """With env unset, `trace()` writes nothing to stderr."""
    tr = _fresh_trace_module(monkeypatch, enable=False)
    buf = io.StringIO()
    with redirect_stderr(buf):
        tr.trace("phase_one")
        tr.trace("phase_two")
    assert buf.getvalue() == ""


def test_trace_disabled_falsy_values(monkeypatch):
    """`SYMMETRIA_IDE_TRACE=0` / `=false` / `=` all disable tracing.

    The tracer's enable check rejects these falsy strings explicitly
    so users who set the env to `0` thinking it's a boolean flag
    don't accidentally get the noisy path.
    """
    for falsy in ("0", "false", ""):
        monkeypatch.setenv("SYMMETRIA_IDE_TRACE", falsy)
        sys.modules.pop("symmetria_ide.trace", None)
        tr = importlib.import_module("symmetria_ide.trace")
        buf = io.StringIO()
        with redirect_stderr(buf):
            tr.trace("phase")
        assert buf.getvalue() == "", f"falsy={falsy!r} leaked output"


def test_trace_enabled_emits_to_stderr(monkeypatch):
    """With env set, `trace()` writes one `[TRACE] <ms> <name>` line."""
    tr = _fresh_trace_module(monkeypatch, enable=True)
    buf = io.StringIO()
    with redirect_stderr(buf):
        tr.trace("hello")
    out = buf.getvalue()
    # Format: `[TRACE]   <ms>.<frac> hello\n` — at least one space + a
    # numeric token + the name. Loose match to avoid coupling tests to
    # the exact ms-formatting width chosen in trace.py.
    assert out.startswith("[TRACE]")
    assert " hello\n" in out
    # Verify the middle token is a valid float (catches format regressions
    # where the ms value could become None/NaN/empty).
    assert re.search(r"[0-9]+\.[0-9]+", out), f"no numeric ms token in: {out!r}"


def test_elapsed_ms_is_monotonic_positive(monkeypatch):
    """`elapsed_ms()` returns a positive monotonic value.

    The clock used (`time.monotonic`) is guaranteed non-decreasing,
    so two consecutive calls must satisfy second >= first.
    """
    tr = _fresh_trace_module(monkeypatch, enable=False)
    first = tr.elapsed_ms()
    second = tr.elapsed_ms()
    assert first >= 0.0
    assert second >= first
