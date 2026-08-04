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
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot

from .agent_bridge import emit_gc_safe
from .usage_providers import PROVIDER_IDS, fetch
from .usage_store import UsageStore, poll_lock, publish_if_newer, read

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

    # A user-requested refresh started or finished; the readout pulses while
    # it is true. Automatic rounds deliberately do NOT raise it — a status bar
    # that blinks every five minutes on its own reads as a glitch, not as work.
    refreshingChanged = Signal()

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
        self._refreshing = False
        self._refresh_pending = False
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

    @property
    def refreshing(self) -> bool:
        """True while a round the USER asked for is in flight."""
        return self._refreshing

    def start(self) -> None:
        """Begin ticking, and check immediately (don't wait a full interval).

        The immediate check is what makes a freshly-launched IDE show usage
        within a second or two when the shared file is cold or stale.

        ⚠ `SYMMETRIA_IDE_USAGE_POLL=0` disables polling entirely, and it is the
        TEST SUITE'S HARD OFF SWITCH (set for every test in conftest). Several
        tests call `AppController.start()`, which reaches here — without the
        switch each of those fires a real request against the user's Anthropic
        account and spawns a real `codex app-server`, from a suite that is
        supposed to touch nothing live. Manual `refresh()` is deliberately NOT
        gated: it only ever runs because someone clicked.
        """
        if os.environ.get("SYMMETRIA_IDE_USAGE_POLL") == "0":
            log.debug("usage poller disabled (SYMMETRIA_IDE_USAGE_POLL=0)")
            return
        self._timer.start()
        self._tick()

    def stop(self) -> None:
        """Terminal — the poller is NOT restartable after this.

        The stop event is never cleared and the pool is shut down for good, so a
        later `start()` / `refresh()` silently no-ops. That is correct for the
        one caller (`AppController.shutdown`); anything that ever needs a
        pause/resume must rebuild the pool and clear the event here rather than
        assume `start()` revives it.
        """
        self._stop_event.set()
        self._timer.stop()
        # wait=False: shutdown runs on the GUI thread during teardown and an
        # in-flight Codex fetch can take seconds. The worker checks the stop
        # event between providers and publishes nothing further.
        self._pool.shutdown(wait=False, cancel_futures=True)

    # -- scheduling ------------------------------------------------------

    @Slot()
    def refresh(self) -> None:
        """Poll EVERY provider now, TTL ignored — the user asked explicitly.

        Distinct from `_tick` in two ways. It targets all providers rather than
        only the stale ones (the point of asking is "prove these numbers are
        current", which a skipped-because-fresh provider does not do), and it
        raises `refreshing` so the readout can show the work.

        If a round is already in flight, this adopts it instead of queueing a
        second one: the pool has a single worker, so a queued round would run
        AFTER the current one and double the requests for no new information.

        ⚠ Losing the cross-process poll lock is a legitimate outcome, not a
        failure: another IDE window is fetching this very moment, and its result
        reaches us through the file watcher within seconds. The spinner clears
        when our round ends either way — it means "we checked", not "we fetched".
        """
        if self._stop_event.is_set():
            return
        self._set_refreshing(True)
        if self._inflight:
            # Adopt the running round — but remember the ask. `_inflight` is
            # cleared in the QUEUED `_on_results`, so a click can land after the
            # worker already finished and before that slot runs; adopting a dead
            # round there would clear the spinner without fetching anything and
            # the click would be silently lost. The flag makes `_on_results`
            # start a real round instead.
            self._refresh_pending = True
            return
        self._start_round(PROVIDER_IDS)

    def _start_round(self, providers: tuple[str, ...]) -> None:
        self._inflight = True
        self._pool.submit(self._run_round, providers)

    def _set_refreshing(self, value: bool) -> None:
        if self._refreshing == value:
            return
        self._refreshing = value
        self.refreshingChanged.emit()

    @Slot()
    def _tick(self) -> None:
        if self._inflight or self._stop_event.is_set():
            return
        stale = self._stale_providers()
        if not stale:
            return
        self._start_round(stale)

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
                    if usage.error:
                        continue
                    # ⚠ The module-level function, NOT `self._store.publish` —
                    # that method re-arms the QFileSystemWatcher, and this is a
                    # worker thread. Touching a GUI-thread QObject from here
                    # corrupts the watcher silently rather than raising. The
                    # re-arm still happens: the rename fires the watcher, and
                    # `UsageStore._on_file_changed` re-adds the path GUI-side.
                    if not publish_if_newer(self._store.path, usage):
                        # False is ambiguous: either a peer's observation is
                        # already newer (fine — we hold the poll lock, but a
                        # status-line tap from ANY window can land between our
                        # fetch and our write), or the write itself failed.
                        # Only the second is an error, and it matters: a write
                        # that silently fails leaves the file un-advanced, so
                        # the provider is re-fetched every tick forever with
                        # nothing on screen to say why. Re-read to tell them
                        # apart rather than reporting a spurious failure.
                        stored = read(self._store.path).get(provider)
                        if (
                            stored is None
                            or stored.observed_at_ns < usage.observed_at_ns
                        ):
                            errors[provider] = "publish-failed"
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
        if self._refresh_pending and not self._stop_event.is_set():
            # A click landed while this round was finishing. Honour it with a
            # real round rather than clearing the spinner on a result the user
            # never asked for; `refreshing` stays true across the handover.
            self._refresh_pending = False
            self._start_round(PROVIDER_IDS)
        else:
            # Whatever raised it — the round is over, so the readout stops.
            self._set_refreshing(False)
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
