"""Behavioural contract for the two-stage agent spawn chooser."""

from __future__ import annotations

import json
import os
import re
import runpy
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from symmetria_ide.agent_harness import HARNESSES, MENU_ORDERED_HARNESSES
from symmetria_ide.app import AppController

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "qml_harness" / "spawn_menu_probe.py"


def _extract_braced_body(text: str, declaration_start: int) -> str:
    """Return a QML declaration body without relying on a character window."""
    open_index = text.index("{", declaration_start)
    depth = 0
    index = open_index
    quote: str | None = None
    while index < len(text):
        character = text[index]
        if quote is not None:
            if character == "\\":
                index += 2
                continue
            if character == quote:
                quote = None
        elif character in "'\"`":
            quote = character
        elif character == "/" and text[index + 1 : index + 2] == "/":
            newline = text.find("\n", index)
            index = len(text) if newline == -1 else newline
            continue
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return text[open_index : index + 1]
        index += 1
    raise AssertionError(f"unbalanced QML braces from index {declaration_start}")


def _shortcut_body(source: str, sequence: str) -> str:
    sequence_index = source.index(f'sequences: ["{sequence}"]')
    declaration_start = source.rfind("Shortcut", 0, sequence_index)
    assert declaration_start >= 0, f"Shortcut for {sequence} not found"
    return _extract_braced_body(source, declaration_start)


def _spawn_menu_receivers(source: str) -> tuple[str, ...]:
    """Accept direct access or a lint-motivated local alias as equivalent."""
    receivers = ["agentSpawnMenu"]
    alias = re.search(r"\bvar\s+(\w+)\s*=\s*agentSpawnMenu\s*;", source)
    if alias is not None:
        receivers.append(alias.group(1))
    return tuple(receivers)


def _without_line_comments(source: str) -> str:
    """Remove explanatory QML comments before matching adjacent statements."""
    return re.sub(r"//[^\n]*", "", source)


@pytest.fixture(scope="module")
def probe() -> dict[str, object]:
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    completed = subprocess.run(
        [sys.executable, str(PROBE)],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, (
        f"spawn-menu probe exited {completed.returncode}\n"
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    return json.loads(completed.stdout)


def test_menu_opens_at_harness_stage(probe):
    assert probe["open"] == {"visible": True, "stage": 0, "harness": ""}


def test_unavailable_harness_is_visible_dimmed_and_non_selecting(probe):
    rows = probe["stage0_rows"]
    assert rows["pi"] is not None
    assert rows["opencode"] is not None, "unavailable rows must stay in the chooser"
    assert rows["opencode"]["color"] != rows["pi"]["color"], (
        "the unavailable OpenCode row must be marked by colour"
    )
    assert rows["opencode"]["opacity"] == rows["pi"]["opacity"], (
        "dim colour alone — stacking an opacity reduction on top of it took "
        "the row to ~1.8:1 contrast, i.e. unreadable"
    )
    assert probe["unavailable_opencode"] == {
        "visible": True,
        "stage": 0,
        "harness": "",
    }


def test_unavailable_harness_row_states_its_reason(probe):
    """Its key does nothing when pressed; the label is what explains why."""
    labels = probe["stage0_labels"]
    assert labels["pi"] == "Pi"
    assert labels["opencode"] == "OpenCode (not installed)"


def test_availability_is_re_read_when_the_chooser_opens(probe):
    """`agentHarnessCatalog` notifies; opening the menu is what re-reads PATH.

    A constant property would leave the first read frozen for the session, so
    a harness installed mid-session could never light up.
    """
    assert probe["catalog_before_refresh"] == [True, True, False]
    assert probe["catalog_after_refresh"] == [True, True, True]
    assert probe["refresh_calls"] > 0


def test_probe_catalog_rows_match_the_production_projection_contract():
    """The QML double must grow whenever the AppController row shape grows."""
    namespace = runpy.run_path(str(PROBE))
    probe_rows = cast(tuple[dict[str, object], ...], namespace["_HARNESS_METADATA"])
    availability = {harness.name: True for harness in MENU_ORDERED_HARNESSES}
    projector = cast(AppController, SimpleNamespace(_harness_available=availability))
    production_rows = [
        AppController._harness_row(projector, harness)
        for harness in MENU_ORDERED_HARNESSES
    ]
    probe_rows_with_availability = [{**row, "available": True} for row in probe_rows]

    assert [row["name"] for row in probe_rows_with_availability] == [
        row["name"] for row in production_rows
    ]
    assert [set(row) for row in probe_rows_with_availability] == [
        set(row) for row in production_rows
    ]


def test_header_names_the_question_each_stage_asks(probe):
    assert probe["header_stage0"]["title"] == "Choose harness for agent #1"
    assert probe["header_stage0"]["icon"]["visible"] is False
    assert probe["header_stage1"]["title"] == "Spawn agent #1"


def test_pi_selection_and_spawn_polarity(probe):
    assert probe["select_pi"] == {"visible": True, "stage": 1, "harness": "pi"}
    assert probe["pi_stage1"]["resume_label_present"] is True
    # The HEADER icon specifically — "some pi mark in the subtree" was also
    # satisfied by a stage-0 row and proved nothing about the header.
    header_icon = probe["header_stage1"]["icon"]
    assert header_icon["visible"] is True
    assert header_icon["source"].endswith("/assets/pi-icon.svg")
    assert probe["pi_new"] == [["fresh", True, "pi", ""]]
    assert probe["pi_new_safe"] == [["fresh", False, "pi", ""]]


def test_same_key_selects_claude_then_continues_it(probe):
    assert probe["select_claude"] == {
        "visible": True,
        "stage": 1,
        "harness": "claude",
    }
    assert probe["claude_continue"] == [["continue", True, "claude", ""]]


def test_escape_steps_back_then_dismisses(probe):
    assert probe["escape_stage1"] == {
        "visible": True,
        "stage": 0,
        "harness": "",
    }
    assert probe["escape_stage0"]["visible"] is False


def test_reassert_preserves_state_while_open_resets_it(probe):
    assert probe["reassert"] == {"visible": True, "stage": 1, "harness": "pi"}
    assert probe["reopen"] == {"visible": True, "stage": 0, "harness": ""}


def test_resume_routing_is_capability_driven(probe):
    assert probe["pi_resume"] == {
        "spawns": [["resume", True, "pi", ""]],
        "signals": [],
    }
    assert probe["select_opencode"] == {
        "visible": True,
        "stage": 1,
        "harness": "opencode",
    }
    assert probe["opencode_resume"] == {
        "spawns": [],
        "signals": [["opencode", True]],
    }


def test_location_toggle_while_open_restarts_the_wizard(probe):
    """Ctrl+Shift+U can fire with the chooser up, and vps is claude-only.

    Keeping a stage-0 Pi selection there would let the user press `n` and only
    learn the spawn was refused after the menu had closed.
    """
    assert probe["vps_toggled_while_open"] == {
        "visible": True,
        "stage": 1,
        "harness": "claude",
    }
    assert probe["local_toggled_while_open"] == {
        "visible": True,
        "stage": 0,
        "harness": "",
    }


def test_vps_skips_harness_stage_and_keeps_attach(probe):
    assert probe["vps_open"] == {
        "visible": True,
        "stage": 1,
        "harness": "claude",
    }
    assert probe["vps_attach"] == [[True]]


def test_direct_dismiss_still_closes_from_action_stage(probe):
    assert probe["direct_dismiss_stage1"]["visible"] is False


def test_spawn_attempt_emits_dismissed_after_dispatch_to_restore_focus(probe):
    """A controller no-op must not leave focus orphaned behind a hidden menu."""
    assert probe["spawn_focus_restore"] == {
        "events": ["spawn", "dismissed"],
        "spawns": [["fresh", True, "pi", ""]],
        "visible": False,
    }


def test_session_picker_open_sets_and_resets_the_resume_request(probe):
    """The two-argument entry point owns setup; callers must not pre-seed it."""
    assert probe["session_picker_open"] == {
        "harness": "pi",
        "dangerous": False,
        "state": "loading",
        "sessions": [],
        "selected_index": 0,
        "session_requests": 1,
        "visible": True,
    }


def test_session_picker_spawns_its_configured_harness(probe):
    assert probe["session_picker"]["spawns"] == [["resume", False, "pi", "ses_probe"]]
    assert any("pi" in title.lower() for title in probe["session_picker"]["titles"])


def test_main_threads_resume_harness_into_session_picker():
    """The spawn menu's `r` handoff must carry the harness, not assume one.

    Without it the picker keeps whatever harness it last held (default
    OpenCode) and spawns THAT for a resume raised by a different one.
    """
    main_qml = (REPO_ROOT / "qml" / "Main.qml").read_text()
    handoff = re.search(
        r"onResumePickerRequested:\s*\(\s*harness\s*,\s*dangerous\s*\)\s*=>"
        r"[\s\S]{0,200}?agentSessionPicker\.open\(\s*harness\s*,\s*dangerous\s*\)",
        main_qml,
    )
    assert handoff is not None, (
        "Main.qml must route resumePickerRequested(harness, dangerous) into "
        "agentSessionPicker.open(harness, dangerous) — the guarded handoff "
        "that makes the picker spawn the harness that raised it"
    )


def test_ctrl_number_on_an_empty_slot_preserves_an_open_choice():
    """Ctrl+1..5 is exempt from the modal guard so the menu stays usable.

    Reaching for an empty slot with the chooser already up must therefore
    re-assert focus, not call open() — open() restarts the wizard and discards
    a stage-1 harness the user had already picked.
    """
    main_qml = (REPO_ROOT / "qml" / "Main.qml").read_text()
    instantiator_index = main_qml.index("model: controller.maxAgentSlots")
    declaration_start = main_qml.rfind("Instantiator", 0, instantiator_index)
    dispatch = _extract_braced_body(main_qml, declaration_start)
    executable_dispatch = _without_line_comments(dispatch)
    preserves_choice = any(
        re.search(
            rf"else\s+if\s*\(\s*{receiver}\.visible\s*\)\s*\{{?\s*"
            rf"{receiver}\.reassert\(\)\s*;[\s\S]*?"
            rf"else\s*\{{\s*{receiver}\.open\(\)\s*;",
            executable_dispatch,
        )
        is not None
        for receiver in _spawn_menu_receivers(dispatch)
    )
    assert preserves_choice, (
        "the empty-slot branch must reassert() an already-visible chooser and "
        "open() only when it is closed"
    )


def test_ctrl_shift_a_reasserts_an_open_chooser_without_resetting_stage():
    """Repeating the menu chord must preserve the selected harness."""
    main_qml = (REPO_ROOT / "qml" / "Main.qml").read_text()
    shortcut = _shortcut_body(main_qml, "Ctrl+Shift+A")
    executable_shortcut = _without_line_comments(shortcut)
    preserves_choice = any(
        re.search(
            rf"if\s*\(\s*{receiver}\.visible\s*\)\s*\{{?\s*"
            rf"{receiver}\.reassert\(\)\s*;[\s\S]*?"
            rf"else\s+if\s*\([\s\S]*?controller\.focusedAgent\s*===\s*0"
            rf"[\s\S]*?controller\.agentSurfaceVisible[\s\S]*?\)\s*\{{?\s*"
            rf"{receiver}\.open\(\)\s*;",
            executable_shortcut,
        )
        is not None
        for receiver in _spawn_menu_receivers(shortcut)
    )
    assert preserves_choice, (
        "Ctrl+Shift+A must reassert() a visible chooser; open() resets the "
        "wizard and discards a stage-1 Pi selection"
    )


def test_modal_overlay_routes_escape_through_overridable_seam():
    source = (REPO_ROOT / "qml" / "ModalOverlay.qml").read_text()
    assert "function handleEscape()" in source
    assert "overlayRoot.handleEscape()" in source


def test_pi_icon_has_baked_white_fill():
    """Read through the registry — the asset the menu actually renders.

    A hardcoded path here would keep passing against an orphaned file if the
    registry entry were ever repointed.
    """
    icon = REPO_ROOT / "qml" / HARNESSES["pi"].icon
    source = icon.read_text()
    assert "#ffffff" in source.lower()
    assert "currentcolor" not in source.lower()
