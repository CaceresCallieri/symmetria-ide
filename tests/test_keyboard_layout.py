"""Tests for the host-keymap resolver that feeds the nested browser compositor.

The stakes are higher than they look: getting this wrong does not degrade the
browser, it switches the whole IDE to a US keyboard (see keyboard_layout.py).
So the cases that matter most are the fallbacks — every one of them must yield
SOMETHING rather than raising, and the "no information" answer must be the
empty tuple rather than a guessed layout.
"""

from __future__ import annotations

import subprocess

import pytest

from symmetria_ide import keyboard_layout
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


class TestActiveGroupRotation:
    """A multi-group config must pin the group the user is ACTUALLY on.

    xkb exposes no active-group setter, so order is the only lever: the active
    entry has to become group 1. Getting this wrong reproduces the very bug
    this module exists to fix, just for `kb_layout = us,latam` users.
    """

    def test_rotates_layout_and_variant_to_the_active_group(self):
        payload = _devices(
            _kbd(
                "kbd",
                "us,latam",
                main=True,
                variant="intl,",
                active_layout_index=1,
                options="grp:alt_shift_toggle",
            )
        )
        resolved = keymap_from_hyprland_devices(payload)
        assert resolved is not None
        assert resolved["layout"] == "latam,us"
        assert resolved["variant"] == ",intl"

    def test_group_zero_is_left_untouched(self):
        payload = _devices(
            _kbd("kbd", "us,latam", main=True, variant="intl,", active_layout_index=0)
        )
        resolved = keymap_from_hyprland_devices(payload)
        assert resolved is not None
        assert resolved["layout"] == "us,latam"
        assert resolved["variant"] == "intl,"

    def test_single_layout_is_a_no_op_at_any_index(self):
        # The common case, and the one running in production — an
        # out-of-range index must never mangle it.
        payload = _devices(_kbd("kbd", "latam", main=True, active_layout_index=3))
        resolved = keymap_from_hyprland_devices(payload)
        assert resolved is not None
        assert resolved["layout"] == "latam"

    def test_junk_index_degrades_to_group_zero(self):
        for junk in ("two", None, [], {}):
            payload = _devices(
                _kbd("kbd", "us,latam", main=True, active_layout_index=junk)
            )
            resolved = keymap_from_hyprland_devices(payload)
            assert resolved is not None
            assert resolved["layout"] == "us,latam"


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
        # Load-bearing: this runs inside _build_engine, which nothing above
        # guards, so a leaked exception stops the IDE from starting at all.
        def boom():
            raise RuntimeError("hyprctl is on fire")

        assert resolve_host_keymap({}, devices_reader=boom) == empty_keymap()

    def test_a_multi_group_session_reaches_the_caller_rotated(self):
        # End-to-end through the public entry point, not just the parser.
        resolved = resolve_host_keymap(
            {},
            devices_reader=lambda: _devices(
                _kbd("k", "us,latam", main=True, active_layout_index=1)
            ),
        )
        assert resolved["layout"] == "latam,us"

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


class TestReadHyprlandDevices:
    """The subprocess boundary — the piece most likely to break on a Hyprland
    CLI or schema change, and the one that must degrade to None rather than
    raise, because nothing above `_build_engine` guards it."""

    @pytest.fixture(autouse=True)
    def _hyprland_session(self, monkeypatch):
        monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "test-signature")

    def test_returns_none_without_running_anything_off_hyprland(self, monkeypatch):
        monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)

        def explode(*args, **kwargs):
            raise AssertionError("must not shell out off a Hyprland session")

        monkeypatch.setattr(keyboard_layout.subprocess, "run", explode)
        assert keyboard_layout._read_hyprland_devices() is None

    def _run_returning(self, monkeypatch, *, stdout="", returncode=0):
        completed = subprocess.CompletedProcess(
            args=["hyprctl"], returncode=returncode, stdout=stdout, stderr=""
        )
        monkeypatch.setattr(
            keyboard_layout.subprocess, "run", lambda *a, **k: completed
        )

    def test_parses_valid_json(self, monkeypatch):
        self._run_returning(monkeypatch, stdout='{"keyboards": []}')
        assert keyboard_layout._read_hyprland_devices() == {"keyboards": []}

    def test_non_zero_exit_is_none(self, monkeypatch):
        self._run_returning(monkeypatch, stdout='{"keyboards": []}', returncode=1)
        assert keyboard_layout._read_hyprland_devices() is None

    def test_non_json_stdout_is_none(self, monkeypatch):
        self._run_returning(monkeypatch, stdout="not json at all")
        assert keyboard_layout._read_hyprland_devices() is None

    @pytest.mark.parametrize(
        "error",
        [
            subprocess.TimeoutExpired(cmd="hyprctl", timeout=0.5),
            FileNotFoundError("hyprctl"),
            # text=True decodes inside the call, so a hardware string with a
            # non-UTF-8 byte raises this — a ValueError, NOT a SubprocessError.
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
        ],
    )
    def test_subprocess_failures_degrade_to_none(self, monkeypatch, error):
        def raise_it(*args, **kwargs):
            raise error

        monkeypatch.setattr(keyboard_layout.subprocess, "run", raise_it)
        assert keyboard_layout._read_hyprland_devices() is None
