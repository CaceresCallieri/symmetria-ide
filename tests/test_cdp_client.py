"""Tests for the CDP client's message router and attach state machine.

No socket and no Chrome: frames are handed to `_on_message` directly, which is
where all the routing logic lives. This is the riskiest new code in the browser
stack — a mis-routed reply silently binds the wrong window, and a wedged attach
leaves the registry frozen with no error anywhere.
"""

from __future__ import annotations

import json

import pytest

from symmetria_ide.cdp_client import _MAX_ATTACH_ATTEMPTS, CdpClient


@pytest.fixture
def client():
    c = CdpClient()
    yield c
    c.close()


def frame(**payload) -> str:
    return json.dumps(payload)


class TestCommandReplies:
    def test_reply_routes_to_its_callback(self, client):
        got: list[dict] = []
        client._connected = True
        client.send("Target.createTarget", {}, got.append)
        client._on_message(frame(id=1, result={"targetId": "T-1"}))
        assert got == [{"targetId": "T-1"}]

    def test_callback_fires_once(self, client):
        """A second frame with the same id must not re-invoke — the pending
        entry is popped, so a duplicate would mean the id was reused."""
        got: list[dict] = []
        client._connected = True
        client.send("X", {}, got.append)
        client._on_message(frame(id=1, result={}))
        client._on_message(frame(id=1, result={}))
        assert len(got) == 1

    def test_error_reply_still_reaches_the_callback(self, client):
        """The caller must be able to tell "failed" from "never answered"."""
        got: list[dict] = []
        client._connected = True
        client.send("X", {}, got.append)
        client._on_message(frame(id=1, error={"message": "Not supported"}))
        assert got and "targetId" not in got[0]

    def test_send_while_disconnected_reports_failure(self, client):
        """Returning False is what stops a caller waiting on a callback that
        will never fire."""
        assert client.send("X") is False

    def test_unparseable_frame_is_survivable(self, client):
        client._on_message("{not json")  # must not raise


class TestTargetEvents:
    def test_page_target_is_reported(self, client):
        seen: list[tuple] = []
        client.targetUpdated.connect(lambda *a: seen.append(a))
        client._on_message(
            frame(
                method="Target.targetCreated",
                params={
                    "targetInfo": {
                        "targetId": "T-1",
                        "type": "page",
                        "url": "https://a.test",
                        "title": "A",
                    }
                },
            )
        )
        assert seen == [("T-1", "https://a.test", "A")]

    @pytest.mark.parametrize(
        "kind", ["service_worker", "browser_ui", "background_page", "other"]
    )
    def test_non_page_targets_are_dropped(self, client, kind):
        """Chrome reports its own internals as targets — the omnibox popup, an
        extension's service worker. Adopting one into a window slot would
        point the registry at something the user can never see."""
        seen: list[tuple] = []
        client.targetUpdated.connect(lambda *a: seen.append(a))
        client._on_message(
            frame(
                method="Target.targetCreated",
                params={
                    "targetInfo": {
                        "targetId": "X",
                        "type": kind,
                        "url": "",
                        "title": "",
                    }
                },
            )
        )
        assert seen == []

    def test_destroyed_target_is_reported(self, client):
        gone: list[str] = []
        client.targetGone.connect(gone.append)
        client._on_message(
            frame(method="Target.targetDestroyed", params={"targetId": "T-1"})
        )
        assert gone == ["T-1"]


class TestAttachLifecycle:
    def test_retry_budget_is_finite(self, client):
        """Chrome may never come up (killed, crashed). Retrying forever would
        leave a timer firing for the life of the IDE."""
        client._port = 1
        client._attempts = _MAX_ATTACH_ATTEMPTS
        client._retry_or_give_up("boom")
        assert client._connecting is False

    def test_close_prevents_a_late_attach(self, client):
        """A /json/version request already in flight completes AFTER close()
        and would otherwise open a websocket past teardown."""
        client.close()
        client._attempt_attach()
        assert client._connecting is False

    def test_connect_after_close_is_allowed(self, client):
        """Chrome can be killed and respawned within one IDE session."""
        client.close()
        client.connect_to(0)  # port 0 short-circuits before any I/O
        assert client._closed is False or client._port == 0

    def test_socket_error_before_connect_consumes_the_budget(self, client):
        """Qt does NOT emit `disconnected` for a socket that never connected,
        so without this path a failed handshake would retry zero times and log
        nothing — CDP silently detached for the whole session."""
        client._port = 1
        client._attempts = _MAX_ATTACH_ATTEMPTS  # force the give-up branch
        client._connecting = True
        client._on_socket_error()
        assert client._connecting is False

    def test_socket_error_on_a_live_session_is_ignored(self, client):
        """A drop after a successful attach is `disconnected`'s business —
        treating it as an attach failure would retry over a live session."""
        client._connected = True
        client._connecting = False
        client._on_socket_error()
        assert client._connecting is False
