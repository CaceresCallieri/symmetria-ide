"""Smoke tests for the headless TerminalBackend REPL (term_repl).

Verifies the JSONL protocol mechanics — command parsing, ack envelopes,
snapshot shape, clean shutdown. Does NOT assert on shell-rendered
content (that requires async waits; deferred until `wait_for` lands).

The tests spawn `python -m symmetria_ide.term_repl` as a real subprocess
to exercise the full stdin/stdout JSONL surface — mirrors how external
clients (debug scripts, future agent tooling) will invoke it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"


def _run_repl(
    commands: list[dict],
    timeout: float = 8.0,
    raw_prefix: str = "",
) -> list[dict]:
    """Spawn term_repl, send each command on its own line, parse the
    JSONL stdout. Returns the list of event objects in arrival order.

    ``raw_prefix`` is prepended verbatim before the JSON-serialised
    commands — use it to inject malformed or non-JSON lines that
    ``json.dumps`` cannot produce (e.g. for parse-error tests).
    """
    payload = raw_prefix + "\n".join(json.dumps(cmd) for cmd in commands) + "\n"
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(SRC_DIR), os.environ.get("PYTHONPATH", "")])
    )
    result = subprocess.run(
        [sys.executable, "-m", "symmetria_ide.term_repl"],
        input=payload,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    events: list[dict] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            # Surface bad stdout lines in test failures rather than crashing the helper.
            raise AssertionError(
                f"term_repl emitted non-JSON stdout line: {line!r}\n"
                f"stderr: {result.stderr}"
            ) from None
    return events


def _by_id(events: list[dict], req_id: str) -> list[dict]:
    return [e for e in events if e.get("id") == req_id]


# ---------------------------------------------------------------------------
# Protocol mechanics
# ---------------------------------------------------------------------------


def test_start_then_stop_emits_two_acks():
    """Minimal lifecycle: start the shell, then stop. Each command must
    be acknowledged with its `id` preserved."""
    events = _run_repl(
        [
            {"id": "1", "type": "start", "cwd": "/tmp"},
            {"id": "2", "type": "stop"},
        ]
    )
    starts = _by_id(events, "1")
    stops = _by_id(events, "2")
    assert len(starts) == 1
    assert starts[0]["type"] == "ack"
    assert starts[0]["status"] == "ok"
    assert len(stops) == 1
    assert stops[0]["type"] == "ack"
    assert stops[0]["status"] == "ok"
    # EOF-triggered synthetic stop must NOT emit a trailing null-id ack:
    # the client has already closed stdin so the ack would be unread
    # noise. Without suppression, automated test harnesses would see a
    # spurious `{"id": null, "type": "ack"}` after the legitimate stop.
    null_acks = [e for e in events if e.get("id") is None and e.get("type") == "ack"]
    assert null_acks == []


def test_unknown_command_emits_error_with_command_type():
    """Unknown command types must produce an `error` event carrying the
    offending command_type — clients rely on this for diagnostic
    surfacing in their own error handlers."""
    events = _run_repl([{"id": "x", "type": "frobnicate"}])
    errors = _by_id(events, "x")
    assert len(errors) == 1
    assert errors[0]["type"] == "error"
    assert errors[0]["command_type"] == "frobnicate"
    assert "frobnicate" in errors[0]["message"]


def test_malformed_json_does_not_crash_repl():
    """Garbage on stdin must produce an error event, NOT terminate the
    REPL — the next valid command should still process."""
    events = _run_repl(
        [{"id": "ok", "type": "stop"}],
        raw_prefix="{not valid json\n",
    )
    # The malformed line emits an error event with no id.
    parse_errors = [
        e
        for e in events
        if e.get("type") == "error" and "json parse" in e.get("message", "")
    ]
    assert len(parse_errors) >= 1
    # The valid stop after still ACKs.
    stop_acks = [e for e in events if e.get("id") == "ok" and e.get("type") == "ack"]
    assert len(stop_acks) == 1


def test_snapshot_before_start_emits_error():
    """A snapshot request without an active shell must produce an
    error envelope, not crash or hang."""
    events = _run_repl(
        [
            {"id": "snap", "type": "snapshot"},
            {"id": "stop", "type": "stop"},
        ]
    )
    snap = _by_id(events, "snap")
    assert len(snap) == 1
    assert snap[0]["type"] == "error"
    assert "start" in snap[0]["message"].lower()


# ---------------------------------------------------------------------------
# Snapshot shape (content asserts deferred until wait_for lands)
# ---------------------------------------------------------------------------


def test_snapshot_shape_includes_required_fields():
    """A snapshot after start must return rows / cursor / cols /
    rows_count — content may be empty (race with shell startup), but
    the envelope shape is contractual."""
    events = _run_repl(
        [
            {"id": "1", "type": "start", "cwd": "/tmp"},
            {"id": "2", "type": "snapshot"},
            {"id": "3", "type": "stop"},
        ]
    )
    snap = _by_id(events, "2")
    assert len(snap) == 1
    payload = snap[0]
    assert payload["type"] == "snapshot"
    assert isinstance(payload["rows"], list)
    assert payload["cols"] == 80  # _DEFAULT_COLS
    assert payload["rows_count"] == 24  # _DEFAULT_ROWS
    assert len(payload["rows"]) == 24
    assert isinstance(payload["cursor"], list)
    assert len(payload["cursor"]) == 2


def test_resize_changes_snapshot_dimensions():
    """After resize, subsequent snapshots reflect the new geometry —
    proves the resize command actually reaches the backend."""
    events = _run_repl(
        [
            {"id": "1", "type": "start", "cwd": "/tmp"},
            {"id": "2", "type": "resize", "cols": 120, "rows": 40},
            {"id": "3", "type": "snapshot"},
            {"id": "4", "type": "stop"},
        ]
    )
    snap = _by_id(events, "3")
    assert len(snap) == 1
    assert snap[0]["cols"] == 120
    assert snap[0]["rows_count"] == 40
    assert len(snap[0]["rows"]) == 40


# ---------------------------------------------------------------------------
# Encoding paths
# ---------------------------------------------------------------------------


def test_write_b64_decodes_bytes():
    """Base64-encoded payloads must decode to raw bytes — the ack-only
    test proves the command parses + accepts the payload; full byte
    delivery is verified once wait_for lands."""
    # base64("\x05") == "BQ=="
    events = _run_repl(
        [
            {"id": "1", "type": "start", "cwd": "/tmp"},
            {"id": "2", "type": "write_b64", "data_b64": "BQ=="},
            {"id": "3", "type": "stop"},
        ]
    )
    write_acks = _by_id(events, "2")
    assert len(write_acks) == 1
    assert write_acks[0]["type"] == "ack"


def test_write_rejects_non_string_data():
    """`write` requires `data` to be a JSON string. A number or null
    must produce an error envelope, not silently coerce."""
    events = _run_repl(
        [
            {"id": "1", "type": "start", "cwd": "/tmp"},
            {"id": "bad", "type": "write", "data": 42},
            {"id": "3", "type": "stop"},
        ]
    )
    err = _by_id(events, "bad")
    assert len(err) == 1
    assert err[0]["type"] == "error"
    assert "string" in err[0]["message"].lower()


def test_write_b64_rejects_non_string_data_b64():
    """`write_b64` requires `data_b64` to be a JSON string. A number must
    produce an error envelope, not silently coerce."""
    events = _run_repl(
        [
            {"id": "1", "type": "start", "cwd": "/tmp"},
            {"id": "bad", "type": "write_b64", "data_b64": 42},
            {"id": "3", "type": "stop"},
        ]
    )
    err = _by_id(events, "bad")
    assert len(err) == 1
    assert err[0]["type"] == "error"
    assert err[0]["command_type"] == "write_b64"
    assert "string" in err[0]["message"].lower()


def test_write_b64_rejects_invalid_base64():
    """`write_b64` with an invalid base64 payload must produce an error
    envelope carrying the command_type — not crash or silently drop the data."""
    events = _run_repl(
        [
            {"id": "1", "type": "start", "cwd": "/tmp"},
            {"id": "bad64", "type": "write_b64", "data_b64": "not-valid-base64!!!"},
            {"id": "3", "type": "stop"},
        ]
    )
    err = _by_id(events, "bad64")
    assert len(err) == 1
    assert err[0]["type"] == "error"
    assert err[0]["command_type"] == "write_b64"
