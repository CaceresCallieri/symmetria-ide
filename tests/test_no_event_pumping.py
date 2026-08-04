"""No test may pump the shared Qt event loop.

WHY THIS IS A TEST AND NOT A COMMENT. The rule was already written down — in
`.claude/memory/reference/qt-pyside/processevents_shared_app_segv.md`, in
`.claude/rules/`, and in the docstrings of half a dozen test modules — and two
files kept a `_pump_events` helper anyway, because pumping is the obvious way
to collect a queued signal and each file looked fine on its own.

The cost was not obvious either. Those pumps drained the GLOBAL queue of the
session-scoped `QCoreApplication`, running `deleteLater` deletions posted by
earlier QML-heavy modules and tripping the Python-3.14 cyclic-GC-vs-Qt SEGV
(CLAUDE.md gotcha #10). Measured over full-suite runs: 4 failures in 9, arriving
as a hang, as exit 139 and as exit 134 — and the offending file passed in
isolation every single time, so the blame landed repeatedly on whatever had
been edited last.

A grep is enough to hold the line, and unlike the prose it fails at the moment
the pump is written.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent

# Method names that run the global event queue. Everything past
# `processEvents` is here because it pumps just as thoroughly, and forbidding
# only the famous one moves the fault under a name nobody greps for.
#
# `sendPostedEvents` deserves its place at the front: narrowing the pump to
# `sendPostedEvents(None, QEvent.Type.MetaCall)` was TRIED as a fix for the
# original crash and did not work (3/3 still failed), so it is a known and
# tempting wrong turn — and `sendPostedEvents(None, DeferredDelete)` would run
# precisely the deletions this whole guard exists to prevent.
_PUMPING_METHODS = frozenset(
    {
        "processEvents",
        "sendPostedEvents",
        "qWait",
        "qWaitFor",
        "exec",
        "exec_",
    }
)


def _pump_calls(source: str) -> list[str]:
    """Lines calling a pumping method, found via the AST.

    Deliberately not a regex over the text: every module here NAMES
    `processEvents` in prose explaining why it must not be called, and a
    substring match would forbid documenting the rule — which is how a
    well-meant guard ends up deleted.
    """
    tree = ast.parse(source)
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Attribute AND bare-name calls: `p = app.processEvents; p()` rebinds
        # the method and would slip past an attribute-only check.
        if isinstance(func, ast.Attribute) and func.attr in _PUMPING_METHODS:
            found.append(f"line {node.lineno}: .{func.attr}()")
        elif isinstance(func, ast.Name) and func.id in _PUMPING_METHODS:
            found.append(f"line {node.lineno}: {func.id}()")
    return found


def _test_sources() -> list[Path]:
    # Recursive: a non-recursive glob exempts the first `tests/<subdir>/`
    # anyone adds, silently — the exact bug this same change fixed in CI, where
    # `qml/*.qml` had been skipping `qml/browser/` for months.
    return sorted(
        path for path in _TESTS_DIR.rglob("*.py") if "__pycache__" not in path.parts
    )


@pytest.mark.parametrize("path", _test_sources(), ids=lambda p: p.name)
def test_module_does_not_pump_the_event_loop(path: Path):
    offenders = _pump_calls(path.read_text())
    assert not offenders, (
        f"{path.name} pumps the shared event loop, which runs prior modules' "
        "deleteLater deletions and SEGVs mid-suite (gotcha #10). Spy with an "
        "explicit Qt.ConnectionType.DirectConnection and wait with "
        "conftest.wait_until.\n" + "\n".join(offenders)
    )


class TestTheDetector:
    """Without these the file passes just as happily with a broken matcher."""

    def test_a_real_pump_is_found(self):
        assert _pump_calls("QCoreApplication.processEvents()")
        assert _pump_calls("QTest.qWait(100)")
        assert _pump_calls("app.exec()")

    def test_the_narrowed_pump_is_found(self):
        """The one that was actually tried as a fix and did not work — so it is
        the likeliest to be reached for again."""
        assert _pump_calls("QCoreApplication.sendPostedEvents(None, MetaCall)")
        assert _pump_calls("QTest.qWaitFor(lambda: done)")

    def test_a_rebound_pump_is_found(self):
        """`p = app.processEvents; p()` defeats an attribute-only matcher."""
        assert _pump_calls("processEvents()")

    def test_prose_about_the_rule_is_not_a_pump(self):
        """The rule has to stay explainable in the files it governs."""
        assert not _pump_calls('"""never call QCoreApplication.processEvents()"""')
        assert not _pump_calls("x = 1  # no QCoreApplication.processEvents() here")

    def test_an_unrelated_call_is_not_a_pump(self):
        assert not _pump_calls("server.stop()")
