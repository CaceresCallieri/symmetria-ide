"""Parser tests for the subscription-usage providers.

The payloads below are TRIMMED COPIES OF REAL RESPONSES captured from the live
account, not invented fixtures — including the two shapes that actually bit
during development: Codex reporting `secondary: null` (its plan exposes only a
weekly window), and `rateLimitsByLimitId` sitting as a SIBLING of `rateLimits`
rather than inside it.

No network, no subprocess: `parse_*` is where all the shape knowledge lives, so
the fetchers only add I/O and error mapping around it.
"""

from __future__ import annotations

import json

from symmetria_ide.usage_providers import (
    CLAUDE,
    CODEX,
    KEY_SCOPED,
    KEY_SESSION,
    KEY_WEEKLY,
    ProviderUsage,
    fetch,
    fetch_claude,
    label_for_window,
    merge_tap_observation,
    parse_claude_usage,
    parse_codex_usage,
    usage_from_dict,
)

_CLAUDE_PAYLOAD = {
    "five_hour": {
        "utilization": 19.0,
        "resets_at": "2026-08-04T20:10:00.600239+00:00",
    },
    "seven_day": {
        "utilization": 20.0,
        "resets_at": "2026-08-10T13:00:00.600258+00:00",
    },
    "limits": [
        {"kind": "session", "percent": 19, "resets_at": "2026-08-04T20:10:00+00:00"},
        {"kind": "weekly_all", "percent": 20, "resets_at": "2026-08-10T13:00:00+00:00"},
        {
            "kind": "weekly_scoped",
            "percent": 9,
            "resets_at": "2026-08-10T12:59:59.600477+00:00",
            "scope": {"model": {"id": None, "display_name": "Fable"}},
        },
    ],
    "extra_usage": {
        "is_enabled": False,
        "monthly_limit": 5000,
        "used_credits": 2524.0,
        "utilization": 50.48,
        "currency": "USD",
        "disabled_reason": "out_of_credits",
    },
}

_CODEX_PAYLOAD = {
    "rateLimits": {
        "limitId": "codex",
        "planType": "prolite",
        "primary": {
            "usedPercent": 3,
            "windowDurationMins": 10080,
            "resetsAt": 1786303084,
        },
        "secondary": None,
        "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
    },
    "rateLimitsByLimitId": {
        "codex": {"limitId": "codex", "primary": {"usedPercent": 3}},
        "codex_bengalfox": {
            "limitId": "codex_bengalfox",
            "limitName": "GPT-5.3-Codex-Spark",
            "primary": {
                "usedPercent": 0,
                "windowDurationMins": 10080,
                "resetsAt": 1786466835,
            },
        },
    },
    "rateLimitResetCredits": {"availableCount": 1},
}


def _window(usage: ProviderUsage, key: str, scope: str = ""):
    for w in usage.windows:
        if w.key == key and w.scope == scope:
            return w
    return None


# -- label derivation ---------------------------------------------------


def test_label_for_window_derives_from_duration():
    # Never hardcoded per provider: a session window Codex might re-enable
    # labels itself correctly with no code change.
    assert label_for_window(10080) == "7d"
    assert label_for_window(300) == "5h"
    assert label_for_window(30) == "30m"


def test_label_for_window_absent_is_empty():
    assert label_for_window(None) == ""
    assert label_for_window(0) == ""


# -- claude -------------------------------------------------------------


def test_claude_parses_session_and_weekly():
    usage = parse_claude_usage(_CLAUDE_PAYLOAD, "max", 1234)
    assert usage.provider == CLAUDE
    assert usage.plan == "max"
    assert usage.observed_at_ns == 1234
    session = _window(usage, KEY_SESSION)
    weekly = _window(usage, KEY_WEEKLY)
    assert session is not None and session.pct == 19 and session.label == "5h"
    assert weekly is not None and weekly.pct == 20 and weekly.label == "7d"


def test_claude_converts_iso_reset_to_epoch_seconds():
    # The QML countdown does `resets_at - Date.now()/1000`, so seconds is the
    # contract; an ISO string reaching QML would render as a bogus countdown.
    session = _window(parse_claude_usage(_CLAUDE_PAYLOAD, "max", 1), KEY_SESSION)
    assert session is not None
    assert session.resets_at == 1785874200


def test_claude_keeps_only_scoped_limits_from_the_array():
    # `limits` repeats the account-wide session/weekly rows we already read from
    # the named keys — taking them again would double every window.
    usage = parse_claude_usage(_CLAUDE_PAYLOAD, "max", 1)
    scoped = [w for w in usage.windows if w.key == KEY_SCOPED]
    assert len(scoped) == 1
    assert scoped[0].scope == "Fable" and scoped[0].pct == 9


def test_claude_carries_extra_credits():
    usage = parse_claude_usage(_CLAUDE_PAYLOAD, "max", 1)
    assert usage.extra["credits"]["used"] == 2524
    assert usage.extra["credits"]["limit"] == 5000


def test_claude_missing_credentials_is_an_error_snapshot(tmp_path):
    # The error contract: no exception, and observed_at_ns 0 so the store's
    # merge can never let this blank out good numbers.
    usage = fetch_claude(tmp_path / "absent.json")
    assert usage.error == "no-credentials"
    assert usage.observed_at_ns == 0
    assert usage.windows == ()


def test_claude_credentials_without_oauth_block_is_an_error(tmp_path):
    path = tmp_path / "creds.json"
    path.write_text(json.dumps({"mcpOAuth": {}}), encoding="utf-8")
    assert fetch_claude(path).error == "no-credentials"


# -- codex --------------------------------------------------------------


def test_codex_parses_the_weekly_window():
    usage = parse_codex_usage(_CODEX_PAYLOAD, 99)
    assert usage.provider == CODEX and usage.plan == "prolite"
    weekly = _window(usage, KEY_WEEKLY)
    assert weekly is not None
    assert (weekly.pct, weekly.label, weekly.resets_at) == (3, "7d", 1786303084)


def test_codex_tolerates_null_secondary_window():
    # This plan exposes only a weekly window; `secondary: null` is the normal
    # case, not a degraded one.
    usage = parse_codex_usage(_CODEX_PAYLOAD, 1)
    assert _window(usage, KEY_SESSION) is None


def test_codex_reads_per_model_buckets_from_the_sibling_key():
    # Regression: `rateLimitsByLimitId` is a sibling of `rateLimits`, not a
    # child. Reading it off the wrong level silently yields zero extra buckets.
    usage = parse_codex_usage(_CODEX_PAYLOAD, 1)
    spark = _window(usage, KEY_SCOPED, scope="GPT-5.3-Codex-Spark")
    assert spark is not None and spark.pct == 0


def test_codex_skips_the_main_bucket_in_the_by_id_map():
    usage = parse_codex_usage(_CODEX_PAYLOAD, 1)
    assert [w.scope for w in usage.windows if w.key == KEY_SCOPED] == [
        "GPT-5.3-Codex-Spark"
    ]


def test_codex_malformed_payload_is_an_error_snapshot():
    usage = parse_codex_usage({"rateLimits": None}, 1)
    assert usage.error == "bad-response" and usage.observed_at_ns == 0


def test_codex_carries_reset_credits():
    assert parse_codex_usage(_CODEX_PAYLOAD, 1).extra["reset_credits"] == 1


# -- registry + serialization -------------------------------------------


def test_unknown_provider_errors_rather_than_raising():
    usage = fetch("gemini")
    assert usage.error == "unknown-provider" and usage.observed_at_ns == 0


def test_snapshot_survives_a_dict_round_trip():
    # as_dict/usage_from_dict is the shared-file wire format; a lossy round trip
    # would silently degrade what peer windows read.
    original = parse_claude_usage(_CLAUDE_PAYLOAD, "max", 4242)
    restored = usage_from_dict(json.loads(json.dumps(original.as_dict())))
    assert restored == original


def test_tap_merge_keeps_what_the_tap_cannot_observe():
    # The status-line tap carries only the two account-wide percentages. Folding
    # it into the last poll is what stops the detail popup from losing its plan,
    # per-model buckets and credits after every single agent turn.
    polled = parse_claude_usage(_CLAUDE_PAYLOAD, "max", 1000)
    tapped = merge_tap_observation(polled, 55, 111, 66, 222, 2000)
    assert tapped.source == "tap" and tapped.observed_at_ns == 2000
    assert _window(tapped, KEY_SESSION).pct == 55
    assert _window(tapped, KEY_WEEKLY).pct == 66
    assert _window(tapped, KEY_SCOPED, scope="Fable") is not None
    assert tapped.plan == "max"
    assert tapped.extra["credits"]["used"] == 2524


def test_tap_merge_works_with_no_prior_snapshot():
    # First observation of a cold boot: no poll has landed yet, so there is
    # simply nothing to carry forward.
    tapped = merge_tap_observation(None, 10, 1, 20, 2, 500)
    assert [w.key for w in tapped.windows] == [KEY_SESSION, KEY_WEEKLY]
    assert tapped.plan == "" and tapped.extra == {}


def test_usage_from_dict_rejects_junk():
    assert usage_from_dict({}) is None
    assert usage_from_dict({"provider": ""}) is None
    assert usage_from_dict("nope") is None
