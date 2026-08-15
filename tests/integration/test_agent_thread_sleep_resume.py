"""Phase-3 RPC-level specs for sleeping and resuming local agent threads."""

from __future__ import annotations

import importlib
import os
import time

import pytest

from symmetria_ide.app import AppController


class _Bridge:
    def __init__(self) -> None:
        self.removes: list[int] = []

    def notify_spawn(self, _instance: dict) -> None:
        pass

    def notify_remove(self, slot: int) -> None:
        self.removes.append(slot)

    def notify_focus(self, _slot: int) -> None:
        pass

    def notify_title(self, _slot: int, _title: str) -> None:
        pass

    def notify_activity(self, _slot: int, **_activity: object) -> None:
        pass

    def stop(self) -> None:
        pass


class _Timer:
    def __init__(self) -> None:
        self.stopped = False
        self.deleted = False

    def stop(self) -> None:
        self.stopped = True

    def deleteLater(self) -> None:  # noqa: N802 - Qt-compatible test double
        self.deleted = True


@pytest.fixture
def controller(monkeypatch):
    monkeypatch.delenv("SYMMETRIA_IDE_AGENT_PROMPT", raising=False)
    monkeypatch.delenv("SYMMETRIA_IDE_AGENT_VIEW", raising=False)
    monkeypatch.delenv("SYMMETRIA_IDE_AGENT_TMUX", raising=False)
    monkeypatch.setattr(
        "symmetria_ide.app.shutil.which", lambda executable: f"/bin/{executable}"
    )
    value = AppController()
    value._agent_bridge = _Bridge()
    yield value
    value.shutdown()


@pytest.fixture
def linked_project(tmp_path):
    main = tmp_path / "project"
    (main / ".git" / "worktrees" / "feature").mkdir(parents=True)
    worktree = tmp_path / "project-feature"
    worktree.mkdir()
    (worktree / ".git").write_text(
        f"gitdir: {main}/.git/worktrees/feature\n", encoding="utf-8"
    )
    return main, worktree


def _hook(slot: int, event: str, **fields) -> dict:
    return {
        "type": "hook",
        "agent_id": f"{os.getpid()}_{slot}",
        "hook_event_name": event,
        **fields,
    }


def _set_project(controller: AppController, root) -> None:
    controller._route_capsule({"id": "cwd", "label": "", "value": str(root)})


def _rows(controller: AppController):
    return controller.agentThreads.rows()


def _spawn_resumable(controller: AppController, session_id: str = "ses-phase3") -> int:
    controller.spawn_agent("resume", False, "opencode", session_id)
    return controller.focusedAgent


def test_sleep_direct_pty_releases_only_its_pane_without_tmux(
    controller: AppController, monkeypatch
) -> None:
    """Spec: direct-PTY sleep frees the slot and never shells out to tmux."""
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "symmetria_ide.app.subprocess.run",
        lambda argv, **_kwargs: calls.append(list(argv)),
    )
    slot = _spawn_resumable(controller)

    controller.sleep_agent(slot)

    assert controller.agentOrder == []
    assert controller.agentSlotActive == [False]
    assert calls == []


def test_sleep_tmux_kills_exact_recorded_session_before_releasing_pane(
    controller: AppController, monkeypatch, tmp_path
) -> None:
    """Spec: tmux sleep kills one exact session before Loader deactivation."""
    socket = str(tmp_path / "agent.sock")
    monkeypatch.setenv("SYMMETRIA_IDE_AGENT_TMUX", "1")
    monkeypatch.setenv("SYMMETRIA_IDE_TMUX_SOCKET", socket)
    events: list[object] = []
    monkeypatch.setattr(
        "symmetria_ide.app.subprocess.run",
        lambda argv, **_kwargs: events.append(list(argv)),
    )
    slot = _spawn_resumable(controller)
    controller.agent_spawn_argv(slot)
    recorded_name = controller._term_agents[slot]["tmux_session"]
    events.clear()  # discard spawn's orphan-session cleanup
    original_sync = controller._sync_pane_slots

    def sync_and_observe() -> None:
        events.append("release")
        original_sync()

    monkeypatch.setattr(controller, "_sync_pane_slots", sync_and_observe)

    controller.sleep_agent(slot)

    assert events == [
        ["tmux", "-S", socket, "kill-session", "-t", recorded_name],
        "release",
    ]
    assert controller.agentSlotActive == [False]


def test_sleep_keeps_dead_row_and_runs_the_complete_close_cleanup_funnel(
    controller: AppController, monkeypatch
) -> None:
    """Spec: sleep changes slot to zero while preserving every close side effect."""
    slot = _spawn_resumable(controller)
    controller._term_agent_activity[slot] = {
        "state": "working",
        "tool": "Editing",
        "agentType": "opencode",
    }
    controller._locally_captured_agents.add(slot)
    controller._agent_coord_attention[slot] = "waiting"
    timer = _Timer()
    controller._pending_interrupt_clears[slot] = timer
    calls: list[str] = []

    for name in (
        "_on_coord_agent_closed",
        "_cancel_pending_interrupt_clear",
        "_release_agent_browser_windows",
    ):
        original = getattr(controller, name)

        def observe(*args, _name=name, _original=original, **kwargs):
            calls.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(controller, name, observe)

    original_registry_sync = controller._sync_agent_registry

    def observe_registry() -> None:
        calls.append("_sync_agent_registry")
        original_registry_sync()

    monkeypatch.setattr(controller, "_sync_agent_registry", observe_registry)

    controller.sleep_agent(slot)

    assert [(row.session_id, row.slot) for row in _rows(controller)] == [
        ("ses-phase3", 0)
    ]
    assert slot not in controller._term_agents
    assert slot not in controller._term_agent_activity
    assert slot not in controller._locally_captured_agents
    assert slot not in controller._agent_coord_attention
    assert timer.stopped and timer.deleted
    assert calls.count("_on_coord_agent_closed") == 1
    assert calls.count("_cancel_pending_interrupt_clear") == 1
    assert calls.count("_release_agent_browser_windows") == 1
    assert calls.count("_sync_agent_registry") == 1
    assert controller._agent_bridge.removes == [slot]


def test_natural_exit_keeps_only_threads_with_a_resumable_session_id(
    controller: AppController,
) -> None:
    """Spec: a crash leaves a durable dead row, never an unsafe provisional row."""
    durable_slot = _spawn_resumable(controller, "ses-durable")
    controller._term_agents[durable_slot]["spawn_mono"] = (
        time.monotonic() - controller._AGENT_FAST_DEATH_SECONDS - 1
    )
    controller.on_agent_finished(durable_slot)
    assert [(row.thread_id, row.slot) for row in _rows(controller)] == [
        ("opencode:ses-durable", 0)
    ]

    controller.spawn_agent("fresh", False, "claude")
    provisional_slot = controller.focusedAgent
    controller._term_agents[provisional_slot]["spawn_mono"] = (
        time.monotonic() - controller._AGENT_FAST_DEATH_SECONDS - 1
    )
    controller.on_agent_finished(provisional_slot)

    assert [(row.thread_id, row.slot) for row in _rows(controller)] == [
        ("opencode:ses-durable", 0)
    ]


def test_thread_history_keeps_live_prefix_recent_dead_order_and_no_live_twin(
    controller: AppController, monkeypatch
) -> None:
    """Regression: extracting history cannot change the rail's composition rules."""
    thread_model = importlib.import_module("symmetria_ide.agent_thread_model")
    ended_at = iter((10, 20))
    monkeypatch.setattr(
        thread_model,
        "time",
        type("Clock", (), {"time": staticmethod(lambda: next(ended_at))}),
    )

    first = _spawn_resumable(controller, "dead-first")
    controller.sleep_agent(first)
    second = _spawn_resumable(controller, "dead-second")
    controller.sleep_agent(second)
    controller.spawn_agent("fresh", False, "claude")

    assert [(row.thread_id, row.slot) for row in _rows(controller)] == [
        (f"live:{controller.focusedAgent}", controller.focusedAgent),
        ("opencode:dead-second", 0),
        ("opencode:dead-first", 0),
    ]

    controller.resume_thread("opencode:dead-first")

    rows = _rows(controller)
    assert [row.thread_id for row in rows].count("opencode:dead-first") == 1
    assert next(row.slot for row in rows if row.thread_id == "opencode:dead-first") > 0
    assert [(row.thread_id, row.slot) for row in rows if row.slot == 0] == [
        ("opencode:dead-second", 0)
    ]


def test_work_root_is_persisted_on_identity_capture_move_and_before_sleep(
    controller: AppController, linked_project
) -> None:
    """Spec: all three live edges durably refresh the IDE-owned work root."""
    store = importlib.import_module("symmetria_ide.agent_thread_store")
    main, worktree = linked_project
    _set_project(controller, main)
    controller.spawn_agent("fresh", False, "claude")
    slot = controller.focusedAgent

    controller._on_agent_hook(
        _hook(slot, "UserPromptSubmit", session_id="claude-session", cwd=str(main))
    )
    assert store.load_work_root(str(main), "claude", "claude-session") == str(main)

    controller._on_agent_hook(
        _hook(
            slot,
            "PreToolUse",
            tool_name="Edit",
            tool_path=str(worktree / "src" / "app.py"),
            session_id="claude-session",
            cwd=str(main),
        )
    )
    assert store.load_work_root(str(main), "claude", "claude-session") == str(worktree)

    # Prove sleep performs its own final write rather than relying on the last
    # hook: deliberately stale the durable copy while the live record is right.
    assert store.save_work_root(str(main), "claude", "claude-session", str(main))
    controller.sleep_agent(slot)
    assert store.load_work_root(str(main), "claude", "claude-session") == str(worktree)


def test_resume_dead_thread_uses_exact_identity_and_recorded_worktree(
    controller: AppController, linked_project
) -> None:
    """Spec: resume bypasses fresh-agent redirect and starts in durable work_root."""
    main, worktree = linked_project
    _set_project(controller, main)
    slot = _spawn_resumable(controller, "ses-worktree")
    controller._on_agent_hook(
        _hook(
            slot,
            "PreToolUse",
            tool_name="Write",
            tool_path=str(worktree / "result.txt"),
            session_id="ses-worktree",
            cwd=str(main),
        )
    )
    controller.sleep_agent(slot)

    controller.resume_thread("opencode:ses-worktree")

    resumed_slot = controller.focusedAgent
    record = controller._term_agents[resumed_slot]
    assert record["spawn_type"] == "resume"
    assert record["harness"] == "opencode"
    assert record["session_id"] == "ses-worktree"
    assert record["cwd"] == str(worktree)
    assert controller.agent_start_dir(resumed_slot) == str(worktree)
    assert controller.agent_spawn_argv(resumed_slot)[-2:] == [
        "--session",
        "ses-worktree",
    ]


def test_resume_missing_worktree_falls_back_to_main_with_visible_notice(
    controller: AppController, linked_project
) -> None:
    """Spec: a vanished durable root cannot turn resume into a broken spawn."""
    main, worktree = linked_project
    _set_project(controller, main)
    slot = _spawn_resumable(controller, "ses-vanished")
    controller._on_agent_hook(
        _hook(
            slot,
            "PreToolUse",
            tool_name="Edit",
            tool_path=str(worktree / "gone.py"),
            session_id="ses-vanished",
            cwd=str(main),
        )
    )
    controller.sleep_agent(slot)
    vanished = worktree.with_name("vanished-worktree")
    worktree.rename(vanished)
    notices: list[tuple[str, str]] = []
    controller.locationAlert.connect(
        lambda title, detail: notices.append((title, detail))
    )

    controller.resume_thread("opencode:ses-vanished")

    resumed = controller._term_agents[controller.focusedAgent]
    assert resumed["cwd"] == str(main)
    assert notices and all(notice[0] and notice[1] for notice in notices)


def test_resume_already_live_thread_focuses_without_spawning_duplicate(
    controller: AppController,
) -> None:
    """Spec: durable identity is unique across the live pool."""
    existing = _spawn_resumable(controller, "ses-live")
    controller.spawn_agent("fresh", False, "claude")
    assert controller.focusedAgent != existing
    original_order = list(controller.agentOrder)

    controller.resume_thread("opencode:ses-live")

    assert controller.agentOrder == original_order
    assert controller.focusedAgent == existing
    assert (
        sum(
            record.get("session_id") == "ses-live"
            for record in controller._term_agents.values()
        )
        == 1
    )
