"""Source-level guards/specs for Phase 1's QML and documentation seams."""

from __future__ import annotations

import re
from pathlib import Path

from qml_source import extract_braced_body

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_agent_pane_repeater_uses_a_model_not_a_dynamic_integer() -> None:
    """Guard: Main.qml binds pane delegates to the append-only registry."""
    source = (REPO_ROOT / "qml" / "Main.qml").read_text(encoding="utf-8")
    repeater_id = source.index("id: agentSlotRepeater")
    declaration = source.rfind("Repeater", 0, repeater_id)
    body = extract_braced_body(source, declaration)
    model_binding = re.search(r"\bmodel\s*:\s*([^\n]+)", body)

    assert model_binding is not None
    expression = model_binding.group(1).strip().rstrip(";")
    assert expression not in {"controller.maxAgentSlots", "maxAgentSlots"}
    assert "agentPane" in expression


def test_ctrl_one_through_five_remain_dense_focus_or_spawn_shortcuts() -> None:
    """Guard: the five quick keys remain a fixed convenience surface."""
    source = (REPO_ROOT / "qml" / "Main.qml").read_text(encoding="utf-8")
    sequence = source.index('sequences: ["Ctrl+" + (index + 1)]')
    declaration = source.rfind("Instantiator", 0, sequence)
    body = extract_braced_body(source, declaration)
    model_binding = re.search(r"\bmodel\s*:\s*([^\n]+)", body)

    assert model_binding is not None
    shortcut_model = model_binding.group(1).strip().rstrip(";")
    assert shortcut_model == "5"
    assert "var order = controller.agentOrder" in body
    assert "controller.focus_agent(order[index])" in body
    assert "menu.open()" in body


def test_claude_documents_both_repeater_growth_measurements() -> None:
    """Guard: the warning records why the two models must stay separate."""
    source = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    normalized = source.replace(" ", "").replace("->", "→")

    assert "3→8" in normalized
    assert "3→9" in normalized
    assert re.search(r"integer.{0,240}(destroy|recreat|kill)", source, re.I | re.S)
    assert re.search(
        r"append[- ]only.{0,240}(zero|no|ningun|ningún).{0,80}(destroy|baja)",
        source,
        re.I | re.S,
    )
    assert "either operation churns delegates and kills live agent CLIs" in source


def test_append_only_measurement_has_an_indexed_reproducible_record() -> None:
    """Guard: the measured invariant ships with its rerun instructions."""
    relative_record = Path(
        ".claude/memory/reference/qt-pyside/append_only_pane_registry.md"
    )
    record_path = REPO_ROOT / relative_record

    assert record_path.is_file(), (
        "the source documentation points at a measurement record that is absent "
        "from this checkout"
    )
    memory_index = (REPO_ROOT / ".claude/memory/MEMORY.md").read_text(encoding="utf-8")
    assert "reference/qt-pyside/append_only_pane_registry.md" in memory_index

    record = record_path.read_text(encoding="utf-8")
    normalized = record.replace(" ", "").replace("->", "→")
    assert re.search(r"\*\*Date:\*\*\s*\d{4}-\d{2}-\d{2}", record)
    assert "**Configuration:**" in record
    assert "**Method:**" in record
    assert "tests/qml_harness/pane_growth_probe.py" in record
    assert "QT_QPA_PLATFORM=offscreen" in record
    assert "3→8" in normalized
    assert "3→9" in normalized
    assert '"destroyed_initial": 0' in record
