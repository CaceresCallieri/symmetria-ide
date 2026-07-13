"""remote_location pure helpers — session naming, list parsing, remote argv.

No Qt objects, no ssh, no threads: everything here is pure string/argv math
(the RemoteContext state machine is covered in test_app_controller_location).
The argv assertions round-trip through shlex.split because ssh flattens the
remote command through the remote shell — what matters is the token list the
REMOTE side reconstructs, not the local string.
"""

from __future__ import annotations

import shlex
import time

from symmetria_ide import ssh_runner
from symmetria_ide.remote_location import (
    TMUX_LIST_FORMAT,
    parse_tmux_sessions,
    remote_agent_argv,
    vps_tmux_session_name,
)
from symmetria_ide.server_registry import RemoteServer

SERVER = RemoteServer(name="vps", host="203.0.113.7")
ROOT = "/opt/dev/repos/symmetria-ide"


def _record(spawn_type: str = "fresh", dangerous: bool = True, **extra) -> dict:
    record = {
        "spawn_type": spawn_type,
        "dangerous": dangerous,
        "harness": "claude",
        "tmux_session": "symmetria-ide-vps-2",
        "remote_root": ROOT,
    }
    record.update(extra)
    return record


def _remote_tokens(argv: list[str]) -> list[str]:
    """The token list the remote shell reconstructs from the ssh command.

    Strips (and asserts) the ``env LANG=C.UTF-8`` locale wrapper that
    ``remote_command_argv`` prefixes onto every remote command, so the
    argv-shape tests below stay about the tmux/claude command itself.
    """
    assert argv[-2] == "--"
    tokens = shlex.split(argv[-1])
    assert tokens[:2] == ["env", ssh_runner.REMOTE_LOCALE_ENV]
    return tokens[2:]


# ---------------------------------------------------------------------------
# vps_tmux_session_name
# ---------------------------------------------------------------------------


def test_session_name_is_repo_vps_slot():
    assert (
        vps_tmux_session_name("/home/jc/projects/symmetria-ide", 2)
        == "symmetria-ide-vps-2"
    )


def test_session_name_tolerates_trailing_slash():
    assert vps_tmux_session_name("/home/jc/projects/demo/", 1) == "demo-vps-1"


# ---------------------------------------------------------------------------
# parse_tmux_sessions
# ---------------------------------------------------------------------------


def test_parse_filters_to_remote_root():
    stdout = (
        f"vigilia-abcd\t100\t/opt/dev/repos/vigilia\n"
        f"symmetria-ide-x\t200\t{ROOT}\n"
        f"symmetria-ide-y\t300\t{ROOT}/src\n"
        f"scratch\t400\t/opt/dev/scratch\n"
    )
    rows = parse_tmux_sessions(stdout, ROOT)
    assert [row["name"] for row in rows] == ["symmetria-ide-y", "symmetria-ide-x"]


def test_parse_newest_first():
    stdout = f"old\t100\t{ROOT}\nnew\t900\t{ROOT}\nmid\t500\t{ROOT}\n"
    rows = parse_tmux_sessions(stdout, ROOT)
    assert [row["name"] for row in rows] == ["new", "mid", "old"]


def test_parse_prefix_collision_is_not_a_match():
    """/opt/dev/repos/symmetria-ide-extras must not match .../symmetria-ide."""
    stdout = f"other\t100\t{ROOT}-extras\n"
    assert parse_tmux_sessions(stdout, ROOT) == []


def test_parse_skips_malformed_lines():
    stdout = f"just-a-name\nname-only\t123\n\tmissing-name\t{ROOT}\nok\t5\t{ROOT}\n"
    rows = parse_tmux_sessions(stdout, ROOT)
    assert [row["name"] for row in rows] == ["ok"]


def test_parse_when_is_local_time_string():
    created = 1700000000
    stdout = f"s\t{created}\t{ROOT}\n"
    (row,) = parse_tmux_sessions(stdout, ROOT)
    assert row["when"] == time.strftime("%Y-%m-%d %H:%M", time.localtime(created))


def test_parse_bad_epoch_degrades_to_empty_when():
    stdout = f"s\tnot-a-number\t{ROOT}\n"
    (row,) = parse_tmux_sessions(stdout, ROOT)
    assert row["when"] == ""


def test_list_format_carries_the_three_fields():
    # The parse above assumes this exact field order — pin it.
    assert TMUX_LIST_FORMAT.split("\t") == [
        "#{session_name}",
        "#{session_created}",
        "#{pane_current_path}",
    ]


# ---------------------------------------------------------------------------
# remote_agent_argv
# ---------------------------------------------------------------------------


def test_fresh_dangerous_argv_end_to_end():
    argv = remote_agent_argv(SERVER, _record())
    assert argv[0] == "ssh"
    assert argv[1] == "-t"  # interactive pane
    assert argv[-3] == "dev@203.0.113.7"
    tokens = _remote_tokens(argv)
    assert tokens == [
        "tmux",
        "-S",
        SERVER.tmux_socket,
        "new-session",
        "-A",
        "-c",
        ROOT,
        "-s",
        "symmetria-ide-vps-2",
        "claude",
        "--dangerously-skip-permissions",
    ]


def test_safe_spawn_omits_dangerous_flag():
    tokens = _remote_tokens(remote_agent_argv(SERVER, _record(dangerous=False)))
    assert "--dangerously-skip-permissions" not in tokens
    assert tokens[-1] == "claude"


def test_resume_and_continue_carry_their_flags():
    resume = _remote_tokens(remote_agent_argv(SERVER, _record("resume")))
    assert resume[-1] == "-r"
    cont = _remote_tokens(remote_agent_argv(SERVER, _record("continue")))
    assert cont[-1] == "-c"


def test_model_and_effort_ride_the_inner_argv():
    tokens = _remote_tokens(
        remote_agent_argv(SERVER, _record(), model="fable", effort="high")
    )
    assert tokens[-4:] == ["--model", "fable", "--effort", "high"]


def test_invalid_effort_is_skipped_model_survives():
    tokens = _remote_tokens(
        remote_agent_argv(SERVER, _record(), model="fable", effort="turbo")
    )
    assert "--effort" not in tokens
    assert tokens[-2:] == ["--model", "fable"]


def test_attach_has_no_inner_command():
    """attach → tmux new -A with NO trailing command = pure attach-or-shell."""
    record = _record("attach", tmux_session="phone-started")
    tokens = _remote_tokens(remote_agent_argv(SERVER, record))
    assert tokens[-2:] == ["-s", "phone-started"]
    assert "claude" not in tokens


def test_no_env_wrapper_or_local_machinery():
    """The local spawn machinery must NOT leak into the remote argv.

    (_remote_tokens has already stripped the deliberate locale wrapper;
    the "env" being guarded against here is the local SYMMETRIA_AGENT_ID
    one — Vigilia manages the server agent env, not us.)
    """
    tokens = _remote_tokens(remote_agent_argv(SERVER, _record()))
    assert "env" not in tokens
    assert not any(token.startswith("SYMMETRIA_") for token in tokens)
    assert "--settings" not in tokens
    assert "--mcp-config" not in tokens
    assert "-f" not in tokens  # no local tmux conf on the VPS server
