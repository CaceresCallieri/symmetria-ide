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

⚠ **The two sides reload differently.** The FM watches the file through a
``FileWatcher`` and re-applies it live; the IDE takes a load-once snapshot at
startup. So editing the scheme while an IDE window is open repaints the
FM-provided surfaces (file tree, git badges, Active Changes rows) inside that
window while the IDE's own chrome keeps the old palette until restart — a
visibly split palette in the meantime. Making the IDE side live is NOT a matter
of adding a ``QFileSystemWatcher``: ``Theme.qml`` reads the map through a
function call, which QML does not re-evaluate (CLAUDE.md gotcha #3), so every
token binding would first have to depend on a notifying property. Know that
cost before starting; a restart is the supported path today.

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
import re
import stat
from pathlib import Path

from .state_paths import config_home

log = logging.getLogger(__name__)

#: Env var pointing directly at a scheme file. Highest priority; used by
#: worktree runs to preview a palette without touching the user's config.
SCHEME_ENV = "SYMMETRIA_UI_SCHEME"

#: Path under the XDG config dir, shared with FmTheme's `_configDir`.
SCHEME_RELATIVE_PATH = Path("symmetria/ui/color-scheme.json")

#: Upper bound on the scheme file. A real scheme is ~3 KB; anything past this
#: is not one, and the read happens on the GUI thread before `engine.load()`.
_MAX_BYTES = 256_000

#: Accepted colour forms, with or without the leading ``#``: RGB, RGBA, RRGGBB,
#: AARRGGBB. Anything else is skipped rather than passed through — see
#: :func:`load_scheme` for why passing it through is worse than dropping it.
_HEX_COLOUR = re.compile(r"#?(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})\Z")


def scheme_path() -> Path:
    """Return the scheme file path, whether or not it exists.

    Resolution is env-first, then XDG (via ``state_paths.config_home``, which
    also documents why a relative ``$XDG_CONFIG_HOME`` is ignored). The
    returned path is NOT checked for existence — callers that need that should
    use :func:`load_scheme`, which treats a missing file as an empty scheme.
    """
    override = os.environ.get(SCHEME_ENV, "").strip()
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_absolute():
            return candidate
        # A relative override would resolve against the cwd, which differs per
        # IDE window — same hazard as a relative $XDG_CONFIG_HOME. Refuse it
        # loudly rather than reading a different file per window.
        log.warning(
            "%s is relative (%r); ignoring it and using the XDG path",
            SCHEME_ENV,
            override,
        )

    return config_home() / SCHEME_RELATIVE_PATH


def load_scheme(path: Path | None = None) -> dict[str, str]:
    """Load the colour scheme as ``{role_name: "#rrggbb"}``.

    ``path`` defaults to :func:`scheme_path`; passing one explicitly is what
    makes this testable without env juggling (same shape as
    ``server_registry.load_servers``).

    Never raises. A missing file, unreadable file, malformed JSON, or a payload
    without a ``colours`` object all yield ``{}`` — the caller then falls back
    to its built-in literals, which is the normal case rather than an error
    path.

    Values are VALIDATED as hex colours, and a bad one is dropped rather than
    passed through. That matters more than it looks: QML converts a string to
    ``color`` at binding time, and an unconvertible string does not fall back
    to the property's default — it lands on Qt's own default (black or
    transparent). Passing ``"red"`` through as ``"#red"`` would therefore blank
    a token rather than leave it alone, and the only trace would be a Qt
    conversion warning the IDE's message handler mostly swallows. Dropping the
    key keeps the built-in literal, which is the documented contract.
    """
    target = path if path is not None else scheme_path()
    try:
        # stat before read: the path is fully user-controlled through
        # $SYMMETRIA_UI_SCHEME, and this runs on the GUI thread before
        # `engine.load()`. Reading a FIFO there would block startup forever
        # with no timeout; reading a huge file would stall it.
        info = target.stat()
        if not stat.S_ISREG(info.st_mode):
            log.warning("ui scheme at %s is not a regular file; ignoring", target)
            return {}
        if info.st_size > _MAX_BYTES:
            log.warning(
                "ui scheme at %s is %d bytes (max %d); ignoring",
                target,
                info.st_size,
                _MAX_BYTES,
            )
            return {}
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        log.warning("ui scheme unreadable at %s: %s", target, exc)
        return {}
    except UnicodeDecodeError as exc:
        log.warning("ui scheme at %s is not UTF-8: %s", target, exc)
        return {}

    try:
        payload = json.loads(raw)
    except ValueError as exc:
        log.warning("ui scheme is not valid JSON at %s: %s", target, exc)
        return {}

    colours = payload.get("colours") if isinstance(payload, dict) else None
    if not isinstance(colours, dict):
        log.warning("ui scheme at %s has no 'colours' object", target)
        return {}

    resolved: dict[str, str] = {}
    for key, value in colours.items():
        if not isinstance(value, str):
            log.warning("ui scheme: skipping %s (value is not a string)", key)
            continue
        text = value.strip()
        if not _HEX_COLOUR.match(text):
            log.warning("ui scheme: skipping %s=%r (not a hex colour)", key, value)
            continue
        # Tolerate both "0f0f10" (the shell's convention) and "#0f0f10".
        resolved[str(key)] = text if text.startswith("#") else f"#{text}"

    log.info("ui scheme loaded from %s (%d colours)", target, len(resolved))
    return resolved
