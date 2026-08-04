"""Tests for the pure agent-coordination module (wait_for_agent triggers).

Everything here is Qt-free by design (agent_coordination.py mirrors the
agent_harness / project_browser_marker style): the trigger engine, the
transcript-path encoding, the tail extractor, the judge prompt/verdict
handling, and the injection templates. The AppController wiring (settle
timers, judge thread, inject retry) is exercised separately.
"""

from __future__ import annotations

import json

from symmetria_ide import agent_coordination as coord

# ---------------------------------------------------------------------------
# TriggerEngine
# ---------------------------------------------------------------------------


def _register(engine, watched=1, registrant=2, note="", busy=True, has_session=True):
    return engine.register(
        watched,
        registrant,
        note,
        watched_has_session=has_session,
        watched_is_busy=busy,
        now=1000.0,
    )


def test_register_arms_while_watched_busy():
    engine = coord.TriggerEngine()
    result = _register(engine, busy=True)
    assert result.status == "armed"
    assert result.error == ""
    assert len(engine.triggers) == 1
    assert not engine.triggers[0].evaluating


def test_register_evaluates_immediately_when_already_idle_with_session():
    """Watched already idle + has run → judge now (status "evaluating"),
    marked in-flight so the next idle edge can't double-fire it."""
    engine = coord.TriggerEngine()
    result = _register(engine, busy=False, has_session=True)
    assert result.status == "evaluating"
    assert result.trigger.evaluating


def test_register_arms_when_idle_but_never_ran():
    """No session yet (agent never ran) → nothing to judge; arm for the
    first real idle edge."""
    engine = coord.TriggerEngine()
    result = _register(engine, busy=False, has_session=False)
    assert result.status == "armed"


def test_register_rejects_self_wait():
    engine = coord.TriggerEngine()
    result = _register(engine, watched=2, registrant=2)
    assert result.error
    assert engine.triggers == []


def test_reregister_same_pair_replaces_note():
    engine = coord.TriggerEngine()
    _register(engine, note="first")
    _register(engine, note="second")
    assert len(engine.triggers) == 1
    assert engine.triggers[0].note == "second"


def test_reregister_surfaces_replaced_trigger():
    """The caller must reconcile in-flight judge routing keyed on the old
    trigger's id — register() surfaces the replaced trigger for that."""
    engine = coord.TriggerEngine()
    first = _register(engine, note="first")
    assert first.replaced is None
    second = _register(engine, note="second")
    assert second.replaced is not None
    assert second.replaced.id == first.trigger.id
    # Different pairs never report a replacement.
    other = _register(engine, watched=4, registrant=2)
    assert other.replaced is None


def test_on_agent_idle_returns_and_marks_candidates():
    engine = coord.TriggerEngine()
    _register(engine, watched=1, registrant=2)
    _register(engine, watched=1, registrant=3)
    _register(engine, watched=4, registrant=2)  # different watched slot
    candidates = engine.on_agent_idle(1)
    assert {t.registrant_slot for t in candidates} == {2, 3}
    assert all(t.evaluating for t in candidates)
    # A second idle edge while judging returns nothing (no double-fire).
    assert engine.on_agent_idle(1) == []


def test_rearm_and_complete():
    engine = coord.TriggerEngine()
    _register(engine, watched=1, registrant=2)
    (trigger,) = engine.on_agent_idle(1)
    engine.rearm(trigger)
    assert not trigger.evaluating
    assert engine.on_agent_idle(1) == [trigger]  # re-armed → candidate again
    engine.complete(trigger)
    assert engine.triggers == []


def test_on_agent_closed_partitions_orphaned_and_discarded():
    """Closing an agent drops every trigger involving it: watched-side drops
    are orphaned (registrant should be told), registrant-side ones vanish."""
    engine = coord.TriggerEngine()
    _register(engine, watched=1, registrant=2)  # orphaned when 1 closes
    _register(engine, watched=3, registrant=1)  # discarded when 1 closes
    _register(engine, watched=3, registrant=4)  # untouched
    result = engine.on_agent_closed(1)
    assert [t.registrant_slot for t in result.orphaned] == [2]
    assert [t.watched_slot for t in result.discarded] == [3]
    assert len(engine.triggers) == 1
    assert engine.triggers[0].registrant_slot == 4


# ---------------------------------------------------------------------------
# Transcript path + tail extraction
# ---------------------------------------------------------------------------


def test_claude_transcript_path_encoding(monkeypatch):
    """cwd encoding: `/` AND `.` both map to `-` (verified live — see the
    function docstring); basename is the session UUID."""
    monkeypatch.setenv("HOME", "/home/jc")
    path = coord.claude_transcript_path("/home/jc/projects/symmetria-ide", "abc-123")
    assert path == (
        "/home/jc/.claude/projects/-home-jc-projects-symmetria-ide/abc-123.jsonl"
    )
    dotted = coord.claude_transcript_path("/home/jc/.config/quickshell", "s1")
    assert "/-home-jc--config-quickshell/" in dotted


def test_claude_transcript_path_empty_inputs():
    assert coord.claude_transcript_path("", "abc") == ""
    assert coord.claude_transcript_path("/x", "") == ""


def _write_transcript(tmp_path, lines):
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines))
    return str(path)


def test_extract_transcript_tail_flattens_roles_and_skips_tools(tmp_path):
    path = _write_transcript(
        tmp_path,
        [
            {"type": "user", "message": {"role": "user", "content": "do the thing"}},
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    # Block-list content: text blocks surface, tool_use skipped.
                    "content": [
                        {"type": "text", "text": "working on it"},
                        {"type": "tool_use", "name": "Bash", "input": {}},
                    ],
                },
            },
            {
                # tool_result-only user envelope → no text → skipped entirely.
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "content": "ok"}],
                },
            },
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": "done, all tests pass"},
            },
        ],
    )
    tail = coord.extract_transcript_tail(path)
    assert "[user]: do the thing" in tail
    assert "[assistant]: working on it" in tail
    assert "[assistant]: done, all tests pass" in tail
    assert "tool_result" not in tail


def test_extract_transcript_tail_limits_messages(tmp_path):
    path = _write_transcript(
        tmp_path,
        [
            {"type": "assistant", "message": {"role": "assistant", "content": f"m{i}"}}
            for i in range(30)
        ],
    )
    tail = coord.extract_transcript_tail(path, max_messages=5)
    assert "m29" in tail
    assert "m24" not in tail  # only the last 5 survive


def test_extract_transcript_tail_missing_file():
    assert coord.extract_transcript_tail("/nonexistent/x.jsonl") == ""
    assert coord.extract_transcript_tail("") == ""


# ---------------------------------------------------------------------------
# Judge prompt + verdict parsing
# ---------------------------------------------------------------------------


def test_build_judge_prompt_carries_note_and_tail():
    prompt = coord.build_judge_prompt("[assistant]: done", "tests green")
    assert "tests green" in prompt
    assert "[assistant]: done" in prompt
    assert '"complete"' in prompt and '"needs_user"' in prompt


def _cli_envelope(result_text):
    """Wrap verdict text the way `claude -p --output-format json` does."""
    return json.dumps({"type": "result", "result": result_text})


def test_parse_judge_output_clean():
    verdict = coord.parse_judge_output(
        _cli_envelope('{"status": "complete", "summary": "all done"}')
    )
    assert verdict.status == coord.VERDICT_COMPLETE
    assert verdict.summary == "all done"


def test_parse_judge_output_fenced():
    verdict = coord.parse_judge_output(
        _cli_envelope('```json\n{"status": "in_progress", "summary": "mid"}\n```')
    )
    assert verdict.status == coord.VERDICT_IN_PROGRESS


def test_parse_judge_output_bare_verdict_json():
    """A bare verdict object (no CLI wrapper) is accepted directly."""
    verdict = coord.parse_judge_output('{"status": "needs_user", "summary": "q"}')
    assert verdict.status == coord.VERDICT_NEEDS_USER
    assert verdict.summary == "q"


def test_parse_judge_output_garbage_is_needs_user():
    """Every failure mode degrades to needs_user — never a silent go-ahead."""
    for stdout in (
        "",
        "not json at all",
        _cli_envelope("the agent seems finished"),  # prose, not JSON
        _cli_envelope('{"status": "banana"}'),  # off-schema status
        json.dumps({"type": "result"}),  # envelope without result
    ):
        assert coord.parse_judge_output(stdout).status == coord.VERDICT_NEEDS_USER


def test_judge_argv_shape():
    argv = coord.judge_argv("PROMPT")
    assert argv[0] == "claude"
    assert "-p" in argv and "PROMPT" in argv
    assert "--output-format" in argv and "json" in argv
    assert "--max-turns" in argv


# ---------------------------------------------------------------------------
# Injection templates
# ---------------------------------------------------------------------------


def test_injection_templates_mention_agent_and_note():
    go = coord.goahead_text(3, "finished cleanly", "api merged")
    assert "#3" in go and "api merged" in go and "proceed" in go.lower()
    hold = coord.hold_text(3, "asked the user a question", "api merged")
    assert "#3" in hold and "hold" in hold.lower()
    cancelled = coord.cancelled_text(3, "")
    assert "#3" in cancelled and "cancelled" in cancelled.lower()
    caveat = coord.opencode_caveat_text(3, "api merged")
    assert "#3" in caveat and "opencode" in caveat.lower()
