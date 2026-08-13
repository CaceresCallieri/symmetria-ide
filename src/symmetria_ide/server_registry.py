"""Remote-server registry for the Local ↔ VPS location toggle.

Machine-local, hand-edited config describing the remote development servers
this machine can pair projects with (the Vigilia-provisioned VPS being the
canonical entry). Lives at ``$XDG_CONFIG_HOME/symmetria-ide/servers.json`` —
deliberately NOT the committed ``.symmetria/ide.json`` marker (hosts and key
paths are machine facts, not project facts) and NOT ``state_paths``
(``$XDG_STATE_HOME`` is derived state; this file is user-authored config).

File shape (all fields except ``name`` and ``host`` optional)::

    {
      "version": 1,
      "servers": [
        {
          "name": "vigilia-vps",
          "host": "64.176.22.138",
          "user": "dev",
          "identity_file": "~/.ssh/id_ed25519",
          "repos_dir": "/opt/dev/repos",
          "tmux_socket": "/home/dev/.vigilia/tmux.sock",
          "hub_socket": "/home/dev/.vigilia/agent-hub.sock"
        }
      ]
    }

The defaults mirror Vigilia's server layout (see
``~/projects/vigilia/docs/phase2-headless-hub-plan.md`` "Live infrastructure")
so a minimal entry is just ``{"name": ..., "host": ...}``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from .state_paths import config_home

log = logging.getLogger(__name__)

_CONFIG_RELPATH = Path("symmetria-ide") / "servers.json"


@dataclass(slots=True, frozen=True)
class RemoteServer:
    """One remote development server (immutable; safe to share across threads).

    ``identity_file`` is stored verbatim (may contain ``~``) and expanded at
    use time by ``ssh_runner`` — keeping the stored form verbatim makes the
    dataclass a faithful mirror of the user's config file.
    """

    name: str
    host: str
    user: str = "dev"
    identity_file: str = "~/.ssh/id_ed25519"
    repos_dir: str = "/opt/dev/repos"
    tmux_socket: str = "/home/dev/.vigilia/tmux.sock"
    hub_socket: str = "/home/dev/.vigilia/agent-hub.sock"

    @property
    def ssh_destination(self) -> str:
        return f"{self.user}@{self.host}"


def config_path() -> Path:
    """Resolve the registry file path (XDG config, ``~/.config`` fallback).

    The XDG math lives in ``state_paths.config_home`` — shared with
    ``ui_scheme``, which needs the identical resolution, and which is also
    where the "ignore a relative ``$XDG_CONFIG_HOME``" rule is documented.
    """
    return config_home() / _CONFIG_RELPATH


def _server_from_entry(entry: object) -> RemoteServer | None:
    """Validate one raw JSON entry into a RemoteServer (None = skip + warn)."""
    if not isinstance(entry, dict):
        log.warning("server_registry: skipping non-object entry %r", entry)
        return None
    name = str(entry.get("name", "")).strip()
    host = str(entry.get("host", "")).strip()
    if not name or not host:
        log.warning("server_registry: skipping entry missing name/host: %r", entry)
        return None
    defaults = RemoteServer(name=name, host=host)
    return RemoteServer(
        name=name,
        host=host,
        user=str(entry.get("user", defaults.user)) or defaults.user,
        identity_file=str(entry.get("identity_file", defaults.identity_file))
        or defaults.identity_file,
        repos_dir=str(entry.get("repos_dir", defaults.repos_dir)) or defaults.repos_dir,
        tmux_socket=str(entry.get("tmux_socket", defaults.tmux_socket))
        or defaults.tmux_socket,
        hub_socket=str(entry.get("hub_socket", defaults.hub_socket))
        or defaults.hub_socket,
    )


def load_servers(path: Path | None = None) -> list[RemoteServer]:
    """Parse the registry file; missing/malformed degrades to ``[]``.

    Per-entry validation warns and skips rather than failing the whole file —
    one typo'd entry must not take down the good ones.
    """
    resolved = path if path is not None else config_path()
    try:
        raw = resolved.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError:
        log.exception("server_registry: cannot read %s", resolved)
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        log.exception("server_registry: malformed JSON in %s", resolved)
        return []
    entries = data.get("servers", []) if isinstance(data, dict) else []
    if not isinstance(entries, list):
        log.warning("server_registry: 'servers' is not a list in %s", resolved)
        return []
    servers = []
    for entry in entries:
        server = _server_from_entry(entry)
        if server is not None:
            servers.append(server)
    return servers
