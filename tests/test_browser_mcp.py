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


# ---------------------------------------------------------------------------
# Bridge attribution (Stage 3) — which agent is driving which window.
# ---------------------------------------------------------------------------


def test_run_op_fires_attribution_start_then_end(auto):
    """An attributed op fires on_attribution("start") before the request and
    ("end") when the result lands — balanced because on_result fires once."""
    calls = []
    emitted = []
    auto.automationRequested.connect(lambda rid, *a: emitted.append(rid))
    bridge = BrowserMcpBridge(
        auto,
        slot_resolver=lambda w: 4,
        on_attribution=lambda aid, slot, phase: calls.append((aid, slot, phase)),
    )
    fut: concurrent.futures.Future = concurrent.futures.Future()

    bridge._run_op(0, "eval_js", "{}", fut, "1234_2")
    assert calls == [("1234_2", 4, "start")]  # start before the result
    assert not fut.done()

    auto.on_result(emitted[0], json.dumps({"ok": True}))
    assert fut.result() == {"ok": True}
    assert calls == [("1234_2", 4, "start"), ("1234_2", 4, "end")]  # balanced


def test_run_op_skips_attribution_for_empty_agent_id(auto):
    """An untagged caller (no X-Symmetria-Agent header → "") drives the window
    but records no attribution — neither start nor end."""
    calls = []
    emitted = []
    auto.automationRequested.connect(lambda rid, *a: emitted.append(rid))
    bridge = BrowserMcpBridge(
        auto, slot_resolver=lambda w: 4, on_attribution=lambda *a: calls.append(a)
    )
    fut: concurrent.futures.Future = concurrent.futures.Future()

    bridge._run_op(0, "eval_js", "{}", fut, "")
    auto.on_result(emitted[0], json.dumps({"ok": True}))
    assert calls == []


def test_run_op_no_window_skips_attribution(auto):
    """A no-window resolution never reaches a slot, so it must not attribute
    (start/end would be unbalanced — no result callback runs)."""
    calls = []
    bridge = BrowserMcpBridge(
        auto, slot_resolver=lambda w: 0, on_attribution=lambda *a: calls.append(a)
    )
    fut: concurrent.futures.Future = concurrent.futures.Future()
    bridge._run_op(2, "eval_js", "{}", fut, "1234_2")
    assert fut.result() == {"ok": False, "error": "no-window"}
    assert calls == []


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
        # opener takes (url, agent_id) since Stage 3 — the controller records
        # window ownership for the calling agent.
        window_opener=lambda url, agent_id="": {"ok": True, "window": 1},
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


def test_reap_orphan_configs(tmp_path, monkeypatch):
    """Configs from dead pids are removed; live-pid and non-pid files survive
    (so a hard-killed IDE doesn't leave discovery-poisoning leftovers)."""
    from symmetria_ide.browser_mcp import _CONFIG_PREFIX, reap_orphan_configs

    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    dead = tmp_path / f"{_CONFIG_PREFIX}9999999.json"  # ~certainly not a live pid
    dead.write_text("{}")
    live = tmp_path / f"{_CONFIG_PREFIX}1.json"  # pid 1 (init) is always alive
    live.write_text("{}")
    junk = tmp_path / f"{_CONFIG_PREFIX}notapid.json"  # non-numeric → left alone
    junk.write_text("{}")

    reap_orphan_configs()

    assert not dead.exists()
    assert live.exists()
    assert junk.exists()


def test_agent_config_path_writes_identity_header(auto, tmp_path, monkeypatch):
    """A per-agent config points at the server URL AND stamps the
    X-Symmetria-Agent header — the basis for attributing tool calls back to
    the spawning agent. Filename is keyed by the agent id (<pid>_<slot>)."""
    from symmetria_ide.browser_mcp import _AGENT_HEADER

    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    server = _server(auto)
    server._port = 23456  # pretend bound
    path = server.agent_config_path("1234_2")
    assert path.endswith("1234_2.json")
    with open(path) as handle:
        config = json.load(handle)
    entry = config["mcpServers"][SERVER_NAME]
    assert entry["type"] == "http"
    assert entry["url"] == "http://127.0.0.1:23456/mcp"
    assert entry["headers"][_AGENT_HEADER] == "1234_2"


def test_agent_config_path_empty_without_port_or_id(auto, tmp_path, monkeypatch):
    """No port (server down) or empty id → "" — spawn_argv treats that as no
    --mcp-config, so the agent simply gets no browser tools."""
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    server = _server(auto)
    assert server.agent_config_path("1234_2") == ""  # _port still 0
    server._port = 23456
    assert server.agent_config_path("") == ""  # no agent id


def test_stop_unlinks_per_agent_configs(auto, tmp_path, monkeypatch):
    """stop() removes every per-agent config it wrote (graceful cleanup; a
    hard-kill's orphans are swept by reap_orphan_configs instead)."""
    import os

    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    server = _server(auto)
    server._port = 23456
    p1 = server.agent_config_path("1234_2")
    p2 = server.agent_config_path("1234_3")
    assert os.path.exists(p1) and os.path.exists(p2)
    server.stop()  # never started, but must still unlink the configs
    assert not os.path.exists(p1)
    assert not os.path.exists(p2)


def test_reap_orphan_configs_handles_per_agent_filenames(tmp_path, monkeypatch):
    """The leading-pid parse reaps per-agent (<pid>_<slot>) configs from dead
    IDEs while sparing a live pid's — so attribution configs don't leak."""
    from symmetria_ide.browser_mcp import _CONFIG_PREFIX, reap_orphan_configs

    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    dead = tmp_path / f"{_CONFIG_PREFIX}9999999_2.json"  # dead pid, per-agent name
    dead.write_text("{}")
    live = tmp_path / f"{_CONFIG_PREFIX}1_3.json"  # pid 1 (init) always alive
    live.write_text("{}")

    reap_orphan_configs()

    assert not dead.exists()
    assert live.exists()


def test_perf_js_payload_shape():
    """The browser_perf payload reads the reliable Performance APIs and keeps
    the process-relative-paint guard (validated live against a real page)."""
    from symmetria_ide.browser_mcp import _PERF_JS

    assert "getEntriesByType('navigation')" in _PERF_JS
    assert "getEntriesByType('resource')" in _PERF_JS
    for key in ("timing", "webVitals", "transferKB", "resourceCount", "jsHeapMB"):
        assert key in _PERF_JS
    # The plaus() guard drops QtWebEngine's leaked process-relative paint
    # timestamp — must survive refactors (see the 512660ms artifact).
    assert "plaus" in _PERF_JS
