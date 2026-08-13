"""Shared resolution of per-project on-disk state paths.

The IDE keeps several kinds of per-project state under ``$XDG_STATE_HOME``
(the file-tree expansion cache in ``tree_state_cache``, the saved-session
manifest in ``session_store``, ...), each in its own subfolder but all keyed
by the same digest of the project root's absolute path. This module is the
single home for that path math so the subfolder layout and the hashing scheme
cannot drift between callers.

Extracted from ``tree_state_cache`` (its original home) once ``session_store``
needed the identical scheme — two callsites of the same logic, the same
precedent as ``fs_atomic`` being extracted for ``project_browser_marker``.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


def state_dir(subfolder: str) -> Path:
    """Resolve (and create) the ``symmetria-ide/<subfolder>`` state directory.

    Honours ``$XDG_STATE_HOME``; falls back to ``~/.local/state`` per the XDG
    base directory spec. The directory is created on demand — callers don't
    need to mkdir.
    """
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state"
    )
    out = Path(base) / "symmetria-ide" / subfolder
    out.mkdir(parents=True, exist_ok=True)
    return out


def config_home() -> Path:
    """Resolve the XDG config root (``$XDG_CONFIG_HOME``, else ``~/.config``).

    Unlike :func:`state_dir` this creates nothing and appends no subfolder —
    callers own their own layout under it (``server_registry`` puts its file in
    ``symmetria-ide/``, ``ui_scheme`` in ``symmetria/ui/``).

    A RELATIVE ``$XDG_CONFIG_HOME`` is ignored, per the XDG base directory
    spec, and the ``~/.config`` fallback applies instead. That is not
    pedantry here: the IDE runs many concurrent windows with different working
    directories, so honouring a relative value would silently resolve config
    to a different file per window. ``app.py``'s ``_scratch_dir`` applies the
    same rule for the runtime dir.
    """
    base = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if base:
        candidate = Path(base).expanduser()
        if candidate.is_absolute():
            return candidate
    return Path.home() / ".config"


def repo_hash(repo_root: str) -> str:
    """Stable filename digest for a project root path.

    NOT a security primitive — collision-resistant enough that the per-user
    state dir won't see filename clashes for any realistic number of projects.
    A truncated sha256 of the absolute path; stable per-path across runs.
    """
    return hashlib.sha256(repo_root.encode("utf-8")).hexdigest()[:16]
