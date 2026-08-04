"""Keeps the shared usage file fresh — the half the event sources can't cover.

Claude publishes its usage for free on every agent turn (the status-line tap →
`AppController._on_status_line`), which is both instant and zero-cost. What it
cannot do is keep the number honest while the IDE sits idle: no turn, no
observation, and the panel slowly drifts into lying. Codex has no event path at
all short of holding a `codex app-server` process open indefinitely.

So this poller fills the gaps rather than replacing the events:

  * it runs a cheap staleness check every `interval_ms` (a tmpfs read),
  * it fetches ONLY the providers whose stored observation is older than the
    TTL — so a busy Claude session is never polled, because its tap already
    beat the TTL,
  * and it takes the store's non-blocking `poll_lock` first, so with N IDE
    windows open exactly one of them performs the round.

Threading: the fetches are blocking network/subprocess work (Codex alone costs
~2s), so they run on a single-worker pool, never the GUI thread. The worker
publishes straight into the shared file — the GUI side finds out through the
same `QFileSystemWatcher` a peer window would, so there is exactly one update
path instead of two. The only thing marshalled back is the per-provider error
state, which is view-only and lives nowhere on disk (an errored snapshot must
never replace good numbers — see `usage_providers`' error contract).
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot

from .agent_bridge import emit_gc_safe
from .usage_providers import PROVIDER_IDS, fetch
from .usage_store import UsageStore, poll_lock

log = logging.getLogger(__name__)

__all__ = ["UsagePoller"]

# How often we ask "is anything stale?". Cheap (one tmpfs read), so the cadence
# is about responsiveness after a wake/resume, not about load.
_TICK_MS = 60_000
# How old an observation may get before we refresh it. Rate limits move on the
# scale of hours; five minutes is well inside "honest" while keeping the Codex
# subprocess spawn rare.
_TTL_SECONDS = 300
_NS_PER_SECOND = 1_000_000_000


class UsagePoller(QObject):
    """Polls stale providers under a cross-process lock; owns no display state."""

    # Worker → GUI (queued): {provider_id: reason} for every provider the round
    # ATTEMPTED — reason "" means it succeeded. Providers the round skipped are
    # absent, which is what lets the GUI side leave their state alone.
    _resultsReady = Signal(dict)

    # The per-provider error map changed; the panel re-reads `errors`.
    errorsChanged = Signal()

    def __init__(
        self,
        store: UsageStore,
        parent: QObject | None = None,
        *,
        tick_ms: int = _TICK_MS,
        ttl_seconds: int = _TTL_SECONDS,
    ):
        super().__init__(parent)
        self._store = store
        self._ttl_ns = ttl_seconds * _NS_PER_SECOND
        self._errors: dict[str, str] = {}
        self._inflight = False
        self._stop_event = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="usage-poll")
        self._timer = QTimer(self)
        self._timer.setInterval(tick_ms)
        self._timer.timeout.connect(self._tick)
        # queued: _resultsReady is emitted from the pool thread, and the slot
        # mutates state QML binds against (project-standards §4 P2).
        self._resultsReady.connect(self._on_results, Qt.ConnectionType.QueuedConnection)

    @property
    def errors(self) -> dict[str, str]:
        """provider id → failure reason for the last round that touched it."""
        return dict(self._errors)

    def start(self) -> None:
        """Begin ticking, and check immediately (don't wait a full interval).

        The immediate check is what makes a freshly-launched IDE show usage
        within a second or two when the shared file is cold or stale.
        """
        self._timer.start()
        self._tick()

    def stop(self) -> None:
        self._stop_event.set()
        self._timer.stop()
        # wait=False: shutdown runs on the GUI thread during teardown and an
        # in-flight Codex fetch can take seconds. The worker checks the stop
        # event between providers and publishes nothing further.
        self._pool.shutdown(wait=False, cancel_futures=True)

    # -- scheduling ------------------------------------------------------

    @Slot()
    def _tick(self) -> None:
        if self._inflight or self._stop_event.is_set():
            return
        stale = self._stale_providers()
        if not stale:
            return
        self._inflight = True
        self._pool.submit(self._run_round, stale)

    def _stale_providers(self) -> tuple[str, ...]:
        """Providers whose stored observation is older than the TTL.

        Reads the SHARED file, not a local cache, so an observation published by
        a peer window (or by this window's own status-line tap) counts as fresh
        and suppresses the fetch. That is the whole reason a busy Claude session
        costs zero HTTP requests.
        """
        current = self._store.read_current()
        cutoff = time.time_ns() - self._ttl_ns
        return tuple(
            pid
            for pid in PROVIDER_IDS
            if (usage := current.get(pid)) is None or usage.observed_at_ns < cutoff
        )

    # -- worker ----------------------------------------------------------

    def _run_round(self, providers: tuple[str, ...]) -> None:
        """Fetch + publish each stale provider. Runs on the pool thread."""
        errors: dict[str, str] = {}
        try:
            with poll_lock(self._store.path) as acquired:
                if not acquired:
                    # Another window owns this round; its result reaches us
                    # through the file watcher. Reporting no errors here is
                    # deliberate — we learned nothing, good or bad.
                    return
                for provider in providers:
                    if self._stop_event.is_set():
                        return
                    usage = fetch(provider)
                    errors[provider] = usage.error  # "" on success
                    if not usage.error:
                        self._store.publish(usage)
        except Exception:
            # A poll failure must never take down the IDE; the panel keeps
            # showing the last known values with their age.
            log.exception("usage poll round failed")
        finally:
            if not self._stop_event.is_set():
                emit_gc_safe(self._resultsReady, errors)

    # -- GUI side --------------------------------------------------------

    @Slot(dict)
    def _on_results(self, errors: dict) -> None:
        self._inflight = False
        # Only providers the round ATTEMPTED are touched. A round that skipped
        # Claude (its status-line tap kept it fresh) must not clear a real Codex
        # error, and a round that lost the poll lock reports nothing at all.
        changed = False
        for provider, reason in errors.items():
            previous = self._errors.get(provider, "")
            if reason == previous:
                continue
            if reason:
                self._errors[provider] = reason
            else:
                self._errors.pop(provider, None)
            changed = True
        if changed:
            self.errorsChanged.emit()
