"""Shared Symmetria-toolkit colour scheme, read from an optional JSON file.

The IDE's chrome palette (``qml/design/Theme.qml``) and the File Manager's
(``FmTheme.qml``, reused inside the IDE for the file tree and git surfaces)
used to be independent: the IDE's colours were QML literals, while FmTheme
followed **Symmetria Shell's** ``color-scheme.json``. The two drifted, and
editing the shell's palette re-skinned the desktop as a side effect.

Both now read ONE optional file, owned by neither app:

    $SYMMETRIA_UI_SCHEME                          (explicit override)
    $XDG_CONFIG_HOME/symmetria/ui/color-scheme.json
    ~/.config/symmetria/ui/color-scheme.json      (when XDG_CONFIG_HOME is unset)

The shell's file is deliberately NOT in that chain — the shell took its own
metallic direction that these two apps do not follow.

**The file is optional and usually absent.** Both apps ship a built-in dark
palette that is the real default; this file exists so one edit can re-skin both
at once. A missing, unreadable, or malformed file is not an error — it yields an
empty mapping and every token falls back to its built-in literal.

Format (identical to the shell's, so one can be copied as a starting point):
values are hex WITHOUT the leading ``#``, nested under ``colours``::

    {"colours": {"surface": "0f0f10", "onSurface": "d4d4d8"}}

Key names are Material-3 role names. FmTheme applies only the keys its own
palette already declares; ``Theme.qml`` maps the subset it cares about. When
adding a key, add it on both sides — the two mappings are independent by design
(the IDE's chrome and the FM's panels want different roles), so a key added here
alone changes nothing.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

#: Env var pointing directly at a scheme file. Highest priority; used by
#: worktree runs to preview a palette without touching the user's config.
SCHEME_ENV = "SYMMETRIA_UI_SCHEME"

#: Path under the XDG config dir, shared with FmTheme's `_configDir`.
SCHEME_RELATIVE_PATH = Path("symmetria/ui/color-scheme.json")


def scheme_path() -> Path:
    """Return the scheme file path, whether or not it exists.

    Resolution is env-first, then XDG. The returned path is NOT checked for
    existence — callers that need that should use :func:`load_scheme`, which
    treats a missing file as an empty scheme.
    """
    override = os.environ.get(SCHEME_ENV, "").strip()
    if override:
        return Path(override).expanduser()

    config_home = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return base / SCHEME_RELATIVE_PATH


def load_scheme() -> dict[str, str]:
    """Load the colour scheme as ``{role_name: "#rrggbb"}``.

    Never raises. A missing file, unreadable file, malformed JSON, or a payload
    without a ``colours`` object all yield ``{}`` — the caller then falls back
    to its built-in literals, which is the normal case rather than an error
    path. Entries whose value is not a string are skipped individually so one
    bad key cannot discard a whole otherwise-valid scheme.
    """
    path = scheme_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        log.warning("ui scheme unreadable at %s: %s", path, exc)
        return {}

    try:
        payload = json.loads(raw)
    except ValueError as exc:
        log.warning("ui scheme is not valid JSON at %s: %s", path, exc)
        return {}

    colours = payload.get("colours") if isinstance(payload, dict) else None
    if not isinstance(colours, dict):
        log.warning("ui scheme at %s has no 'colours' object", path)
        return {}

    resolved: dict[str, str] = {}
    for key, value in colours.items():
        if isinstance(value, str) and value:
            # Tolerate both "0f0f10" (the shell's convention) and "#0f0f10".
            resolved[str(key)] = value if value.startswith("#") else f"#{value}"

    log.info("ui scheme loaded from %s (%d colours)", path, len(resolved))
    return resolved
