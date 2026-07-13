"""server_registry — parse/validate the machine-local remote-server registry.

Pure file-parsing tests: no ssh, no Qt beyond the session QCoreApplication.
The contract under test: missing/malformed files degrade to [], bad entries
are skipped without failing the good ones, and Vigilia-shaped defaults fill
every omitted field so a minimal entry is just {"name", "host"}.
"""

from __future__ import annotations

import json

from symmetria_ide.server_registry import (
    RemoteServer,
    config_path,
    load_servers,
)


def _write(tmp_path, payload) -> str:
    path = tmp_path / "servers.json"
    path.write_text(
        payload if isinstance(payload, str) else json.dumps(payload),
        encoding="utf-8",
    )
    return path


def test_missing_file_returns_empty(tmp_path):
    assert load_servers(tmp_path / "nope.json") == []


def test_malformed_json_returns_empty(tmp_path):
    assert load_servers(_write(tmp_path, "{not json")) == []


def test_non_dict_top_level_returns_empty(tmp_path):
    assert load_servers(_write(tmp_path, ["not", "a", "dict"])) == []


def test_servers_not_a_list_returns_empty(tmp_path):
    assert load_servers(_write(tmp_path, {"servers": {"name": "x"}})) == []


def test_minimal_entry_gets_vigilia_defaults(tmp_path):
    path = _write(tmp_path, {"servers": [{"name": "vigilia-vps", "host": "10.0.0.5"}]})
    servers = load_servers(path)
    assert len(servers) == 1
    server = servers[0]
    assert server == RemoteServer(name="vigilia-vps", host="10.0.0.5")
    assert server.user == "dev"
    assert server.identity_file == "~/.ssh/id_ed25519"
    assert server.repos_dir == "/opt/dev/repos"
    assert server.tmux_socket == "/home/dev/.vigilia/tmux.sock"
    assert server.hub_socket == "/home/dev/.vigilia/agent-hub.sock"
    assert server.ssh_destination == "dev@10.0.0.5"


def test_explicit_fields_override_defaults(tmp_path):
    path = _write(
        tmp_path,
        {
            "servers": [
                {
                    "name": "homebox",
                    "host": "box.tail.ts.net",
                    "user": "jc",
                    "identity_file": "~/.ssh/homebox",
                    "repos_dir": "/srv/repos",
                    "tmux_socket": "/run/tmux.sock",
                    "hub_socket": "/run/hub.sock",
                }
            ]
        },
    )
    (server,) = load_servers(path)
    assert server.user == "jc"
    assert server.identity_file == "~/.ssh/homebox"
    assert server.repos_dir == "/srv/repos"
    assert server.tmux_socket == "/run/tmux.sock"
    assert server.hub_socket == "/run/hub.sock"
    assert server.ssh_destination == "jc@box.tail.ts.net"


def test_bad_entries_skipped_good_ones_kept(tmp_path):
    path = _write(
        tmp_path,
        {
            "servers": [
                "not-an-object",
                {"name": "", "host": "1.2.3.4"},  # missing name
                {"name": "no-host"},  # missing host
                {"name": "good", "host": "5.6.7.8"},
            ]
        },
    )
    servers = load_servers(path)
    assert [s.name for s in servers] == ["good"]


def test_identity_file_stored_verbatim_not_expanded(tmp_path):
    """Expansion is ssh_runner's job at use time — the dataclass mirrors
    the config file so a round-trip inspection shows what the user wrote."""
    path = _write(
        tmp_path,
        {"servers": [{"name": "x", "host": "h", "identity_file": "~/.ssh/k"}]},
    )
    (server,) = load_servers(path)
    assert server.identity_file == "~/.ssh/k"


def test_config_path_honours_xdg_config_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config_path() == tmp_path / "symmetria-ide" / "servers.json"


def test_default_path_used_when_no_explicit_path(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    target = tmp_path / "symmetria-ide"
    target.mkdir(parents=True)
    (target / "servers.json").write_text(
        json.dumps({"servers": [{"name": "implicit", "host": "9.9.9.9"}]}),
        encoding="utf-8",
    )
    assert [s.name for s in load_servers()] == ["implicit"]
