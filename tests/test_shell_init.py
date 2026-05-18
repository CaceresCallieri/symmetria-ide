"""Tests for Phase 2.5 deliverable 3 PR 1: shell-init scripts + auto-injection.

Two surfaces under test:

1. The `runtime/symmetria-shell/{zsh,bash}/` shell scripts themselves —
   structural assertions that the OSC 7 emitter is present, the user's
   real rcfile is sourced (so their setup is preserved), and the chpwd
   hook is appended (not replacing user's existing hooks).

2. `terminal_backend._shell_launch_args(shell_path)` — returns the right
   (argv, env_additions) tuple per shell so the spawned shell loads the
   correct bootstrap. zsh gets ZDOTDIR redirect, bash gets --rcfile,
   anything else gets a plain -l spawn + a warning log.

Pure-function shape — no Qt instantiation, no subprocess, no PTY. The
live OSC 7 round-trip will be validated when PR 2 wires up the parser
and PR 3 routes into _cwd; this PR just lays the auto-injection
plumbing.
"""

from __future__ import annotations

import logging

import pytest

from symmetria_ide.terminal_backend import _SHELL_INIT_DIR, _shell_launch_args


# ---------------------------------------------------------------------------
# Shell init scripts — runtime/symmetria-shell/zsh/ + bash/init.sh
# ---------------------------------------------------------------------------


def test_shell_init_dir_exists():
    """The runtime/symmetria-shell/ directory must exist — TerminalBackend
    references it at module load time via `_SHELL_INIT_DIR`. A missing
    directory means `_shell_launch_args` points zsh/bash at nonexistent
    paths and the spawned shell silently falls back to user's rc files
    without the OSC 7 hook installed."""
    assert _SHELL_INIT_DIR.is_dir(), f"missing: {_SHELL_INIT_DIR}"


@pytest.fixture(scope="module")
def zsh_zshenv() -> str:
    return (_SHELL_INIT_DIR / "zsh" / ".zshenv").read_text()


@pytest.fixture(scope="module")
def zsh_zshrc() -> str:
    return (_SHELL_INIT_DIR / "zsh" / ".zshrc").read_text()


@pytest.fixture(scope="module")
def bash_init() -> str:
    return (_SHELL_INIT_DIR / "bash" / "init.sh").read_text()


def test_zsh_zshenv_sources_user_zshenv(zsh_zshenv: str):
    """ZDOTDIR redirect makes zsh skip the user's `~/.zshenv` — our
    .zshenv must source it explicitly so the user's PATH / env / fpath
    setup lands before .zshrc runs."""
    assert 'source "$HOME/.zshenv"' in zsh_zshenv


def test_zsh_zshrc_sources_user_zshrc(zsh_zshrc: str):
    """Same as .zshenv but for interactive rcfile — user's plugins,
    prompt, aliases, completions must load."""
    assert 'source "$HOME/.zshrc"' in zsh_zshrc


def test_zsh_zshrc_unsets_zdotdir(zsh_zshrc: str):
    """Child shells (nested zsh, scripts that re-exec) must see normal
    rcfile lookup paths. Our auto-injection is one-shot per IDE-spawned
    terminal — `unset ZDOTDIR` restores standard behavior for descendants."""
    assert "unset ZDOTDIR" in zsh_zshrc


def test_zsh_zshrc_defines_osc7_emitter(zsh_zshrc: str):
    """The function `symmetria_osc7` must be defined, and must emit an
    OSC 7 sequence (ESC ] 7 ; file://<host>/<path> ESC \\)."""
    assert "symmetria_osc7()" in zsh_zshrc
    # OSC 7 with ST terminator — emitter prints printf-format `\\e]7;file://...\\e\\\\`.
    assert r"\e]7;file://" in zsh_zshrc
    # ST terminator (xterm spec) — chosen over BEL per the script's comment.
    assert r"\e\\" in zsh_zshrc


def test_zsh_zshrc_appends_chpwd_hook(zsh_zshrc: str):
    """The hook MUST append to `chpwd_functions` (not overwrite), so any
    user hooks already configured keep firing. The `+=( )` operator is
    zsh's standard array-append syntax."""
    assert "chpwd_functions+=(symmetria_osc7)" in zsh_zshrc


def test_zsh_zshrc_fires_emitter_at_startup(zsh_zshrc: str):
    """`chpwd_functions` only fires on directory CHANGE — not on shell
    startup. The init must invoke the emitter once explicitly so the IDE
    knows the initial cwd (otherwise the file tree would lag until the
    user issues their first `cd`)."""
    # The function call appears at least once OUTSIDE its own definition.
    # Count occurrences — should be ≥2: one inside the function, one bare call.
    assert (
        zsh_zshrc.count("symmetria_osc7") >= 3
    )  # def + chpwd_functions entry + bare call


def test_bash_init_sources_login_files(bash_init: str):
    """bash spawned with --rcfile (no -l) skips login files. Our init.sh
    must source `~/.bash_profile` / `~/.bash_login` / `~/.profile` so
    the user's login-shell setup is preserved."""
    assert ".bash_profile" in bash_init
    assert ".bash_login" in bash_init
    assert ".profile" in bash_init


def test_bash_init_sources_user_bashrc(bash_init: str):
    """User's interactive rcfile must load."""
    assert 'source "$HOME/.bashrc"' in bash_init


def test_bash_init_defines_osc7_emitter(bash_init: str):
    """Same OSC 7 wire format as the zsh variant."""
    assert "symmetria_osc7()" in bash_init
    assert r"\e]7;file://" in bash_init


def test_bash_init_appends_prompt_command(bash_init: str):
    """PROMPT_COMMAND extension MUST append (not overwrite), so any user
    PROMPT_COMMAND keeps running. The init checks if PROMPT_COMMAND is
    unset, sets it; otherwise appends with `;` separator."""
    assert 'PROMPT_COMMAND="symmetria_osc7"' in bash_init
    assert "; symmetria_osc7" in bash_init


def test_bash_init_idempotent_re_source(bash_init: str):
    """Sourcing the init multiple times (nested bash) must NOT add the
    hook twice — would result in two OSC 7 emits per prompt, doubling
    the cwd-update traffic."""
    # The idempotency check looks for the substring `;symmetria_osc7;`
    # in PROMPT_COMMAND before appending.
    assert "symmetria_osc7" in bash_init  # already covered
    assert "symmetria_osc7;" in bash_init or ";symmetria_osc7;" in bash_init


# ---------------------------------------------------------------------------
# _shell_launch_args — per-shell argv + env injection
# ---------------------------------------------------------------------------


def test_zsh_launch_uses_zdotdir():
    """zsh gets ZDOTDIR=runtime/symmetria-shell/zsh and `-l` (login)."""
    argv, env = _shell_launch_args("/usr/bin/zsh")
    assert argv == ["/usr/bin/zsh", "-l"]
    assert "ZDOTDIR" in env
    expected_zdotdir = str(_SHELL_INIT_DIR / "zsh")
    assert env["ZDOTDIR"] == expected_zdotdir


def test_zsh_launch_handles_non_standard_path():
    """Shell detection is by basename, NOT exact path. A user with zsh
    installed at /opt/homebrew/bin/zsh or /run/current-system/sw/bin/zsh
    (NixOS) must still get the OSC 7 injection."""
    argv, env = _shell_launch_args("/opt/homebrew/bin/zsh")
    assert argv == ["/opt/homebrew/bin/zsh", "-l"]
    assert "ZDOTDIR" in env


def test_bash_launch_uses_rcfile():
    """bash gets --rcfile pointing at runtime/symmetria-shell/bash/init.sh
    and `-i` (interactive). NOT `-l` — bash ignores --rcfile in login mode."""
    argv, env = _shell_launch_args("/bin/bash")
    assert argv[0] == "/bin/bash"
    assert "--rcfile" in argv
    rcfile_idx = argv.index("--rcfile")
    expected_rcfile = str(_SHELL_INIT_DIR / "bash" / "init.sh")
    assert argv[rcfile_idx + 1] == expected_rcfile
    assert "-i" in argv
    assert "-l" not in argv  # critical: --rcfile is ignored if -l is set
    assert env == {}  # bash injection is via argv, no env additions


def test_unknown_shell_falls_back_to_plain_login(caplog):
    """Unsupported shells (fish, nu, etc.) get a plain login spawn AND
    a warning log so the user knows OSC 7 won't fire until they manually
    install a chpwd hook in their shell's config."""
    with caplog.at_level(logging.WARNING, logger="symmetria_ide.terminal_backend"):
        argv, env = _shell_launch_args("/usr/bin/fish")
    assert argv == ["/usr/bin/fish", "-l"]
    assert env == {}
    # Warning must mention the shell name AND the OSC 7 fact.
    assert any("fish" in record.message for record in caplog.records)
    assert any("OSC 7" in record.message for record in caplog.records)


def test_default_shell_fallback_is_bash():
    """If $SHELL is unset, TerminalBackend.start falls back to /bin/bash.
    The launch args helper called with /bin/bash must produce a valid
    --rcfile invocation — not a warning-and-plain-login (that'd be silent
    OSC 7 breakage for any user without $SHELL set)."""
    argv, _ = _shell_launch_args("/bin/bash")
    assert "--rcfile" in argv  # not the fallback path
