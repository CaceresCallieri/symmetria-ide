"""Pure-logic tests for chrome_host's identity + profile seeding.

The invariants here all descend from one fact: Chrome is a singleton per
`--user-data-dir`, so profile identity IS process identity IS window class IS
workspace. Two IDEs sharing a profile would silently collapse onto one Chrome
process, and one of them would lose its window pinning.
"""

import os

import pytest

from symmetria_ide import chrome_host


class TestIdentity:
    def test_window_class_is_per_process(self):
        """Two IDEs must never share a class — the rule addresses ONE IDE."""
        assert chrome_host.window_class_for(10) != chrome_host.window_class_for(11)
        assert chrome_host.window_class_for(42) == "symmetria-browser-42"

    def test_slug_keeps_the_basename_legible(self):
        assert chrome_host.project_slug("/home/jc/projects/bambin").startswith(
            "bambin-"
        )

    def test_same_basename_different_path_gets_a_different_profile(self):
        """dev vs stable, or two worktrees: same name, separate IDE instances."""
        dev = chrome_host.project_slug("/home/jc/projects/symmetria-ide")
        stable = chrome_host.project_slug("/home/jc/projects/symmetria-ide-stable")
        assert dev != stable

    def test_slug_is_stable_across_calls(self):
        path = "/home/jc/projects/thing"
        assert chrome_host.project_slug(path) == chrome_host.project_slug(path)

    def test_trailing_slash_does_not_fork_the_profile(self):
        assert chrome_host.project_slug("/a/b") == chrome_host.project_slug("/a/b/")

    @pytest.mark.parametrize(
        "root", ["", "/", "/weird path/with:chars"], ids=["empty", "root", "unsafe"]
    )
    def test_slug_is_always_filesystem_safe(self, root):
        slug = chrome_host.project_slug(root)
        assert slug and "/" not in slug and ":" not in slug and " " not in slug


class TestArgv:
    def _argv(self, port=9222):
        return chrome_host.build_chrome_argv("/usr/bin/chrome", "cls", "/tmp/p", port)

    def test_carries_class_profile_and_port(self):
        argv = self._argv()
        assert "--class=cls" in argv
        assert "--user-data-dir=/tmp/p" in argv
        assert "--remote-debugging-port=9222" in argv

    def test_never_uses_app_mode(self):
        """The user explicitly wants the FULL browser: tabs + omnibox.

        `--app=` would strip both. This browser doubles as something the user
        shows to other people, not just an agent viewport.
        """
        assert not any(arg.startswith("--app") for arg in self._argv())

    def test_no_port_means_no_debugging_flag(self):
        assert not any("remote-debugging" in arg for arg in self._argv(port=0))


class TestExecutableResolution:
    """`SYMMETRIA_IDE_CHROME_BIN` is both a real override and the suite's
    off switch (conftest points it at a nonexistent path), so the DEFAULT
    branch is otherwise unreachable from the suite — these cover it."""

    def test_default_searches_path(self, monkeypatch):
        monkeypatch.delenv("SYMMETRIA_IDE_CHROME_BIN", raising=False)
        monkeypatch.setattr(
            chrome_host.shutil,
            "which",
            lambda name: (
                "/usr/bin/google-chrome-stable"
                if name == "google-chrome-stable"
                else None
            ),
        )
        assert chrome_host.chrome_executable() == "/usr/bin/google-chrome-stable"

    def test_default_returns_empty_when_no_chrome_installed(self, monkeypatch):
        monkeypatch.delenv("SYMMETRIA_IDE_CHROME_BIN", raising=False)
        monkeypatch.setattr(chrome_host.shutil, "which", lambda name: None)
        assert chrome_host.chrome_executable() == ""

    def test_override_wins_when_executable(self, tmp_path, monkeypatch):
        binary = tmp_path / "chrome"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        monkeypatch.setenv("SYMMETRIA_IDE_CHROME_BIN", str(binary))
        assert chrome_host.chrome_executable() == str(binary)

    def test_unusable_override_does_not_fall_back_to_path(self, monkeypatch):
        """A typo'd override must disable the browser, not silently launch
        some other browser that happens to be installed."""
        monkeypatch.setenv("SYMMETRIA_IDE_CHROME_BIN", "/nope/chrome")
        monkeypatch.setattr(chrome_host.shutil, "which", lambda name: "/usr/bin/chrome")
        assert chrome_host.chrome_executable() == ""

    def test_conftest_isolation_actually_disables_spawning(self):
        """Guard on the guard: if this ever passes a real path, every test
        that opens a browser starts launching Chrome for real."""
        assert chrome_host.chrome_executable() == ""


class TestProfileSeeding:
    def _template(self, tmp_path, with_local_state=True):
        template = tmp_path / "_template"
        (template / "Default").mkdir(parents=True)
        (template / "Default" / "Cookies").write_text("cookiejar")
        (template / "Default" / "Preferences").write_text("{}")
        (template / "Default" / "Local Storage").mkdir()
        (template / "Default" / "Local Storage" / "leveldb").write_text("ls")
        # Cache is the bulk of a real profile and must NOT be copied.
        (template / "Default" / "Cache").mkdir()
        (template / "Default" / "Cache" / "big").write_text("x" * 1000)
        if with_local_state:
            (template / "Local State").write_text("encrypted_key")
        return template

    def test_seeds_logins_into_a_fresh_profile(self, tmp_path):
        template = self._template(tmp_path)
        profile = tmp_path / "project"
        assert chrome_host.seed_profile_from_template(str(profile), str(template))
        assert (profile / "Default" / "Cookies").read_text() == "cookiejar"
        assert (profile / "Default" / "Local Storage" / "leveldb").exists()

    def test_copies_local_state(self, tmp_path):
        """Without it every copied cookie decrypts to garbage — the OS-crypt
        wrapped key reference lives at the profile ROOT, not in Default."""
        template = self._template(tmp_path)
        profile = tmp_path / "project"
        chrome_host.seed_profile_from_template(str(profile), str(template))
        assert (profile / "Local State").read_text() == "encrypted_key"

    def test_does_not_copy_the_cache(self, tmp_path):
        """A seeded profile is hundreds of MB if you copy the tree."""
        template = self._template(tmp_path)
        profile = tmp_path / "project"
        chrome_host.seed_profile_from_template(str(profile), str(template))
        assert not (profile / "Default" / "Cache").exists()

    def test_never_overwrites_an_existing_profile(self, tmp_path):
        """Overwriting would destroy sessions established inside the project."""
        template = self._template(tmp_path)
        profile = tmp_path / "project"
        (profile / "Default").mkdir(parents=True)
        (profile / "Default" / "Cookies").write_text("mine")
        assert not chrome_host.seed_profile_from_template(str(profile), str(template))
        assert (profile / "Default" / "Cookies").read_text() == "mine"

    def test_missing_template_is_a_no_op_not_an_error(self, tmp_path):
        """No template just means "log in inside the project"."""
        profile = tmp_path / "project"
        assert not chrome_host.seed_profile_from_template(
            str(profile), str(tmp_path / "nope")
        )

    def test_profile_lives_under_xdg_data_home(self, tmp_path, monkeypatch):
        """§9: never write outside XDG without a user-provided path."""
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        path = chrome_host.profile_dir_for("/home/jc/projects/thing")
        assert path.startswith(os.path.join(str(tmp_path), "symmetria-ide", "browser"))
