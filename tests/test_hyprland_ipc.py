"""Pure-logic tests for hyprland_ipc — no Qt, no compositor.

These cover the two things that silently broke an earlier attempt at pinning
browser windows: the rule SYNTAX (Hyprland 0.56 rejects the old
`class:^(x)$` matcher) and the success CHECK (`hyprctl` exits 0 even when it
ignored the keyword, so only the response body tells the truth).
"""

import json

import pytest

from symmetria_ide import hyprland_ipc


class TestRuleBuilding:
    def test_workspace_rule_uses_0_56_matcher_syntax(self):
        rule = hyprland_ipc.build_workspace_rule("6", "symmetria-browser-42")
        # `match:class ^(...)$` — NOT the pre-0.56 `class:^(...)$`, which is
        # rejected with "invalid field class:^(x)$: missing a value".
        assert rule == "workspace 6 silent, match:class ^(symmetria-browser-42)$"

    def test_workspace_rule_is_silent(self):
        """`silent` is what keeps an agent's browser from stealing focus."""
        assert " silent," in hyprland_ipc.build_workspace_rule("3", "cls")

    def test_unset_rule_targets_the_same_class(self):
        assert hyprland_ipc.build_unset_rule("cls") == (
            "workspace unset, match:class ^(cls)$"
        )

    @pytest.mark.parametrize(
        ("ws_id", "ws_name", "expected"),
        [
            (6, "6", "6"),
            (6, "", "6"),
            (3, "scratch", "name:scratch"),
            (-99, "special:magic", "special:magic"),
            (-1, "-1", "-1"),
        ],
        ids=["numbered", "no-name", "named", "special", "negative-id"],
    )
    def test_workspace_target_spelling(self, ws_id, ws_name, expected):
        """Hyprland's three workspace spellings are not interchangeable."""
        assert hyprland_ipc.workspace_rule_target(ws_id, ws_name) == expected


class TestClientParsing:
    def _clients(self, pid, ws_id=6, ws_name="6", address="0x5581ABCD"):
        return json.dumps(
            [
                {"pid": 999, "address": "0xdead", "workspace": {"id": 1, "name": "1"}},
                {
                    "pid": pid,
                    "address": address,
                    "workspace": {"id": ws_id, "name": ws_name},
                },
            ]
        )

    def test_finds_our_window(self):
        window = hyprland_ipc.parse_own_window(self._clients(1234), 1234)
        assert window == {
            "address": "5581abcd",
            "workspace_id": 6,
            "workspace_name": "6",
        }

    def test_address_is_normalized_for_event_comparison(self):
        """clients -j reports `0x…`; the event socket emits bare hex."""
        window = hyprland_ipc.parse_own_window(self._clients(1, address="0xABC"), 1)
        assert window["address"] == hyprland_ipc.normalize_address("abc")

    def test_absent_pid_is_none_not_error(self):
        assert hyprland_ipc.parse_own_window(self._clients(1234), 4321) is None

    @pytest.mark.parametrize(
        "payload", ["", "not json", "{}", "null", "[3]"], ids=lambda p: repr(p)[:12]
    )
    def test_unusable_payloads_degrade_to_none(self, payload):
        """No pinning is an acceptable outcome; a raised exception is not."""
        assert hyprland_ipc.parse_own_window(payload, 1) is None


class TestEventParsing:
    def test_splits_name_and_args(self):
        assert hyprland_ipc.parse_event_line("movewindowv2>>5581abc,3,3") == (
            "movewindowv2",
            ["5581abc", "3", "3"],
        )

    def test_event_without_payload(self):
        assert hyprland_ipc.parse_event_line("configreloaded>>") == (
            "configreloaded",
            [],
        )

    def test_non_event_line_is_inert(self):
        assert hyprland_ipc.parse_event_line("garbage") == ("", [])


class TestKeywordAcceptance:
    """apply_keyword must trust the RESPONSE BODY, never the exit code."""

    @pytest.fixture
    def hyprland(self, monkeypatch):
        monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "test_sig")

    def _stub_hyprctl(self, monkeypatch, stdout):
        monkeypatch.setattr(hyprland_ipc, "_run_hyprctl", lambda args: stdout)

    def test_ok_response_is_success(self, hyprland, monkeypatch):
        self._stub_hyprctl(monkeypatch, "ok\n")
        assert hyprland_ipc.apply_keyword("workspace 6 silent, match:class ^(x)$")

    def test_deprecation_notice_is_failure(self, hyprland, monkeypatch):
        """The exact regression: hyprctl exits 0 and applies nothing."""
        self._stub_hyprctl(
            monkeypatch, "windowrulev2 is deprecated. Correct syntax can be found..."
        )
        assert not hyprland_ipc.apply_keyword("anything")

    def test_invalid_field_is_failure(self, hyprland, monkeypatch):
        self._stub_hyprctl(monkeypatch, "invalid field class:^(x)$: missing a value")
        assert not hyprland_ipc.apply_keyword("workspace 6 silent, class:^(x)$")

    def test_subprocess_failure_is_failure(self, hyprland, monkeypatch):
        self._stub_hyprctl(monkeypatch, None)
        assert not hyprland_ipc.apply_keyword("anything")

    def test_no_hyprland_is_a_quiet_no_op(self, monkeypatch):
        monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
        called = []
        monkeypatch.setattr(
            hyprland_ipc, "_run_hyprctl", lambda args: called.append(args)
        )
        assert not hyprland_ipc.apply_keyword("anything")
        assert called == [], "must not shell out when Hyprland isn't running"
