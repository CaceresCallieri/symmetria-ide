"""Tests for the qmllint ratchet that gates QML commits.

WHAT IS BEING PROTECTED. The hook this replaced could not fail: `qmllint`
exits 0 on warnings, so it printed findings and passed, and a real
`unqualified` violation reached `BrowserPane.qml` with a green pre-commit run.
The whole value of the replacement is that it CAN fail — so the tests that
matter are the ones that would still pass if it silently went back to
approving everything.

The findings themselves are not asserted here (that would be a test of
qmllint). What is asserted is the ratchet's arithmetic and the two places it
must refuse to be lenient: an unlisted file, and a hard error.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_GATE_PATH = _REPO_ROOT / "scripts" / "qmllint_gate.py"


def _load_gate():
    """Import the gate by path — `scripts/` is tooling, not an installed package.

    Adding it to `sys.path` instead would put a directory of loose scripts on
    every test's import path.
    """
    spec = importlib.util.spec_from_file_location("qmllint_gate", _GATE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gate():
    return _load_gate()


def _report(filename: str, warnings: list[dict]) -> dict:
    """A qmllint JSON report, in the shape `_tally` consumes."""
    return {"files": [{"filename": str(_REPO_ROOT / filename), "warnings": warnings}]}


def _warning(category: str, type_: str = "warning") -> dict:
    return {
        "id": category,
        "type": type_,
        "line": 1,
        "column": 1,
        "message": "Unqualified access",
    }


class TestTally:
    def test_findings_are_counted_per_file_and_category(self, gate):
        counts, errors = gate._tally(
            _report(
                "qml/Fake.qml",
                [_warning("unqualified"), _warning("unqualified"), _warning("import")],
            )
        )
        assert counts == {"qml/Fake.qml": {"import": 1, "unqualified": 2}}
        assert errors == []

    def test_paths_are_repo_relative(self, gate):
        """Absolute paths would encode the checkout location, so the same
        content would produce different baselines in the dev and stable
        worktrees and every promotion would show a spurious diff."""
        counts, _ = gate._tally(_report("qml/Fake.qml", [_warning("unqualified")]))
        assert list(counts) == ["qml/Fake.qml"]

    def test_errors_are_separated_from_warnings(self, gate):
        """An error is never baselined. qmllint stops analysing a file it
        cannot parse, so its warning count is not comparable to anything —
        letting an error into the ledger would grandfather in whatever the
        parse failure hid."""
        counts, errors = gate._tally(
            _report("qml/Fake.qml", [_warning("syntax", type_="critical")])
        )
        assert counts == {"qml/Fake.qml": {}}
        assert len(errors) == 1
        assert "syntax" in errors[0]

    def test_an_unknown_severity_is_advisory_not_fatal(self, gate):
        """The severity check is a deny-list on purpose. Inverted, qmllint's
        `debug` type (emitted in some configurations) would be promoted to a
        hard error and block every commit touching the file, with a message
        that reads like a real defect."""
        counts, errors = gate._tally(
            _report("qml/Fake.qml", [_warning("some-new-category", type_="debug")])
        )
        assert errors == []
        assert counts == {"qml/Fake.qml": {"some-new-category": 1}}


class TestRatchet:
    def test_a_count_at_the_baseline_passes(self, gate):
        assert (
            gate.regressions(
                {"a.qml": {"unqualified": 3}}, {"a.qml": {"unqualified": 3}}
            )
            == []
        )

    def test_a_count_below_the_baseline_passes(self, gate):
        """Cleaning up must never require touching the baseline in the same
        commit — otherwise the cheapest way to fix a warning is to not."""
        assert (
            gate.regressions(
                {"a.qml": {"unqualified": 1}}, {"a.qml": {"unqualified": 3}}
            )
            == []
        )

    def test_a_count_above_the_baseline_fails(self, gate):
        failures = gate.regressions(
            {"a.qml": {"unqualified": 4}}, {"a.qml": {"unqualified": 3}}
        )
        assert len(failures) == 1
        assert "3 -> 4" in failures[0]

    def test_an_unlisted_file_has_a_budget_of_zero(self, gate):
        """A new file is the one moment when arriving clean is cheap. Defaulting
        it to its own current count would let any amount of debt in unseen."""
        assert gate.regressions({"new.qml": {"unqualified": 1}}, {}) != []

    def test_an_unlisted_category_has_a_budget_of_zero(self, gate):
        """The per-category split is the point: a file allowed 30
        `missing-property` findings must not thereby be allowed its first
        `unqualified` one."""
        failures = gate.regressions(
            {"a.qml": {"missing-property": 30, "unqualified": 1}},
            {"a.qml": {"missing-property": 30}},
        )
        assert len(failures) == 1
        assert "unqualified" in failures[0]


class TestBaselineFile:
    def test_the_baseline_exists_and_parses(self, gate):
        """Without it every file's budget is zero and the hook fails every
        commit — which is how a gate gets bypassed with `--no-verify` and
        stops existing in practice."""
        data = json.loads(gate.BASELINE_PATH.read_text())
        assert isinstance(data, dict)
        assert data, "an empty baseline would fail every QML commit"

    def test_the_baseline_names_no_untracked_file(self, gate):
        """Catches STALE entries — a deleted or renamed .qml whose row outlives
        it, quietly granting a budget to nothing.

        It does NOT catch the opposite (a baseline regenerated from a partial
        file list, dropping rows): proving that needs a full-tree tally, which
        would make the suite depend on a Qt6 qmllint being installed. CI runs
        `pyside6-qmllint` instead, so the test would fail there for reasons
        unrelated to the code."""
        listed = set(json.loads(gate.BASELINE_PATH.read_text()))
        tracked = {gate._relative(path) for path in gate._tracked_qml_files()}
        assert listed <= tracked, "the baseline names files that are not tracked"

    def test_browser_pane_has_no_outer_id_findings_left(self, gate):
        """The regression that motivated the gate. `BrowserPane.qml` reaches
        for `pane` / `surfaces` / `surfaceHost` from inside an inline
        `Component`; `pragma ComponentBehavior: Bound` is what binds them, and
        dropping it puts eleven `unqualified` findings straight back."""
        data = json.loads(gate.BASELINE_PATH.read_text())
        entry = data.get("qml/browser/BrowserPane.qml", {})
        assert entry.get("unqualified", 0) <= 2, (
            "the ComponentBehavior pragma was likely dropped"
        )
