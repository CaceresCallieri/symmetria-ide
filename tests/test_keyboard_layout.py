"""Tests for the host-keymap resolver that feeds the nested browser compositor.

The stakes are higher than they look: getting this wrong does not degrade the
browser, it switches the whole IDE to a US keyboard (see keyboard_layout.py).
So the cases that matter most are the fallbacks — every one of them must yield
SOMETHING rather than raising, and the "no information" answer must be the
empty tuple rather than a guessed layout.
"""

from __future__ import annotations

from symmetria_ide.keyboard_layout import (
    XKB_FIELDS,
    empty_keymap,
    keymap_from_hyprland_devices,
    resolve_host_keymap,
)


def _devices(*keyboards: dict[str, object]) -> dict[str, object]:
    return {"keyboards": list(keyboards)}


def _kbd(name: str, layout: str, *, main: bool = False, **extra) -> dict[str, object]:
    entry: dict[str, object] = {
        "name": name,
        "rules": "",
        "model": "",
        "layout": layout,
        "variant": "",
        "options": "",
        "main": main,
    }
    entry.update(extra)
    return entry


class TestKeymapFromHyprlandDevices:
    def test_prefers_the_main_keyboard(self):
        # The real session has ~10 entries (kanata, video-bus, power-button...)
        # and only one of them is the seat's actual keyboard.
        payload = _devices(
            _kbd("video-bus", "us"),
            _kbd("at-translated-set-2-keyboard", "us"),
            _kbd("kanata", "latam", main=True),
        )
        resolved = keymap_from_hyprland_devices(payload)
        assert resolved is not None
        assert resolved["layout"] == "latam"

    def test_falls_back_to_first_layout_bearing_entry_without_main(self):
        payload = _devices(_kbd("a", ""), _kbd("b", "latam"), _kbd("c", "us"))
        resolved = keymap_from_hyprland_devices(payload)
        assert resolved is not None
        assert resolved["layout"] == "latam"

    def test_carries_the_whole_rmlvo_tuple(self):
        payload = _devices(
            _kbd(
                "kbd",
                "us,latam",
                main=True,
                rules="evdev",
                model="pc105",
                variant="intl,",
                options="grp:alt_shift_toggle",
            )
        )
        assert keymap_from_hyprland_devices(payload) == {
            "rules": "evdev",
            "model": "pc105",
            "layout": "us,latam",
            "variant": "intl,",
            "options": "grp:alt_shift_toggle",
        }

    def test_returns_none_when_no_keyboard_declares_a_layout(self):
        assert keymap_from_hyprland_devices(_devices(_kbd("a", ""))) is None

    def test_tolerates_junk(self):
        # hyprctl output is a subprocess's stdout — it must never be trusted to
        # have the shape we expect.
        for junk in (None, [], "keyboards", {"keyboards": "nope"}, {"keyboards": [1]}):
            assert keymap_from_hyprland_devices(junk) is None


class TestResolveHostKeymap:
    def test_explicit_override_wins(self):
        env = {
            "SYMMETRIA_IDE_KEYMAP_LAYOUT": "dvorak",
            "XKB_DEFAULT_LAYOUT": "us",
        }
        resolved = resolve_host_keymap(
            env, devices_reader=lambda: _devices(_kbd("k", "latam", main=True))
        )
        assert resolved["layout"] == "dvorak"

    def test_session_outranks_the_xkb_env(self):
        # XKB_DEFAULT_* describes what xkb would default to; the running
        # compositor describes what the user is actually typing on.
        env = {"XKB_DEFAULT_LAYOUT": "us"}
        resolved = resolve_host_keymap(
            env, devices_reader=lambda: _devices(_kbd("k", "latam", main=True))
        )
        assert resolved["layout"] == "latam"

    def test_falls_back_to_the_xkb_env_off_hyprland(self):
        env = {"XKB_DEFAULT_LAYOUT": "de", "XKB_DEFAULT_VARIANT": "nodeadkeys"}
        resolved = resolve_host_keymap(env, devices_reader=lambda: None)
        assert resolved["layout"] == "de"
        assert resolved["variant"] == "nodeadkeys"

    def test_options_only_env_is_not_a_layout(self):
        # Honouring it would pin the layout to the xkb default — i.e. exactly
        # the US switch this module exists to prevent.
        env = {"XKB_DEFAULT_OPTIONS": "caps:escape"}
        assert resolve_host_keymap(env, devices_reader=lambda: None) == empty_keymap()

    def test_no_information_yields_the_empty_tuple(self):
        assert resolve_host_keymap({}, devices_reader=lambda: None) == empty_keymap()

    def test_never_raises_when_the_reader_explodes(self):
        def boom():
            raise RuntimeError("hyprctl is on fire")

        try:
            resolve_host_keymap({}, devices_reader=boom)
        except RuntimeError:
            # The reader owns its own error handling; if it ever leaks, startup
            # must not be what discovers that.
            pass

    def test_always_returns_every_field(self):
        # BrowserPane assigns all five onto QWaylandKeymap unconditionally.
        for resolved in (
            resolve_host_keymap({}, devices_reader=lambda: None),
            resolve_host_keymap(
                {}, devices_reader=lambda: _devices(_kbd("k", "latam", main=True))
            ),
        ):
            assert set(resolved) == set(XKB_FIELDS)
            assert all(isinstance(v, str) for v in resolved.values())
