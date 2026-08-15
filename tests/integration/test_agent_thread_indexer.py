"""Phase-4 integration specs for the cancellable Claude thread indexer."""

from __future__ import annotations

import importlib
import inspect
import threading
import time


def _module():
    # Kept inside tests so the pre-Phase-4 tree remains collectable and each
    # missing behavior produces its own observed red verdict.
    return importlib.import_module("symmetria_ide.agent_thread_indexer")


def _threads(indexer: object) -> list[threading.Thread]:
    return [
        value for value in vars(indexer).values() if isinstance(value, threading.Thread)
    ]


def _events(indexer: object) -> list[threading.Event]:
    event_type = type(threading.Event())
    return [value for value in vars(indexer).values() if isinstance(value, event_type)]


class _BlockingReader:
    harness = "claude"

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def read(self, project_root: str, stop_event: threading.Event) -> list[dict]:
        self.started.set()
        self.release.wait(timeout=2)
        return [
            {
                "harness": "claude",
                "session_id": "worker-result",
                "project_root": project_root,
                "stop_event_was_set": stop_event.is_set(),
            }
        ]


def test_indexer_worker_is_daemon_cancellable_gc_safe_and_explicitly_queued(
    monkeypatch,
) -> None:
    """Spec: the worker follows every required cross-thread lifecycle rule."""
    module = _module()
    reader = _BlockingReader()
    emitted = threading.Event()
    emissions: list[tuple[object, ...]] = []

    def observe_emit(_signal, *payload: object) -> None:
        emissions.append(payload)
        emitted.set()

    monkeypatch.setattr(module, "emit_gc_safe", observe_emit)
    indexer = module.AgentThreadIndexer(readers=[reader])
    try:
        indexer.start("/project")
        assert reader.started.wait(timeout=1), "the index worker never started"
        workers = _threads(indexer)
        assert workers and all(worker.daemon for worker in workers)
        stop_events = _events(indexer)
        assert stop_events, "the indexer owns no threading.Event shutdown token"

        reader.release.set()
        assert emitted.wait(timeout=1), "worker result bypassed emit_gc_safe"
        assert emissions

        source = inspect.getsource(module.AgentThreadIndexer)
        assert "Qt.ConnectionType.QueuedConnection" in source
        assert ".connect(" in source
    finally:
        reader.release.set()
        indexer.stop()

    assert any(event.is_set() for event in stop_events)


class _GenerationReader:
    harness = "claude"

    def __init__(self) -> None:
        self.old_started = threading.Event()

    def read(self, project_root: str, stop_event: threading.Event) -> list[dict]:
        session_id = "stale-old-project" if project_root == "/old" else "fresh-project"
        if project_root == "/old":
            self.old_started.set()
            # A superseding request must cancel this generation. Deliberately
            # return a row anyway to prove the receiver checks the token rather
            # than trusting cooperative cancellation alone.
            stop_event.wait(timeout=2)
        return [
            {
                "harness": "claude",
                "session_id": session_id,
                "project_root": project_root,
            }
        ]


def test_superseded_project_generation_is_discarded_before_merge(
    monkeypatch,
) -> None:
    """Spec: late rows from an old project can never reach the merge boundary."""
    module = _module()
    reader = _GenerationReader()
    emissions: list[tuple[object, ...]] = []
    fresh_seen = threading.Event()

    def observe_emit(_signal, *payload: object) -> None:
        emissions.append(payload)
        if "fresh-project" in repr(payload):
            fresh_seen.set()

    monkeypatch.setattr(module, "emit_gc_safe", observe_emit)
    indexer = module.AgentThreadIndexer(readers=[reader])
    try:
        indexer.start("/old")
        assert reader.old_started.wait(timeout=1)
        indexer.start("/new")
        assert fresh_seen.wait(timeout=2), (
            "the current project result was not published"
        )
        # Let any incorrectly published stale result become observable without
        # pumping the session-global Qt event queue.
        deadline = time.monotonic() + 0.2
        while time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        indexer.stop()

    published = repr(emissions)
    assert "fresh-project" in published
    assert "stale-old-project" not in published
