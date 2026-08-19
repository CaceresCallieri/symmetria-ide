"""Source contract for Phase-3 thread-rail sleep/resume dispatch."""

from __future__ import annotations

import re
from pathlib import Path

from qml_source import extract_braced_body

REPO_ROOT = Path(__file__).resolve().parents[2]
RAIL = REPO_ROOT / "qml" / "AgentThreadRail.qml"


def _source() -> str:
    return re.sub(
        r"/\*.*?\*/|//[^\n]*",
        "",
        RAIL.read_text(encoding="utf-8"),
        flags=re.S,
    )


def test_rail_activates_live_threads_and_resumes_dead_threads_by_identity() -> None:
    """Spec: the primary action branches on slot and keeps durable identity."""
    source = _source()
    marker = source.index("function activateThread")
    body = extract_braced_body(source, source.index("{", marker))

    assert "controller.focus_agent" in body
    assert "controller.resume_thread" in body
    assert re.search(r"\bslot\s*>\s*0\b", body)
    assert re.search(r"\bthreadId\b", body), (
        "dead-row activation must pass (harness, session_id) durable identity"
    )
    assert re.search(r"activateThread\s*\([^)]*threadId", source)


def test_rail_x_sleeps_a_live_thread_instead_of_deleting_its_history() -> None:
    """Spec: the keyboard-accessible end action routes through sleep_agent."""
    source = _source()
    marker = source.index("function _endThread")
    body = extract_braced_body(source, source.index("{", marker))

    assert "controller.sleep_agent" in body
    assert "controller.close_agent" not in body
    assert re.search(r"Qt\.Key_X.{0,320}_endThread", source, re.S)
