"""Tests for the browser automation bridge (Phase 4 Stage 2a).

Pure-Python. The QML delivery side (running `runJavaScript` on the real
WebEngineView) is exercised live, but the Python bridge — request-id
allocation, the pending-callback map, signal emission, and result
resolution — is unit-tested here by simulating QML's `on_result` call.
"""

from __future__ import annotations

import json

import pytest

from symmetria_ide.browser_automation import OPS, BrowserAutomation


@pytest.fixture
def auto():
    return BrowserAutomation()


def _capture_signal(automation):
    """Capture (request_id, slot, op, payload) emissions."""
    emitted = []
    automation.automationRequested.connect(
        lambda rid, slot, op, payload: emitted.append((rid, slot, op, payload))
    )
    return emitted


def test_request_emits_signal_and_stores_callback(auto):
    emitted = _capture_signal(auto)
    got = []
    rid = auto.request(1, "eval_js", {"code": "1+1"}, got.append)
    assert rid != ""
    assert auto.pending_count() == 1
    assert len(emitted) == 1
    e_rid, e_slot, e_op, e_payload = emitted[0]
    assert e_rid == rid
    assert e_slot == 1
    assert e_op == "eval_js"
    assert json.loads(e_payload) == {"code": "1+1"}
    # Callback not invoked yet — it awaits QML's result.
    assert got == []


def test_on_result_resolves_and_pops(auto):
    got = []
    rid = auto.request(1, "eval_js", {"code": "2"}, got.append)
    auto.on_result(rid, json.dumps({"ok": True, "value": 2}))
    assert got == [{"ok": True, "value": 2}]
    assert auto.pending_count() == 0


def test_request_ids_are_unique(auto):
    rid1 = auto.request(1, "snapshot", {}, lambda r: None)
    rid2 = auto.request(1, "snapshot", {}, lambda r: None)
    assert rid1 != rid2
    assert auto.pending_count() == 2


def test_unknown_op_rejected_without_emit(auto):
    emitted = _capture_signal(auto)
    got = []
    rid = auto.request(1, "teleport", {}, got.append)
    assert rid == ""
    assert emitted == []  # no QML round-trip for a bad op
    assert got and got[0]["ok"] is False
    assert "unknown op" in got[0]["error"]
    assert auto.pending_count() == 0


def test_on_result_unknown_id_is_safe_noop(auto):
    # Must not raise — it runs on the GUI thread.
    auto.on_result("nope_0", json.dumps({"ok": True}))
    assert auto.pending_count() == 0


def test_on_result_bad_json_yields_error_dict(auto):
    got = []
    rid = auto.request(1, "eval_js", {"code": "x"}, got.append)
    auto.on_result(rid, "not json{")
    assert got and got[0]["ok"] is False
    assert got[0]["error"] == "bad-result-json"


def test_on_result_non_object_json_yields_error(auto):
    got = []
    rid = auto.request(1, "eval_js", {"code": "x"}, got.append)
    auto.on_result(rid, "[1,2,3]")  # valid JSON, but not an object
    assert got and got[0]["ok"] is False
    assert got[0]["error"] == "result-not-object"


@pytest.mark.parametrize(
    "method,args,exp_op,exp_payload",
    [
        (
            "navigate",
            (1, "https://example.com"),
            "navigate",
            {"url": "https://example.com"},
        ),
        ("eval_js", (1, "document.title"), "eval_js", {"code": "document.title"}),
        ("snapshot", (1,), "snapshot", {}),
        ("click", (1, "r3"), "click", {"ref": "r3"}),
        ("fill", (1, "r3", "hello"), "fill", {"ref": "r3", "text": "hello"}),
    ],
)
def test_convenience_wrappers(auto, method, args, exp_op, exp_payload):
    emitted = _capture_signal(auto)
    getattr(auto, method)(*args, lambda r: None)
    assert len(emitted) == 1
    _, _slot, op, payload = emitted[0]
    assert op == exp_op
    assert json.loads(payload) == exp_payload


def test_ops_constant_matches_wrappers():
    # Guard: every convenience wrapper's op is in OPS (so request() accepts it).
    assert set(OPS) == {"navigate", "eval_js", "snapshot", "click", "fill"}
