"""Tests for the browser MCP bridge + server lifecycle (Phase 4 Stage 2b/4).

The bridge is the mcp-INDEPENDENT marshaling seam (it just needs Qt), so
its GUI-thread slot is unit-tested here by calling it directly — the
queued-signal delivery itself is Qt machinery. The FastMCP/uvicorn server
is integration-verified out-of-band (a standalone harness drives a real
MCP client against a real window); only its env-gated no-op path and
config-file shape are pinned here, since those don't need the network
stack.

Stage 4 retired the page-driving op-path (navigate/eval/snapshot/click/fill
+ BrowserAutomation): driving is delegated to chrome-devtools-mcp over the
embedded CDP endpoint. Our server now exposes only window management
(browser_open / browser_list_windows), both GUI-thread reads.
"""

from __future__ import annotations

import concurrent.futures
import json

from symmetria_ide.browser_mcp import SERVER_NAME, BrowserMcpBridge, BrowserMcpServer


# ---------------------------------------------------------------------------
# Bridge — read marshaling + future resolution.
# ---------------------------------------------------------------------------


def test_run_read_resolves_with_fn_result():
    bridge = BrowserMcpBridge()
    fut: concurrent.futures.Future = concurrent.futures.Future()
    bridge._run_read(lambda: {"ok": True, "windows": [{"window": 1}]}, fut)
    assert fut.result() == {"ok": True, "windows": [{"window": 1}]}


def test_run_read_swallows_exception():
    """A read fn that raises must still resolve the future (never hang the
    awaiting tool) with an error dict."""
    bridge = BrowserMcpBridge()
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


def _server():
    return BrowserMcpServer(
        BrowserMcpBridge(),
        windows_reader=lambda: {"ok": True, "windows": []},
        # opener takes (url, agent_id) since Stage 3 — the controller records
        # window ownership for the calling agent.
        window_opener=lambda url, agent_id="": {"ok": True, "window": 1},
        # attention_setter takes (agent_id, message) — lights the chip globe's
        # attention dot for the calling agent (browser_request_attention).
        attention_setter=lambda agent_id, message="": {"ok": True},
        # wait_registrar takes (display_num, registrant_id, note) — the
        # wait_for_agent coordination tool body (agent coordination v1).
        wait_registrar=lambda agent, registrant_id, note="": {"ok": True},
        # browser_gate: per-project opt-in read at CALL time for the three
        # browser tools (the config entry is always injected now).
        browser_gate=lambda: True,
    )


def test_server_disabled_via_env_is_noop(monkeypatch):
    monkeypatch.setenv("SYMMETRIA_IDE_BROWSER_MCP", "0")
    server = _server()
    server.start()
    assert server.port == 0
    assert server.url == ""
    assert server.config_path == ""


def test_config_file_shape(tmp_path, monkeypatch):
    """The written MCP config is claude-shaped (mcpServers/<name>/type:http)
    so `--mcp-config` loads it. Drive _write_config directly with a known
    port to avoid binding a socket."""
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    server = _server()
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


def test_agent_config_path_writes_identity_header(tmp_path, monkeypatch):
    """A per-agent config points at the server URL AND stamps the
    X-Symmetria-Agent header — the basis for attributing tool calls back to
    the spawning agent. Filename is keyed by the agent id (<pid>_<slot>)."""
    from symmetria_ide.browser_mcp import _AGENT_HEADER

    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    server = _server()
    server._port = 23456  # pretend bound
    path = server.agent_config_path("1234_2", browser_enabled=True)
    assert path.endswith("1234_2.json")
    with open(path) as handle:
        config = json.load(handle)
    entry = config["mcpServers"][SERVER_NAME]
    assert entry["type"] == "http"
    assert entry["url"] == "http://127.0.0.1:23456/mcp"
    assert entry["headers"][_AGENT_HEADER] == "1234_2"


def test_agent_config_path_injects_chrome_devtools_when_cdp_enabled(
    tmp_path, monkeypatch
):
    """When CDP is enabled (QTWEBENGINE_REMOTE_DEBUGGING set in run()), the
    per-agent config ALSO carries an off-the-shelf chrome-devtools-mcp stdio
    entry pointed at the embedded view's CDP port — the agent's full
    browser-automation surface alongside our two window tools."""
    from symmetria_ide.browser_mcp import _CHROME_DEVTOOLS_MCP_VERSION

    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    monkeypatch.setenv("QTWEBENGINE_REMOTE_DEBUGGING", "9911")
    # Force npx "present" so the test is deterministic regardless of the host.
    monkeypatch.setattr(
        "symmetria_ide.browser_mcp.shutil.which", lambda _: "/usr/bin/npx"
    )
    server = _server()
    server._port = 23456
    with open(server.agent_config_path("1234_2", browser_enabled=True)) as handle:
        config = json.load(handle)
    cdt = config["mcpServers"]["chrome-devtools"]
    assert cdt["command"] == "npx"
    assert f"chrome-devtools-mcp@{_CHROME_DEVTOOLS_MCP_VERSION}" in cdt["args"]
    assert "http://127.0.0.1:9911" in cdt["args"]
    # Our own server entry is still present (window allocation + attribution).
    assert SERVER_NAME in config["mcpServers"]


def test_agent_config_path_omits_chrome_devtools_without_cdp(tmp_path, monkeypatch):
    """No CDP port (remote debugging off) → no chrome-devtools entry; the
    agent still gets our window tools."""
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    monkeypatch.delenv("QTWEBENGINE_REMOTE_DEBUGGING", raising=False)
    server = _server()
    server._port = 23456
    with open(server.agent_config_path("1234_2", browser_enabled=True)) as handle:
        config = json.load(handle)
    assert "chrome-devtools" not in config["mcpServers"]
    assert SERVER_NAME in config["mcpServers"]


def test_agent_config_path_omits_chrome_devtools_without_npx(tmp_path, monkeypatch):
    """CDP is live but node/npx is missing → omit the chrome-devtools entry so
    the agent cleanly falls back to the two window tools (a broken stdio server
    would otherwise just fail at the agent's MCP client)."""
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    monkeypatch.setenv("QTWEBENGINE_REMOTE_DEBUGGING", "9911")
    monkeypatch.setattr("symmetria_ide.browser_mcp.shutil.which", lambda _: None)
    server = _server()
    server._port = 23456
    with open(server.agent_config_path("1234_2", browser_enabled=True)) as handle:
        config = json.load(handle)
    assert "chrome-devtools" not in config["mcpServers"]
    assert SERVER_NAME in config["mcpServers"]  # window tools still present


def test_agent_config_path_empty_without_port_or_id(tmp_path, monkeypatch):
    """No port (server down) or empty id → "" — spawn_argv treats that as no
    --mcp-config, so the agent simply gets no browser tools. (browser_enabled
    is True here so the per-project gate isn't what's returning "".)"""
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    server = _server()
    assert server.agent_config_path("1234_2", browser_enabled=True) == ""  # _port 0
    server._port = 23456
    assert server.agent_config_path("", browser_enabled=True) == ""  # no agent id


def test_agent_config_path_gated_off_keeps_server_drops_chrome_devtools(
    tmp_path, monkeypatch
):
    """The per-project gate now bites only chrome-devtools: browser_enabled=
    False (the default) still writes a config carrying OUR server entry (the
    wait_for_agent coordination tool must Just Work in every project — the
    browser tools are call-time gated server-side instead), but omits the
    chrome-devtools entry, so no per-agent Node process spawns."""
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    monkeypatch.setenv("QTWEBENGINE_REMOTE_DEBUGGING", "9911")
    monkeypatch.setattr(
        "symmetria_ide.browser_mcp.shutil.which", lambda _: "/usr/bin/npx"
    )
    server = _server()
    server._port = 23456
    # Explicit False and the default behave identically.
    for path in (
        server.agent_config_path("1234_2", browser_enabled=False),
        server.agent_config_path("1234_2"),
    ):
        with open(path) as handle:
            config = json.load(handle)
        assert SERVER_NAME in config["mcpServers"]
        assert "chrome-devtools" not in config["mcpServers"]


def test_stop_unlinks_per_agent_configs(tmp_path, monkeypatch):
    """stop() removes every per-agent config it wrote (graceful cleanup; a
    hard-kill's orphans are swept by reap_orphan_configs instead)."""
    import os

    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    server = _server()
    server._port = 23456
    p1 = server.agent_config_path("1234_2", browser_enabled=True)
    p2 = server.agent_config_path("1234_3", browser_enabled=True)
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


# ---------------------------------------------------------------------------
# agent_config_content — opencode inline OPENCODE_CONFIG_CONTENT (SSE + schema).
# No file is written (inline env var), so these assert the returned JSON string.
# ---------------------------------------------------------------------------


def test_agent_config_content_opencode_schema(monkeypatch):
    """opencode's inline config uses the `mcp` key with symmetria-browser as a
    REMOTE server on the /sse URL (NOT streamable-HTTP /mcp) carrying the
    identity header — returned as a JSON string for OPENCODE_CONFIG_CONTENT."""
    from symmetria_ide.browser_mcp import _AGENT_HEADER

    monkeypatch.delenv("QTWEBENGINE_REMOTE_DEBUGGING", raising=False)
    server = _server()
    server._port = 34567  # pretend bound
    config = json.loads(server.agent_config_content("1234_2", browser_enabled=True))
    entry = config["mcp"][SERVER_NAME]
    assert entry["type"] == "remote"
    assert entry["url"] == "http://127.0.0.1:34567/sse"  # SSE, not /mcp
    assert entry["enabled"] is True
    assert entry["headers"][_AGENT_HEADER] == "1234_2"


def test_agent_config_content_injects_chrome_devtools_local(monkeypatch):
    """With CDP + npx, chrome-devtools is added as a LOCAL (stdio) server whose
    `command` is the FULL argv array (opencode's shape, vs claude's
    command+args split)."""
    from symmetria_ide.browser_mcp import _CHROME_DEVTOOLS_MCP_VERSION

    monkeypatch.setenv("QTWEBENGINE_REMOTE_DEBUGGING", "9911")
    monkeypatch.setattr(
        "symmetria_ide.browser_mcp.shutil.which", lambda _: "/usr/bin/npx"
    )
    server = _server()
    server._port = 34567
    config = json.loads(server.agent_config_content("1234_2", browser_enabled=True))
    cdt = config["mcp"]["chrome-devtools"]
    assert cdt["type"] == "local"
    assert cdt["command"][0] == "npx"
    assert f"chrome-devtools-mcp@{_CHROME_DEVTOOLS_MCP_VERSION}" in cdt["command"]
    assert "http://127.0.0.1:9911" in cdt["command"]
    assert cdt["enabled"] is True
    assert SERVER_NAME in config["mcp"]  # window/attention tools still present


def test_agent_config_content_omits_chrome_devtools_without_cdp(monkeypatch):
    """No CDP port → no chrome-devtools entry; the agent still gets our window
    tools (matches agent_config_path's behaviour)."""
    monkeypatch.delenv("QTWEBENGINE_REMOTE_DEBUGGING", raising=False)
    server = _server()
    server._port = 34567
    config = json.loads(server.agent_config_content("1234_2", browser_enabled=True))
    assert "chrome-devtools" not in config["mcp"]
    assert SERVER_NAME in config["mcp"]


def test_agent_config_content_gated_off_keeps_server_drops_chrome_devtools(
    monkeypatch,
):
    """Same relaxed gate as agent_config_path: not opted in → the opencode
    inline config still carries OUR server entry (coordination) but omits
    chrome-devtools, even with CDP live."""
    monkeypatch.setenv("QTWEBENGINE_REMOTE_DEBUGGING", "9911")
    monkeypatch.setattr(
        "symmetria_ide.browser_mcp.shutil.which", lambda _: "/usr/bin/npx"
    )
    server = _server()
    server._port = 34567
    for content in (
        server.agent_config_content("1234_2", browser_enabled=False),
        server.agent_config_content("1234_2"),  # default is False
    ):
        config = json.loads(content)
        assert SERVER_NAME in config["mcp"]
        assert "chrome-devtools" not in config["mcp"]


def test_browser_gated_body_blocks_and_passes():
    """The call-time browser-tool gate: a False gate returns the disabled
    error (without invoking the body); True / None passes through."""
    from symmetria_ide.browser_mcp import _browser_gated_body

    calls = []

    def body():
        calls.append(1)
        return {"ok": True}

    blocked = _browser_gated_body(lambda: False, body)
    assert blocked["ok"] is False
    assert "disabled" in blocked["error"]
    assert calls == []  # body never ran
    assert _browser_gated_body(lambda: True, body) == {"ok": True}
    assert _browser_gated_body(None, body) == {"ok": True}


def test_agent_config_content_empty_without_port_or_id():
    """No port (server down) or empty id → "" — the same guards as
    agent_config_path, so a spawn during the not-yet-ready window gets no env."""
    server = _server()
    assert server.agent_config_content("1234_2", browser_enabled=True) == ""  # _port 0
    server._port = 34567
    assert server.agent_config_content("", browser_enabled=True) == ""  # no id


# ---------------------------------------------------------------------------
# _build_combined_asgi_app — dual-transport server build (the novel runtime path).
# ---------------------------------------------------------------------------


def test_build_combined_asgi_app_serves_both_transports():
    """The combined app exposes claude's /mcp (streamable-HTTP) AND opencode's
    /sse + /messages/ (SSE) on one Starlette app — a regression guard for the
    dual-transport build. Also covers the sse_app()-removal risk: a future mcp
    bump that drops sse_app() (or moves a mount path into a collision) fails
    HERE in CI rather than at an opencode spawn. No uvicorn boot needed — pure
    route introspection."""
    from mcp.server.fastmcp import FastMCP

    from symmetria_ide.browser_mcp import _build_combined_asgi_app

    app = _build_combined_asgi_app(FastMCP("test-probe"))
    paths = {getattr(route, "path", None) for route in app.routes}
    assert "/mcp" in paths  # claude streamable-HTTP
    assert "/sse" in paths  # opencode SSE stream
    assert any("messages" in (p or "") for p in paths)  # SSE message POST mount
