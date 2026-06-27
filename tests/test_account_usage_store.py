"""Pure-function tests for the cross-IDE account-usage peer file.

Exercises `read` / `publish_if_newer` against a tmp file — no Qt, no watcher.
The last-write-wins-by-`observed_at_ns` contract is what keeps the shared file
convergent across IDE instances (and keeps an IDE's own write from looping its
watcher), so it's worth pinning.
"""

from __future__ import annotations

from symmetria_ide.account_usage_store import publish_if_newer, read


def _usage(ts: int, five_pct: int = 10) -> dict:
    return {
        "five_pct": five_pct,
        "five_reset": 100,
        "seven_pct": 50,
        "seven_reset": 200,
        "observed_at_ns": ts,
    }


def test_read_absent_file_is_none(tmp_path):
    assert read(tmp_path / "nope.json") is None


def test_publish_writes_when_absent(tmp_path):
    p = tmp_path / "usage.json"
    assert publish_if_newer(p, _usage(1000)) is True
    got = read(p)
    assert got is not None and got["observed_at_ns"] == 1000


def test_publish_no_ops_on_older(tmp_path):
    p = tmp_path / "usage.json"
    publish_if_newer(p, _usage(1000, five_pct=20))
    assert publish_if_newer(p, _usage(500, five_pct=99)) is False
    assert read(p)["five_pct"] == 20  # unchanged


def test_publish_no_ops_on_equal_ts(tmp_path):
    # Equal ts is "not newer" — this is what makes our own write a no-op when it
    # comes back through the watcher, so there's no write loop.
    p = tmp_path / "usage.json"
    publish_if_newer(p, _usage(1000))
    assert publish_if_newer(p, _usage(1000)) is False


def test_publish_overwrites_on_newer(tmp_path):
    p = tmp_path / "usage.json"
    publish_if_newer(p, _usage(1000, five_pct=20))
    assert publish_if_newer(p, _usage(2000, five_pct=33)) is True
    assert read(p)["five_pct"] == 33


def test_publish_rejects_zero_timestamp(tmp_path):
    p = tmp_path / "usage.json"
    assert publish_if_newer(p, _usage(0)) is False
    assert read(p) is None


def test_read_tolerates_partial_json(tmp_path):
    p = tmp_path / "usage.json"
    p.write_text("{ not valid json")
    assert read(p) is None


def test_read_rejects_dict_without_observed_at(tmp_path):
    p = tmp_path / "usage.json"
    p.write_text('{"five_pct": 10}')
    assert read(p) is None
