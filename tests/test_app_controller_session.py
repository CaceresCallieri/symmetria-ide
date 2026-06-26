"""Session save/restore + teardown-funnel tests for AppController.

Hermetic, mirroring test_app_controller_term_agents.py: a real AppController
with a fake bridge, no QML, no subprocesses, no nvim socket. session_store is
redirected to a tmp XDG_STATE_HOME. displayedRoot is controlled via `_cwd`
(unanchored), which is the manifest key.
"""

from __future__ import annotations

import os

import pytest

from symmetria_ide import session_store
from symmetria_ide.app import AppController


class FakeBridge:
    """Minimal AgentBridgeClient publish surface (spawn/focus/etc. captured)."""

    def __init__(self) -> None:
        self.spawns: list[dict] = []

    def notify_spawn(self, instance: dict) -> None:
        self.spawns.append(instance)

    def notify_remove(self, slot: int) -> None:
        pass

    def notify_focus(self, slot: int) -> None:
        pass

    def notify_title(self, slot: int, title: str) -> None:
        pass

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


@pytest.fixture
def controller(monkeypatch, tmp_path):
    # session_store writes under XDG_STATE_HOME/symmetria-ide/sessions/.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("SYMMETRIA_IDE_AGENT_PROMPT", raising=False)
    monkeypatch.delenv("SYMMETRIA_IDE_AGENT_VIEW", raising=False)
    # spawn_agent guards on the harness CLI being installed.
    monkeypatch.setattr(
        "symmetria_ide.app.shutil.which", lambda _name: "/usr/bin/" + _name
    )
    c = AppController()
    c._agent_bridge = FakeBridge()
    # A stable, unanchored project root → displayedRoot == this path.
    c._cwd = str(tmp_path / "proj")
    return c


def _agent(slot, harness="claude", session_id="", spawn_type="fresh", title=""):
    return {
        "harness": harness,
        "spawn_type": spawn_type,
        "session_id": session_id,
        "dangerous": True,
        "title": title,
        "cwd": "/x",
        "spawned_at": 0,
    }


# --- save_session ----------------------------------------------------------


def test_save_session_roundtrips_agents(controller):
    c = controller
    c._term_agents[1] = _agent(1, session_id="sess-1", title="fix bug")
    c._term_agents[2] = _agent(2, harness="opencode", session_id="ses_x")
    c._agent_order = [1, 2]
    c._focused_term_agent = 2
    c._central_surface = "agent"

    c.save_session()

    data = session_store.load(c.displayedRoot)
    assert data is not None
    assert [a["session_id"] for a in data["agents"]] == ["sess-1", "ses_x"]
    assert [a["harness"] for a in data["agents"]] == ["claude", "opencode"]
    # Focused recorded as a 1-based DISPLAY position (slot 2 is position 2).
    assert data["focused_agent"] == 2
    assert data["central_surface"] == "agent"


def test_save_session_deletes_when_empty(controller):
    c = controller
    # Pre-seed a stale manifest, then save with an empty workspace.
    session_store.save(c.displayedRoot, {"agents": [{"harness": "claude"}]})
    assert session_store.exists(c.displayedRoot)

    c.save_session()  # no agents / browsers / editor files

    assert not session_store.exists(c.displayedRoot)


def test_save_session_skips_blank_browser_windows(controller):
    c = controller
    c._browser_tabs = {
        1: {"url": "about:blank", "title": ""},
        2: {"url": "https://example.com", "title": "Ex"},
    }
    c._browser_order = [1, 2]
    c._focused_browser = 2

    c.save_session()

    data = session_store.load(c.displayedRoot)
    assert [b["url"] for b in data["browsers"]] == ["https://example.com"]
    # focused is the 1-based index into the FILTERED list (blank dropped).
    assert data["focused_browser"] == 1


# --- session_id backfill (the keystone) ------------------------------------


def test_bridge_snapshot_backfills_session_id(controller):
    c = controller
    c._term_agents[1] = _agent(1, session_id="")  # claude starts empty
    c._agent_order = [1]
    pid = os.getpid()

    c._on_bridge_snapshot(
        {
            "agents": [
                {
                    "id": f"{pid}_1",
                    "session_id": "live-sess",
                    "activity_state": "running",
                }
            ]
        }
    )

    assert c._term_agents[1]["session_id"] == "live-sess"


def test_bridge_snapshot_does_not_clear_known_session_id(controller):
    c = controller
    c._term_agents[1] = _agent(1, session_id="known")
    c._agent_order = [1]
    pid = os.getpid()

    # Idle snapshot drops session_id (the bridge pops the activity entry) —
    # the IDE must RETAIN the previously captured id.
    c._on_bridge_snapshot({"agents": [{"id": f"{pid}_1", "session_id": ""}]})

    assert c._term_agents[1]["session_id"] == "known"


# --- restore_session -------------------------------------------------------


def test_restore_session_resumes_with_id_and_fresh_without(controller):
    c = controller
    session_store.save(
        c.displayedRoot,
        {
            "central_surface": "agent",
            "focused_agent": 1,
            "agents": [
                {"harness": "claude", "session_id": "sess-A", "dangerous": True},
                {"harness": "claude", "session_id": "", "dangerous": False},
            ],
        },
    )

    c.restore_session()

    order = c._agent_order
    assert len(order) == 2
    first, second = c._term_agents[order[0]], c._term_agents[order[1]]
    # Captured id → resume that exact session.
    assert (first["spawn_type"], first["session_id"]) == ("resume", "sess-A")
    # No id → fresh respawn (can't re-home an unidentified conversation).
    assert second["spawn_type"] == "fresh"
    assert c._focused_term_agent == order[0]
    assert c._central_surface == "agent"


def test_restore_session_reopens_browser_windows(controller):
    c = controller
    session_store.save(
        c.displayedRoot,
        {"central_surface": "terminal", "browsers": [{"url": "https://example.com"}]},
    )

    c.restore_session()

    assert [t["url"] for t in c._browser_tabs.values()] == ["https://example.com"]


def test_restore_session_noop_without_saved_session(controller):
    c = controller
    c.restore_session()  # nothing saved
    assert c._agent_order == []


def test_saved_session_agents_lists_descriptors(controller):
    c = controller
    session_store.save(
        c.displayedRoot,
        {"agents": [{"harness": "claude", "title": "T", "session_id": "s"}]},
    )
    rows = c.saved_session_agents()
    assert rows == [{"harness": "claude", "title": "T", "session_id": "s"}]


# --- teardown funnel -------------------------------------------------------


def test_request_teardown_clean_close_opens_confirm(controller, monkeypatch):
    c = controller
    monkeypatch.setattr(c._backend, "query_buffers", lambda: [])
    seen = []
    c.cleanTeardownRequested.connect(lambda: seen.append("clean"))
    c.dirtyTeardownRequested.connect(lambda p: seen.append(("dirty", p)))

    c.request_teardown(False)

    assert seen == ["clean"]


def test_request_teardown_clean_reload_proceeds(controller, monkeypatch):
    c = controller
    monkeypatch.setattr(c._backend, "query_buffers", lambda: [])
    quit_calls = []
    monkeypatch.setattr(c, "authorize_and_quit", lambda: quit_calls.append(1))

    c.request_teardown(True)

    # Clean + reload proceeds immediately (no dialog) and arms the reload latch.
    assert quit_calls == [1]
    assert c.reload_requested is True


def test_request_teardown_dirty_opens_unsaved_dialog(controller, monkeypatch):
    c = controller
    monkeypatch.setattr(
        c._backend,
        "query_buffers",
        lambda: [
            {"path": "/p/a.py", "modified": True},
            {"path": "/p/b.py", "modified": False},
        ],
    )
    seen = []
    c.dirtyTeardownRequested.connect(lambda paths: seen.append(list(paths)))
    c.cleanTeardownRequested.connect(lambda: seen.append("clean"))

    c.request_teardown(False)

    assert seen == [["/p/a.py"]]  # only the modified buffer's path


def test_teardown_save_all_writes_then_proceeds(controller, monkeypatch):
    c = controller
    calls = []
    monkeypatch.setattr(c._backend, "save_all", lambda: calls.append("wall"))
    monkeypatch.setattr(c, "authorize_and_quit", lambda: calls.append("quit"))

    c.teardown_save_all()

    # wall MUST land before the quit (else qa! discards the saved edits).
    assert calls == ["wall", "quit"]
