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

ONE EXEMPTION, and it is about the word "shared". A harness that pytest launches
with `subprocess.run([sys.executable, ...])` gets a fresh interpreter and its
own application object, so it has no shared queue to drain and no earlier
module's deletions to run. Qt Quick behaviour cannot be tested any other way —
delivering a synthesised key requires turning the loop — and the alternative,
building a QGuiApplication inside the suite, is the very thing that cannot be
done here (conftest owns a session-scoped QCoreApplication) and would court
this crash directly. See `_is_out_of_process_harness`, whose three conditions
are what stop the exemption from covering an ordinary importable helper.
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


_HARNESS_DIR = _TESTS_DIR / "qml_harness"
_QT_APP_TYPES = frozenset({"QGuiApplication", "QApplication", "QCoreApplication"})


def _is_out_of_process_harness(path: Path, tree: ast.Module) -> bool:
    """Does this module own the event loop it pumps?

    The rule above is about the SHARED loop: pumping the session-scoped
    QCoreApplication runs deletions posted by earlier modules. A harness that
    pytest launches with `subprocess.run([sys.executable, ...])` has neither —
    fresh interpreter, its own application object, no prior modules — so the
    failure mode the guard exists for cannot occur, and a Qt Quick harness
    cannot be written without pumping (there is no other way to let the scene
    graph deliver a synthesised key).

    Exempting by directory alone would be too loose: an ordinary helper
    dropped in `qml_harness/` and IMPORTED by a test module would run inside
    the shared process and be waved through. All three conditions must hold —
    the directory, a `__main__` entry point, and constructing its own
    application object — so a module that could be imported is never exempt.
    """
    if path.parent != _HARNESS_DIR:
        return False
    constructs_app = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _QT_APP_TYPES
        for node in ast.walk(tree)
    )
    # `__name__ == "__main__"` exactly. Matching on the left operand alone
    # accepted `if __name__ != "__main__":` — a guard that runs on IMPORT and
    # not when launched, i.e. precisely the module this exemption must not
    # cover.
    has_main_guard = any(
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "__name__"
        and len(node.test.ops) == 1
        and isinstance(node.test.ops[0], ast.Eq)
        and len(node.test.comparators) == 1
        and isinstance(node.test.comparators[0], ast.Constant)
        and node.test.comparators[0].value == "__main__"
        for node in ast.walk(tree)
    )
    return constructs_app and has_main_guard


def _test_sources() -> list[Path]:
    # Recursive: a non-recursive glob exempts the first `tests/<subdir>/`
    # anyone adds, silently — the exact bug this same change fixed in CI, where
    # `qml/*.qml` had been skipping `qml/browser/` for months.
    return sorted(
        path for path in _TESTS_DIR.rglob("*.py") if "__pycache__" not in path.parts
    )


@pytest.mark.parametrize("path", _test_sources(), ids=lambda p: p.name)
def test_module_does_not_pump_the_event_loop(path: Path):
    source = path.read_text()
    if _is_out_of_process_harness(path, ast.parse(source)):
        pytest.skip("standalone subprocess harness — owns the loop it pumps")
    offenders = _pump_calls(source)
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


class TestTheHarnessExemption:
    """The exemption is the guard's only hole — these keep it that size."""

    _STANDALONE = (
        "app = QGuiApplication(sys.argv)\n"
        "def main():\n    app.processEvents()\n"
        'if __name__ == "__main__":\n    main()\n'
    )

    def test_a_standalone_harness_is_exempt(self):
        path = _HARNESS_DIR / "probe.py"
        assert _is_out_of_process_harness(path, ast.parse(self._STANDALONE))

    def test_the_same_file_outside_the_harness_dir_is_not(self):
        """Directory membership is what marks a file as launched, not run."""
        path = _TESTS_DIR / "probe.py"
        assert not _is_out_of_process_harness(path, ast.parse(self._STANDALONE))

    def test_an_importable_helper_in_the_harness_dir_is_not(self):
        """No `__main__` guard and no application object: this would run inside
        the shared pytest process, which is exactly what the guard covers."""
        source = "def helper(app):\n    app.processEvents()\n"
        path = _HARNESS_DIR / "helper.py"
        assert not _is_out_of_process_harness(path, ast.parse(source))

    def test_an_inverted_main_guard_does_not_exempt(self):
        """`__name__ != "__main__"` runs on IMPORT — the shared-process case."""
        source = (
            "app = QGuiApplication(sys.argv)\n"
            "def main():\n    app.processEvents()\n"
            'if __name__ != "__main__":\n    main()\n'
        )
        path = _HARNESS_DIR / "probe.py"
        assert not _is_out_of_process_harness(path, ast.parse(source))

    def test_a_main_guard_alone_does_not_exempt(self):
        source = 'def main():\n    app.processEvents()\nif __name__ == "__main__":\n    main()\n'
        path = _HARNESS_DIR / "probe.py"
        assert not _is_out_of_process_harness(path, ast.parse(source))
