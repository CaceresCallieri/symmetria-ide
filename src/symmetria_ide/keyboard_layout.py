"""Resolve the HOST session's keyboard layout, for the nested browser compositor.

Why this module exists — a Qt defect, measured 2026-08-04
--------------------------------------------------------
Instantiating a `QWaylandCompositor` anywhere in the process makes the HOST
window stop translating keys with the keymap the real compositor sent it, and
start using the NESTED compositor's seat keymap instead. `QWaylandKeymap`
defaults to the xkb defaults — i.e. layout "us" — so on any non-US layout the
entire IDE silently switches to US the moment the browser pane loads.

Measured with a minimal probe (a plain QQuickWindow, keys injected at fixed
scancodes, X11 scancode 47 as the discriminator — US ";" vs latam "n-tilde"):

    no compositor .................. SC47=';'  with an injected keymap honoured
    + WaylandCompositor (empty) .... SC47=';'  US, injected keymap ignored
    + keymap forced to "latam" ..... SC47='n-tilde', SC21='inverted-?'

The middle row is the bug: an EMPTY `WaylandCompositor` with no shell, no
output and no client attached is already enough. Deferring its creation only
delays the damage — the same window translates correctly before the compositor
is constructed and incorrectly one instant after, so lazy-loading the pane is
not a fix. The third row is the workaround this module feeds: tell the nested
compositor to use the host's own layout, and the hijacked translation lands on
the right characters anyway.

Remove this module if Qt ever stops leaking the nested seat's keymap into the
host client's key translation. The probe above is the way to re-check: if the
"+ WaylandCompositor (empty)" row starts honouring the injected keymap, the
defect is gone.

Three things this does NOT fix, all deliberate:

* The host still ignores keymap CHANGES pushed by the real compositor — it is
  pinned to whatever we resolved at startup. In practice that only shows up for
  virtual-keyboard tools that upload their own keymap (`wtype`, and anything
  built on `zwp_virtual_keyboard_v1`); text they inject into the IDE is
  translated with the host layout instead of theirs. A physical keyboard, and a
  layout change made the ordinary way, are both unaffected. Fixing this
  properly means moving the nested compositor out of the IDE's process.
* For the same reason, a multi-group config's GROUP SWITCH is not followed
  either. We rotate the layout list so the group that is active at startup
  lands first (see `_rotate_to_active`), but toggling groups afterwards does
  not move the pinned keymap.
* The resulting keymap compiles as e.g. `pc_latam_us_2_inet` — Qt keeps a
  second, unreachable "us" group of its own. Group 1 is ours and is the active
  one, so it types correctly; the stray group is cosmetic. Confirmed identical
  whether the RMLVO fields are assigned individually or all at once, so it is
  Qt's doing, not the assignment style's.

Precedence, most authoritative first:

1. `SYMMETRIA_IDE_KEYMAP_*` — explicit override / test hook. **All-or-nothing:**
   unset fields become empty, they do NOT fall through to the next source. So
   overriding only the layout drops the host's `options` (a `caps:escape` remap
   included) — set the whole tuple, or none of it.
2. The running Hyprland session, via `hyprctl -j devices`. This is the ONLY
   source that reports what the user is actually typing on right now.
3. `XKB_DEFAULT_*` — the standard xkb env contract, for non-Hyprland hosts.
   Redundant in effect (Qt forwards unset fields to `xkb_keymap_new_from_names`
   as empty strings, and libxkbcommon substitutes `XKB_DEFAULT_*` for those
   itself), but kept explicit so the resolved tuple is inspectable and testable
   rather than resolved invisibly inside xkb.
4. Empty strings, which reproduce today's behaviour — US unless `XKB_DEFAULT_*`
   says otherwise — rather than guessing a layout we have no evidence for.

The full derivation, the forensics that found it and the re-check one-liner
live in `.claude/memory/reference/qt-pyside/nested_compositor_hijacks_host_keymap.md`.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from collections.abc import Callable, Mapping

log = logging.getLogger(__name__)

# The RMLVO tuple, in xkb's own order. These are exactly the settable fields of
# Qt's `QWaylandKeymap`, so the dict this module returns maps onto it 1:1.
XKB_FIELDS = ("rules", "model", "layout", "variant", "options")

_EMPTY_KEYMAP: dict[str, str] = dict.fromkeys(XKB_FIELDS, "")

# This subprocess runs on the GUI thread, before the window exists, on EVERY
# launch — browser project or not. Measured at ~7ms, so the cap exists only to
# bound a wedged compositor: something that has not answered in half a second
# is not going to, and a frozen startup buys nothing.
_HYPRCTL_TIMEOUT_SEC = 0.5


def empty_keymap() -> dict[str, str]:
    """A keymap with every field unset — xkb then falls back to its defaults."""
    return dict(_EMPTY_KEYMAP)


def _named_keymap(source: Mapping[str, object], prefix: str) -> dict[str, str] | None:
    """Read an RMLVO tuple out of `source` using `<prefix><FIELD>` keys.

    Returns None unless a `layout` was found — layout is the only field whose
    absence makes the rest meaningless, and an options-only override would
    silently pin the layout to the xkb default, which is the bug we are fixing.
    """
    found = {
        field: str(source.get(f"{prefix}{field.upper()}") or "").strip()
        for field in XKB_FIELDS
    }
    if not found["layout"]:
        return None
    return found


def _active_layout_index(entry: Mapping[str, object]) -> int:
    """The keyboard's active group, or 0 when absent or unparseable."""
    try:
        index = int(str(entry.get("active_layout_index") or 0))
    except (TypeError, ValueError):
        return 0
    return max(index, 0)


def _rotate_to_active(value: str, index: int) -> str:
    """Rotate a comma-separated RMLVO list so the active entry lands first.

    Rotation rather than a swap, so the remaining groups keep their relative
    order. A short or empty list (a `variant` of "" against a two-layout
    `layout`) is returned untouched — there is nothing to reorder, and xkb
    pads missing variants itself.
    """
    parts = value.split(",")
    if index <= 0 or index >= len(parts):
        return value
    return ",".join(parts[index:] + parts[:index])


def keymap_from_hyprland_devices(payload: object) -> dict[str, str] | None:
    """Pick the active keyboard's RMLVO tuple out of `hyprctl -j devices` output.

    Prefers the keyboard Hyprland marks `main` — with kanata or any other
    virtual keyboard in play there are a dozen entries and only one of them is
    the seat's actual keyboard. Falls back to the first entry that declares a
    layout at all, so the parse still yields something on a Hyprland version
    that stops emitting `main`.
    """
    if not isinstance(payload, Mapping):
        return None
    keyboards = payload.get("keyboards")
    if not isinstance(keyboards, list):
        return None

    def _tuple_for(entry: object) -> dict[str, str] | None:
        if not isinstance(entry, Mapping):
            return None
        found = {field: str(entry.get(field) or "").strip() for field in XKB_FIELDS}
        if not found["layout"]:
            return None
        # A multi-group config (`kb_layout = us,latam`) can be sitting on any
        # group. xkb exposes no "active group" knob on a keymap, so the only
        # lever is ORDER: rotate the active entry to the front and it becomes
        # group 1, which is the group Qt's seat starts on.
        active = _active_layout_index(entry)
        if active:
            found["layout"] = _rotate_to_active(found["layout"], active)
            found["variant"] = _rotate_to_active(found["variant"], active)
        return found

    fallback: dict[str, str] | None = None
    candidates: list[tuple[str, str]] = []
    for entry in keyboards:
        parsed = _tuple_for(entry)
        if parsed is None:
            continue
        if isinstance(entry, Mapping):
            candidates.append((str(entry.get("name") or "?"), parsed["layout"]))
            if entry.get("main") is True:
                return parsed
        if fallback is None:
            fallback = parsed

    # No keyboard claimed `main`. Picking the first is arbitrary, and harmless
    # only while every device agrees — so say so when they do not, rather than
    # letting an arbitrary keyboard's layout become the whole IDE's silently.
    if fallback is not None and len({layout for _, layout in candidates}) > 1:
        log.warning(
            "no main keyboard reported and layouts disagree %r — using %r",
            candidates,
            fallback["layout"],
        )
    return fallback


def _read_hyprland_devices() -> object | None:
    """Run `hyprctl -j devices`, or return None if this is not a Hyprland session.

    Called once, at engine build time. `hyprctl` answers over a unix socket in
    a few milliseconds, but it is still a subprocess on the GUI thread, hence
    the hard timeout and the blanket except: a slow or wedged compositor must
    degrade to "no layout information", never stall startup.
    """
    if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return None
    try:
        completed = subprocess.run(
            ["hyprctl", "-j", "devices"],
            capture_output=True,
            text=True,
            timeout=_HYPRCTL_TIMEOUT_SEC,
            check=False,
        )
    # ValueError is in the tuple for `text=True`: it decodes stdout inside the
    # call, and a device name carrying a non-UTF-8 byte (they come from
    # hardware USB strings) raises UnicodeDecodeError, which is a ValueError
    # and NOT a SubprocessError.
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        log.debug("hyprctl devices failed: %s", exc)
        return None
    if completed.returncode != 0:
        log.debug("hyprctl devices exited %d", completed.returncode)
        return None
    try:
        return json.loads(completed.stdout)
    except ValueError as exc:
        log.debug("hyprctl devices returned non-JSON: %s", exc)
        return None


def resolve_host_keymap(
    environ: Mapping[str, str] | None = None,
    devices_reader: Callable[[], object] = _read_hyprland_devices,
) -> dict[str, str]:
    """Return the RMLVO tuple the nested compositor should be configured with.

    Never raises and never returns None — an unknown layout degrades to
    `empty_keymap()`, which is exactly today's behaviour. That contract is
    load-bearing: this runs inside `_build_engine`, which nothing above
    guards, so an escaping exception would stop the IDE from starting at all
    over a keyboard-layout nicety.
    """
    env = os.environ if environ is None else environ

    override = _named_keymap(env, "SYMMETRIA_IDE_KEYMAP_")
    if override is not None:
        return override

    # Blanket except, deliberately: the reader is injectable, so its failure
    # modes are open-ended, and the docstring above promises this cannot throw.
    try:
        devices = devices_reader()
    except Exception as exc:  # noqa: BLE001 — see comment above
        log.debug("keymap device reader failed: %s", exc)
        devices = None

    from_session = keymap_from_hyprland_devices(devices)
    if from_session is not None:
        return from_session

    from_env = _named_keymap(env, "XKB_DEFAULT_")
    if from_env is not None:
        return from_env

    return empty_keymap()
