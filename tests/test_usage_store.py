"""Pure-function tests for the multi-provider usage peer file.

Pins the three properties the panel's correctness rests on: the merge is PER
PROVIDER (so Claude's frequent taps can't evict Codex's rarer polls), an errored
snapshot can never overwrite good numbers, and the poll lock is genuinely
exclusive (so N open IDE windows still make one request per round).

No Qt: `read` / `publish_if_newer` / `poll_lock` are module-level and pure, the
same split `test_account_usage_store.py` uses.
"""

from __future__ import annotations

from symmetria_ide.usage_providers import ProviderUsage, UsageWindow
from symmetria_ide.usage_store import poll_lock, publish_if_newer, read


def _usage(provider: str, ts: int, pct: int = 10, error: str = "") -> ProviderUsage:
    return ProviderUsage(
        provider=provider,
        label=provider.title(),
        plan="max",
        windows=(UsageWindow(key="weekly", label="7d", pct=pct, resets_at=999),),
        observed_at_ns=ts,
        error=error,
    )


def test_read_absent_file_is_empty(tmp_path):
    assert read(tmp_path / "nope.json") == {}


def test_read_corrupt_file_is_empty(tmp_path):
    path = tmp_path / "usage.json"
    path.write_text("{not json", encoding="utf-8")
    assert read(path) == {}


def test_publish_writes_when_absent(tmp_path):
    path = tmp_path / "usage.json"
    assert publish_if_newer(path, _usage("claude", 1000)) is True
    assert read(path)["claude"].observed_at_ns == 1000


def test_providers_merge_independently(tmp_path):
    # The core multi-provider property: publishing Claude must leave Codex
    # intact. A single global timestamp (the legacy store's shape) would let
    # each Claude tap evict the Codex entry and flicker it out of the panel.
    path = tmp_path / "usage.json"
    publish_if_newer(path, _usage("codex", 500, pct=3))
    publish_if_newer(path, _usage("claude", 9000, pct=42))
    stored = read(path)
    assert set(stored) == {"claude", "codex"}
    assert stored["codex"].windows[0].pct == 3
    assert stored["claude"].windows[0].pct == 42


def test_publish_no_ops_on_older_same_provider(tmp_path):
    path = tmp_path / "usage.json"
    publish_if_newer(path, _usage("claude", 1000, pct=20))
    assert publish_if_newer(path, _usage("claude", 500, pct=99)) is False
    assert read(path)["claude"].windows[0].pct == 20


def test_publish_no_ops_on_equal_ts(tmp_path):
    # Equal is "not newer" — this is what stops our own write from looping our
    # own file watcher.
    path = tmp_path / "usage.json"
    publish_if_newer(path, _usage("claude", 1000))
    assert publish_if_newer(path, _usage("claude", 1000)) is False


def test_publish_overwrites_on_newer(tmp_path):
    path = tmp_path / "usage.json"
    publish_if_newer(path, _usage("claude", 1000, pct=20))
    assert publish_if_newer(path, _usage("claude", 2000, pct=33)) is True
    assert read(path)["claude"].windows[0].pct == 33


def test_errored_snapshot_never_wins(tmp_path):
    # `usage_providers` gives a failed fetch observed_at_ns 0, so a transient
    # network blip keeps showing the last good numbers instead of blanking.
    path = tmp_path / "usage.json"
    publish_if_newer(path, _usage("codex", 1000, pct=7))
    failed = ProviderUsage(provider="codex", label="Codex", error="offline")
    assert publish_if_newer(path, failed) is False
    assert read(path)["codex"].windows[0].pct == 7


def test_poll_lock_is_exclusive(tmp_path):
    # flock is keyed to the open file description, so a second acquisition —
    # even from the same process — behaves exactly like a competing IDE window.
    path = tmp_path / "usage.json"
    with poll_lock(path) as first:
        assert first is True
        with poll_lock(path) as second:
            assert second is False


def test_poll_lock_is_released_on_exit(tmp_path):
    path = tmp_path / "usage.json"
    with poll_lock(path) as acquired:
        assert acquired is True
    with poll_lock(path) as again:
        assert again is True
