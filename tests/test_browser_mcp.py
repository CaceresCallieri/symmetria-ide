"""Tests for the browser MCP bridge + server lifecycle (Phase 4 Stage 2b).

The bridge is the mcp-INDEPENDENT marshaling seam (it just needs Qt), so
its GUI-thread slots are unit-tested here by calling them directly — the
queued-signal delivery itself is Qt machinery. The FastMCP/uvicorn server
is integration-verified out-of-band (a standalone harness drives a real
MCP client against a real window); only its env-gated no-op path and
config-file shape are pinned here, since those don't need the network
stack.
"""

from __future__ import annotations

import concurrent.futures
import json

import pytest

from symmetria_ide.browser_automation import BrowserAutomation
from symmetria_ide.browser_mcp import SERVER_NAME, BrowserMcpBridge, BrowserMcpServer


@pytest.fixture
def auto():
    return BrowserAutomation()


# ---------------------------------------------------------------------------
# Bridge — op marshaling + window→slot resolution + future resolution.
# ---------------------------------------------------------------------------


def test_run_op_resolves_window_and_completes_future(auto):
    """_run_op maps the window arg → internal slot via the resolver, kicks
    the automation, and resolves the future when QML delivers the result."""
    emitted = []
    auto.automationRequested.connect(
        lambda rid, slot, op, payload: emitted.append((rid, slot, op, payload))
    )
    # window 0 → focused slot 7; any other window passes through.
    bridge = BrowserMcpBridge(auto, slot_resolver=lambda w: 7 if w == 0 else w)
    fut: concurrent.futures.Future = concurrent.futures.Future()

    bridge._run_op(0, "eval_js", json.dumps({"code": "1+1"}), fut)

    assert emitted, "automation should have been asked"
    rid, slot, op, _payload = emitted[0]
    assert slot == 7  # window 0 resolved to focused slot 7
    assert op == "eval_js"
    assert not fut.done()  # awaiting QML

    # Simulate QML delivering the runJavaScript result.
    auto.on_result(rid, json.dumps({"ok": True, "value": 2}))
    assert fut.done()
    assert fut.result() == {"ok": True, "value": 2}


def test_run_op_no_window_resolves_error_immediately(auto):
    bridge = BrowserMcpBridge(auto, slot_resolver=lambda w: 0)  # never a window
    fut: concurrent.futures.Future = concurrent.futures.Future()
    bridge._run_op(3, "eval_js", "{}", fut)
    assert fut.result() == {"ok": False, "error": "no-window"}


def test_run_read_resolves_with_fn_result(auto):
    bridge = BrowserMcpBridge(auto, slot_resolver=lambda w: w)
    fut: concurrent.futures.Future = concurrent.futures.Future()
    bridge._run_read(lambda: {"ok": True, "windows": [{"window": 1}]}, fut)
    assert fut.result() == {"ok": True, "windows": [{"window": 1}]}


def test_run_read_swallows_exception(auto):
    """A read fn that raises must still resolve the future (never hang the
    awaiting tool) with an error dict."""
    bridge = BrowserMcpBridge(auto, slot_resolver=lambda w: w)
    fut: concurrent.futures.Future = concurrent.futures.Future()

    def boom():
        raise RuntimeError("kaboom")

    bridge._run_read(boom, fut)
    result = fut.result()
    assert result["ok"] is False
    assert "kaboom" in result["error"]


# ---------------------------------------------------------------------------
# Server — env-gated disable + config-file shape (no network stack needed).
# ---------------------------------------------------------------------------


def _server(auto):
    return BrowserMcpServer(
        BrowserMcpBridge(auto, slot_resolver=lambda w: w),
        windows_reader=lambda: {"ok": True, "windows": []},
    )


def test_server_disabled_via_env_is_noop(auto, monkeypatch):
    monkeypatch.setenv("SYMMETRIA_IDE_BROWSER_MCP", "0")
    server = _server(auto)
    server.start()
    assert server.port == 0
    assert server.url == ""
    assert server.config_path == ""


def test_config_file_shape(auto, tmp_path, monkeypatch):
    """The written MCP config is claude-shaped (mcpServers/<name>/type:http)
    so `--mcp-config` loads it. Drive _write_config directly with a known
    port to avoid binding a socket."""
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    server = _server(auto)
    server._port = 12345  # pretend the server bound this port
    server._write_config()
    assert server.config_path
    with open(server.config_path) as handle:
        config = json.load(handle)
    entry = config["mcpServers"][SERVER_NAME]
    assert entry["type"] == "http"
    assert entry["url"] == "http://127.0.0.1:12345/mcp"
