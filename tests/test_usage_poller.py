"""Scheduling behaviour of the usage poller — no threads, no network.

`_pool.submit` is replaced with a recorder, so every test here exercises the
DECISION (which providers a round targets, when a round is started at all, how
the refreshing flag moves) rather than the fetch. The fetch itself is covered by
`test_usage_providers.py`; the two never need to run together.

The session-scoped QCoreApplication from conftest is what lets a QObject be
constructed at all; nothing here pumps the event loop (see the conftest warning
about `processEvents`), so the queued `_resultsReady` is invoked directly.
"""

from __future__ import annotations

import time

from symmetria_ide.usage_poller import UsagePoller
from symmetria_ide.usage_providers import PROVIDER_IDS, ProviderUsage, UsageWindow
from symmetria_ide.usage_store import UsageStore


class _RecordingPool:
    """Stands in for the ThreadPoolExecutor; records instead of running."""

    def __init__(self):
        self.submitted: list[tuple] = []

    def submit(self, fn, *args):
        self.submitted.append(args)

    def shutdown(self, **_kwargs):
        pass


def _poller(tmp_path, ttl_seconds: int = 300) -> tuple[UsagePoller, _RecordingPool]:
    store = UsageStore(path=tmp_path / "usage.json")
    poller = UsagePoller(store, ttl_seconds=ttl_seconds)
    # Close the real pool right away — no test here wants a worker thread, and
    # a live one would pin the poller (the leak the conftest fixture exists for).
    poller._pool.shutdown(wait=False)
    pool = _RecordingPool()
    poller._pool = pool
    return poller, pool


def _fresh(provider: str, age_seconds: float = 0.0) -> ProviderUsage:
    return ProviderUsage(
        provider=provider,
        label=provider.title(),
        windows=(UsageWindow(key="weekly", label="7d", pct=5, resets_at=1),),
        observed_at_ns=time.time_ns() - int(age_seconds * 1_000_000_000),
    )


# -- automatic rounds ---------------------------------------------------


def test_tick_targets_only_stale_providers(tmp_path):
    poller, pool = _poller(tmp_path)
    poller._store.publish(_fresh("claude"))  # fresh; codex has never been seen
    poller._tick()
    assert pool.submitted == [(("codex",),)]
    poller.stop()


def test_tick_does_nothing_when_everything_is_fresh(tmp_path):
    # The whole point of the TTL: a busy Claude session (its status-line tap
    # keeps publishing) must cost zero HTTP requests.
    poller, pool = _poller(tmp_path)
    for provider in PROVIDER_IDS:
        poller._store.publish(_fresh(provider))
    poller._tick()
    assert pool.submitted == []
    poller.stop()


def test_tick_does_not_raise_the_refreshing_flag(tmp_path):
    # An automatic round must not pulse the readout — a status bar that blinks
    # on its own every few minutes reads as a glitch.
    poller, _pool = _poller(tmp_path)
    poller._tick()
    assert poller.refreshing is False
    poller.stop()


def test_start_is_disabled_by_the_suite_wide_off_switch(tmp_path):
    # conftest sets SYMMETRIA_IDE_USAGE_POLL=0 for every test, which is what
    # keeps `AppController.start()` from hitting the real accounts.
    poller, pool = _poller(tmp_path)
    poller.start()
    assert pool.submitted == []
    poller.stop()


def test_start_polls_immediately_by_default(monkeypatch, tmp_path):
    # The branch the fixture above makes unreachable, asserted explicitly: with
    # no off switch, start() checks at once rather than waiting a full tick —
    # that is what makes a freshly-launched IDE show usage within seconds.
    monkeypatch.delenv("SYMMETRIA_IDE_USAGE_POLL", raising=False)
    poller, pool = _poller(tmp_path)
    poller.start()
    assert pool.submitted == [(PROVIDER_IDS,)]  # nothing stored yet → all stale
    poller.stop()


# -- user-requested refresh ---------------------------------------------


def test_refresh_targets_every_provider_even_when_fresh(tmp_path):
    # Clicking means "prove these are current", which a skipped-because-fresh
    # provider does not do — so the TTL is deliberately ignored here.
    poller, pool = _poller(tmp_path)
    for provider in PROVIDER_IDS:
        poller._store.publish(_fresh(provider))
    poller.refresh()
    assert pool.submitted == [(PROVIDER_IDS,)]
    assert poller.refreshing is True
    poller.stop()


def test_refresh_adopts_an_in_flight_round_instead_of_queueing(tmp_path):
    # The pool has one worker, so a queued second round would run AFTER the
    # first and double the requests for no new information.
    poller, pool = _poller(tmp_path)
    poller._tick()  # starts a round (nothing stored yet → everything stale)
    assert len(pool.submitted) == 1
    poller.refresh()
    assert len(pool.submitted) == 1  # no second submission
    assert poller.refreshing is True  # but the readout still shows the work
    poller.stop()


def test_results_clear_the_refreshing_flag(tmp_path):
    poller, _pool = _poller(tmp_path)
    poller.refresh()
    assert poller.refreshing is True
    poller._on_results({"claude": "", "codex": ""})
    assert poller.refreshing is False
    poller.stop()


def test_results_clear_refreshing_even_when_the_round_failed(tmp_path):
    # "We checked" is what the spinner means, so an errored round must still
    # end it — otherwise a provider that is down pulses the bar forever.
    poller, _pool = _poller(tmp_path)
    poller.refresh()
    poller._on_results({"claude": "offline"})
    assert poller.refreshing is False
    assert poller.errors["claude"] == "offline"
    poller.stop()


def test_refresh_after_stop_does_nothing(tmp_path):
    poller, pool = _poller(tmp_path)
    poller.stop()
    poller.refresh()
    assert pool.submitted == []
    assert poller.refreshing is False


def test_refreshing_signal_fires_once_per_transition(tmp_path):
    poller, _pool = _poller(tmp_path)
    seen: list[bool] = []
    poller.refreshingChanged.connect(lambda: seen.append(poller.refreshing))
    poller.refresh()
    poller.refresh()  # already refreshing — must not re-emit
    poller._on_results({})
    assert seen == [True, False]
    poller.stop()
