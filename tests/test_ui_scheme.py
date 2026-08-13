"""Tests for the optional shared toolkit colour scheme.

The module's whole contract is "never raises, and a bad value falls back to the
caller's literal rather than blanking a token" — neither of which any other
test would notice failing, because a blanked colour is a silently wrong pixel,
not an exception.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from symmetria_ide.ui_scheme import SCHEME_ENV, load_scheme, scheme_path


def _write_scheme(path: Path, colours: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"colours": colours}), encoding="utf-8")
    return path


# ── Path resolution ──────────────────────────────────────────────────


def test_env_override_wins_over_xdg(monkeypatch, tmp_path):
    override = tmp_path / "custom" / "scheme.json"
    monkeypatch.setenv(SCHEME_ENV, str(override))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    assert scheme_path() == override


def test_relative_env_override_is_ignored(monkeypatch, tmp_path):
    """A relative override would resolve against the cwd, which differs per
    IDE window — the XDG path must win instead."""
    monkeypatch.setenv(SCHEME_ENV, "some/relative/scheme.json")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    resolved = scheme_path()
    assert resolved.is_absolute()
    assert resolved == tmp_path / "xdg" / "symmetria" / "ui" / "color-scheme.json"


def test_xdg_config_home_is_honoured(monkeypatch, tmp_path):
    monkeypatch.delenv(SCHEME_ENV, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    assert scheme_path() == tmp_path / "xdg" / "symmetria" / "ui" / "color-scheme.json"


def test_defaults_to_dot_config_when_no_env(monkeypatch, tmp_path):
    """With both vars deleted the path must land under ``~/.config``.

    Required by `.claude/rules/test_env_isolation.md`: a var the suite
    neutralises also needs one test that deletes it and asserts the default.
    """
    monkeypatch.delenv(SCHEME_ENV, raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))

    expected = tmp_path / "home" / ".config" / "symmetria" / "ui" / "color-scheme.json"
    assert scheme_path() == expected


def test_relative_xdg_config_home_is_ignored(monkeypatch, tmp_path):
    monkeypatch.delenv(SCHEME_ENV, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/config")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))

    assert scheme_path().is_absolute()
    assert str(scheme_path()).startswith(str(tmp_path / "home" / ".config"))


# ── Loading: the normal case ─────────────────────────────────────────


def test_loads_and_prefixes_bare_hex(tmp_path):
    path = _write_scheme(tmp_path / "s.json", {"surface": "0f0f10"})

    assert load_scheme(path) == {"surface": "#0f0f10"}


def test_accepts_already_prefixed_hex(tmp_path):
    path = _write_scheme(tmp_path / "s.json", {"surface": "#0f0f10"})

    assert load_scheme(path) == {"surface": "#0f0f10"}


@pytest.mark.parametrize("value", ["abc", "abcd", "0f0f10", "ff0f0f10"])
def test_accepts_every_hex_length(tmp_path, value):
    path = _write_scheme(tmp_path / "s.json", {"surface": value})

    assert load_scheme(path) == {"surface": f"#{value}"}


def test_surrounding_whitespace_is_stripped(tmp_path):
    path = _write_scheme(tmp_path / "s.json", {"surface": "  0f0f10  "})

    assert load_scheme(path) == {"surface": "#0f0f10"}


# ── Loading: every failure path yields {} or drops one key ───────────


def test_missing_file_is_not_an_error(tmp_path):
    assert load_scheme(tmp_path / "absent.json") == {}


def test_non_regular_file_is_ignored(tmp_path):
    """Guards the FIFO case: this read happens on the GUI thread before
    `engine.load()`, so a blocking open would hang startup with no timeout."""
    directory = tmp_path / "adir"
    directory.mkdir()

    assert load_scheme(directory) == {}


def test_oversized_file_is_ignored(tmp_path):
    path = tmp_path / "big.json"
    path.write_text(" " * 300_000, encoding="utf-8")

    assert load_scheme(path) == {}


def test_malformed_json_is_ignored(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{not json", encoding="utf-8")

    assert load_scheme(path) == {}


def test_payload_without_colours_is_ignored(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"mode": "dark"}), encoding="utf-8")

    assert load_scheme(path) == {}


def test_non_object_payload_is_ignored(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps(["surface", "0f0f10"]), encoding="utf-8")

    assert load_scheme(path) == {}


def test_non_utf8_file_is_ignored(tmp_path):
    path = tmp_path / "s.json"
    path.write_bytes(b'{"colours": {"surface": "\xff\xfe"}}')

    assert load_scheme(path) == {}


@pytest.mark.parametrize(
    "bad",
    ["red", "0f0f1", "#gggggg", "", "0f 0f 10", "rgb(1,2,3)"],
)
def test_invalid_colour_is_dropped_not_passed_through(tmp_path, bad):
    """Dropping the key keeps the caller's literal. Passing it through would
    make QML fail the string->color conversion and land the property on Qt's
    own default instead — a blanked token, not the documented fallback."""
    path = _write_scheme(tmp_path / "s.json", {"surface": "0f0f10", "onSurface": bad})

    assert load_scheme(path) == {"surface": "#0f0f10"}


def test_non_string_value_is_dropped(tmp_path):
    path = _write_scheme(tmp_path / "s.json", {"surface": "0f0f10", "onSurface": 42})

    assert load_scheme(path) == {"surface": "#0f0f10"}


def test_one_bad_key_does_not_discard_the_scheme(tmp_path):
    path = _write_scheme(
        tmp_path / "s.json",
        {"surface": "0f0f10", "broken": "nope", "onSurface": "d4d4d8"},
    )

    assert load_scheme(path) == {"surface": "#0f0f10", "onSurface": "#d4d4d8"}
