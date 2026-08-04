"""Subscription-usage providers — one normalized snapshot per AI account.

The IDE surfaces "how much of my plan have I burned" for every agent CLI the
user drives (Claude, Codex today; opencode / GLM / Kimi are a dataclass away).
Each provider exposes that state through a different mechanism, so this module
is the single place that knows those mechanisms and flattens them into ONE
shape (`ProviderUsage`) that the store, the poller and QML all speak.

Pure by design — no Qt, no threads, no file watching. `fetch_*` performs
blocking I/O (HTTP / subprocess) and MUST be called off the GUI thread; the
Qt wiring lives in `usage_poller.py`.

Mechanisms, as measured (not assumed):

  claude  GET https://api.anthropic.com/api/oauth/usage with the OAuth token
          Claude Code already stores in ~/.claude/.credentials.json. Returns
          the 5h + 7d windows, per-model weekly buckets, and extra-usage
          credits. ⚠ UNDOCUMENTED endpoint (it backs the CLI's own /usage) —
          it can change without notice, which is why every failure degrades to
          an `error` snapshot instead of raising, and why the IDE also keeps
          the free event-driven path (the status-line tap) as the primary
          source for Claude. See `usage_poller` for how the two compose.

  codex   `codex app-server` speaks JSON-RPC over stdio; the exchange is
          initialize → initialized → account/rateLimits/read. Costs ~1.9s of
          wall time (process spawn + network), hence off-thread only.

Error contract, load-bearing: a failed fetch returns a `ProviderUsage` with
`error` set and **`observed_at_ns == 0`**. Zero never wins the store's
freshest-wins merge, so a transient network blip can NEVER overwrite the last
known-good numbers with an empty snapshot — the UI keeps showing the stale
values (with their age) plus the error, which is strictly more useful than
blanking the panel.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

__all__ = [
    "CLAUDE",
    "CODEX",
    "KEY_SCOPED",
    "KEY_SESSION",
    "KEY_WEEKLY",
    "PROVIDER_IDS",
    "ProviderUsage",
    "UsageWindow",
    "fetch",
    "fetch_claude",
    "fetch_codex",
    "label_for_window",
    "merge_tap_observation",
    "parse_claude_usage",
    "parse_codex_usage",
    "usage_from_dict",
]

CLAUDE = "claude"
CODEX = "codex"

# Registry order == display order in the status bar.
PROVIDER_IDS: tuple[str, ...] = (CLAUDE, CODEX)

_CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# The beta header the CLI sends alongside an OAuth (not API-key) credential.
_CLAUDE_OAUTH_BETA = "oauth-2025-04-20"
_HTTP_TIMEOUT_SECONDS = 10.0
# Codex spawns a process and talks to the network; measured ~1.9s end to end,
# so this is ~5x headroom rather than a tight bound.
_CODEX_TIMEOUT_SECONDS = 15.0

# Window keys. `session` and `weekly` are the two the compact indicator shows;
# `scoped` covers per-model sub-buckets (Claude's weekly_scoped, Codex's extra
# limit ids) and is popup-only.
KEY_SESSION = "session"
KEY_WEEKLY = "weekly"
KEY_SCOPED = "scoped"

_MINUTES_PER_DAY = 1440


@dataclass(slots=True, frozen=True)
class UsageWindow:
    """One rate-limit window (a 5h session, a 7d week, a per-model bucket)."""

    key: str
    label: str
    pct: int
    resets_at: int  # unix epoch SECONDS — the contract QML's countdown assumes
    scope: str = ""  # "" for account-wide; else the model/bucket display name

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "pct": self.pct,
            "resets_at": self.resets_at,
            "scope": self.scope,
        }


@dataclass(slots=True, frozen=True)
class ProviderUsage:
    """One account's usage state, normalized across providers."""

    provider: str
    label: str
    plan: str = ""
    windows: tuple[UsageWindow, ...] = ()
    extra: dict = field(default_factory=dict)  # credits / spend — popup only
    observed_at_ns: int = 0  # 0 == failed fetch; never wins the merge
    source: str = "poll"  # "poll" | "tap"
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "provider": self.provider,
            "label": self.label,
            "plan": self.plan,
            "windows": [w.as_dict() for w in self.windows],
            "extra": self.extra,
            "observed_at_ns": self.observed_at_ns,
            "source": self.source,
            "error": self.error,
        }


def usage_from_dict(data: dict) -> ProviderUsage | None:
    """Rehydrate a snapshot written by `as_dict` (the shared-file read path).

    Tolerant: anything malformed reads as None ("no value") rather than
    raising, matching the store's treatment of a corrupt file.
    """
    if not isinstance(data, dict) or not data.get("provider"):
        return None
    windows = []
    for raw in data.get("windows") or ():
        if not isinstance(raw, dict):
            continue
        windows.append(
            UsageWindow(
                key=str(raw.get("key", "")),
                label=str(raw.get("label", "")),
                pct=_to_int(raw.get("pct")),
                resets_at=_to_int(raw.get("resets_at")),
                scope=str(raw.get("scope", "")),
            )
        )
    extra = data.get("extra")
    return ProviderUsage(
        provider=str(data["provider"]),
        label=str(data.get("label", "")),
        plan=str(data.get("plan", "")),
        windows=tuple(windows),
        extra=extra if isinstance(extra, dict) else {},
        observed_at_ns=_to_int(data.get("observed_at_ns")),
        source=str(data.get("source", "poll")),
        error=str(data.get("error", "")),
    )


def label_for_window(minutes: int | None) -> str:
    """Compact label for a window of `minutes` ("5h", "7d", "").

    Derived, never hardcoded per provider: if Codex ever re-enables its session
    window (today it reports only the 10080-minute weekly one), it labels itself
    correctly with no code change here.
    """
    mins = _to_int(minutes)
    if mins <= 0:
        return ""
    if mins >= _MINUTES_PER_DAY:
        return f"{mins // _MINUTES_PER_DAY}d"
    if mins >= 60:
        return f"{mins // 60}h"
    return f"{mins}m"


def _key_for_window(minutes: int | None) -> str:
    """A window at or under a day is a "session"; anything longer is "weekly"."""
    return KEY_WEEKLY if _to_int(minutes) >= _MINUTES_PER_DAY else KEY_SESSION


def _to_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _epoch_from_iso(value) -> int:
    """ISO-8601 (with offset, possibly fractional seconds) → epoch seconds.

    Claude's endpoint returns "2026-08-04T20:10:00.600239+00:00"; Codex returns
    a bare epoch. Everything downstream uses epoch seconds, so this is the one
    conversion point. Unparseable → 0, which the QML countdown renders as "no
    reset known" rather than a bogus date.
    """
    if not isinstance(value, str) or not value:
        return 0
    try:
        return int(datetime.fromisoformat(value).timestamp())
    except ValueError:
        log.debug("usage: unparseable reset timestamp %r", value)
        return 0


# ---------------------------------------------------------------- claude


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    """Turns any 3xx into an HTTPError instead of following it.

    ⚠ Security, not politeness. `urlopen` follows redirects by default and
    `HTTPRedirectHandler` carries the request headers to the target — including
    `Authorization: Bearer <the user's live Claude Code OAuth token>`. The
    endpoint this talks to is explicitly undocumented and unstable, so a 30x to
    a different host is a plausible future and would hand out that token. The
    resulting HTTPError lands in the existing `http-<code>` error branch.
    """

    def redirect_request(self, *_args, **_kwargs):
        return None


def _no_redirect_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_RefuseRedirect())


def _claude_credentials_path() -> Path:
    return Path(os.path.expanduser("~/.claude/.credentials.json"))


def _claude_oauth(credentials_path: Path) -> dict | None:
    """Read the OAuth block Claude Code maintains, or None if unusable.

    Read fresh on EVERY fetch and never cached: Claude Code rotates this token
    on its own schedule, and a cached copy would start 401-ing after a refresh.
    We only ever read it — refreshing it ourselves would race the CLI's own
    refresh and could invalidate the user's live session.
    """
    try:
        with open(credentials_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    except OSError as exc:
        log.debug("usage: claude credentials unreadable (%s)", exc)
        return None
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict) or not oauth.get("accessToken"):
        return None
    return oauth


def _claude_window(block, key: str, label: str) -> UsageWindow | None:
    if not isinstance(block, dict):
        return None
    return UsageWindow(
        key=key,
        label=label,
        pct=_to_int(block.get("utilization")),
        resets_at=_epoch_from_iso(block.get("resets_at")),
    )


def parse_claude_usage(payload: dict, plan: str, observed_at_ns: int) -> ProviderUsage:
    """Flatten the OAuth usage payload. Pure — the unit tests' entry point."""
    windows: list[UsageWindow] = []
    session = _claude_window(payload.get("five_hour"), KEY_SESSION, "5h")
    if session is not None:
        windows.append(session)
    weekly = _claude_window(payload.get("seven_day"), KEY_WEEKLY, "7d")
    if weekly is not None:
        windows.append(weekly)

    # Per-model weekly sub-buckets ride the `limits` array rather than a named
    # top-level key, because their set varies per account (Fable today, another
    # model tomorrow). Only the scoped ones are taken here — the account-wide
    # session/weekly rows would duplicate what we already read above.
    for limit in payload.get("limits") or ():
        if not isinstance(limit, dict) or limit.get("kind") != "weekly_scoped":
            continue
        scope = ""
        model = (
            (limit.get("scope") or {}).get("model")
            if isinstance(limit.get("scope"), dict)
            else None
        )
        if isinstance(model, dict):
            scope = str(model.get("display_name") or "")
        windows.append(
            UsageWindow(
                key=KEY_SCOPED,
                label="7d",
                pct=_to_int(limit.get("percent")),
                resets_at=_epoch_from_iso(limit.get("resets_at")),
                scope=scope,
            )
        )

    extra: dict = {}
    credits = payload.get("extra_usage")
    if isinstance(credits, dict):
        # Only what the popup renders — this dict is serialized into the shared
        # file and marshalled into a QML row on every observation, so unread
        # fields (utilization, currency) are pure weight.
        extra["credits"] = {
            "enabled": bool(credits.get("is_enabled")),
            "used": _to_int(credits.get("used_credits")),
            "limit": _to_int(credits.get("monthly_limit")),
            "disabled_reason": str(credits.get("disabled_reason") or ""),
        }

    return ProviderUsage(
        provider=CLAUDE,
        label="Claude",
        plan=plan,
        windows=tuple(windows),
        extra=extra,
        observed_at_ns=observed_at_ns,
        source="poll",
    )


def fetch_claude(credentials_path: Path | None = None) -> ProviderUsage:
    """Poll Claude's account usage. Blocking — call off the GUI thread."""
    path = credentials_path or _claude_credentials_path()
    oauth = _claude_oauth(path)
    if oauth is None:
        return _failed(CLAUDE, "Claude", "no-credentials")

    request = urllib.request.Request(
        _CLAUDE_USAGE_URL,
        headers={
            "Authorization": f"Bearer {oauth['accessToken']}",
            "anthropic-beta": _CLAUDE_OAUTH_BETA,
            "Accept": "application/json",
            "User-Agent": "symmetria-ide",
        },
    )
    try:
        with _no_redirect_opener().open(
            request, timeout=_HTTP_TIMEOUT_SECONDS
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # 401/403 usually means we read the token mid-rotation; the next tick
        # picks up the refreshed one, so this is transient, not fatal.
        reason = "auth" if exc.code in (401, 403) else f"http-{exc.code}"
        return _failed(CLAUDE, "Claude", reason)
    except (urllib.error.URLError, TimeoutError, OSError):
        return _failed(CLAUDE, "Claude", "offline")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _failed(CLAUDE, "Claude", "bad-response")

    if not isinstance(payload, dict):
        return _failed(CLAUDE, "Claude", "bad-response")
    # The plan rides the credentials file, not the usage payload — free, and it
    # avoids a second request to /oauth/profile purely for a display string.
    plan = str(oauth.get("subscriptionType") or "")
    return parse_claude_usage(payload, plan, time.time_ns())


# ----------------------------------------------------------------- codex


def parse_codex_usage(payload: dict, observed_at_ns: int) -> ProviderUsage:
    """Flatten `account/rateLimits/read`'s result. Pure — unit-test entry point."""
    snapshot = payload.get("rateLimits")
    if not isinstance(snapshot, dict):
        return _failed(CODEX, "Codex", "bad-response")

    windows: list[UsageWindow] = []
    for slot in ("primary", "secondary"):
        block = snapshot.get(slot)
        if not isinstance(block, dict):
            continue  # `secondary` is null on plans with only a weekly window
        minutes = block.get("windowDurationMins")
        windows.append(
            UsageWindow(
                key=_key_for_window(minutes),
                label=label_for_window(minutes),
                pct=_to_int(block.get("usedPercent")),
                resets_at=_to_int(block.get("resetsAt")),
            )
        )

    # Extra metered buckets (e.g. a model-specific limit) arrive keyed by
    # limit id alongside the main one; skip the main id so it isn't doubled.
    # ⚠ `rateLimitsByLimitId` is a SIBLING of `rateLimits` in the result, not a
    # child of it — reading it off `snapshot` silently yields no extra buckets.
    main_id = str(snapshot.get("limitId") or "")
    by_id = payload.get("rateLimitsByLimitId")
    if isinstance(by_id, dict):
        for limit_id, bucket in by_id.items():
            if limit_id == main_id or not isinstance(bucket, dict):
                continue
            block = bucket.get("primary")
            if not isinstance(block, dict):
                continue
            windows.append(
                UsageWindow(
                    key=KEY_SCOPED,
                    label=label_for_window(block.get("windowDurationMins")),
                    pct=_to_int(block.get("usedPercent")),
                    resets_at=_to_int(block.get("resetsAt")),
                    scope=str(bucket.get("limitName") or limit_id),
                )
            )

    extra: dict = {}
    credits = snapshot.get("credits")
    if isinstance(credits, dict):
        extra["credits"] = {
            "has_credits": bool(credits.get("hasCredits")),
            "unlimited": bool(credits.get("unlimited")),
            "balance": str(credits.get("balance") or ""),
        }
    resets = payload.get("rateLimitResetCredits")
    if isinstance(resets, dict):
        extra["reset_credits"] = _to_int(resets.get("availableCount"))

    return ProviderUsage(
        provider=CODEX,
        label="Codex",
        plan=str(snapshot.get("planType") or ""),
        windows=tuple(windows),
        extra=extra,
        observed_at_ns=observed_at_ns,
        source="poll",
    )


def _codex_exchange(proc: subprocess.Popen) -> dict | None:
    """Drive initialize → initialized → account/rateLimits/read; return result.

    The server answers requests out of order relative to its own notifications
    (remoteControl/status/changed lands between our two calls), so frames are
    matched on `id`, never on arrival order.
    """
    assert proc.stdin is not None and proc.stdout is not None

    def send(message: dict) -> None:
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()

    send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"clientInfo": {"name": "symmetria-ide", "version": "1"}},
        }
    )
    for line in proc.stdout:
        try:
            frame = json.loads(line)
        except json.JSONDecodeError:
            continue  # stray non-JSON line; the protocol frames still follow
        if not isinstance(frame, dict):
            continue
        if frame.get("id") == 1:
            send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
            send(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "account/rateLimits/read",
                    "params": {},
                }
            )
        elif frame.get("id") == 2:
            result = frame.get("result")
            return result if isinstance(result, dict) else None
    return None  # pipe closed (killed by the watchdog, or the server died)


def fetch_codex() -> ProviderUsage:
    """Poll Codex's account rate limits. Blocking — call off the GUI thread."""
    try:
        proc = subprocess.Popen(
            ["codex", "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            # DEVNULL, not PIPE: nothing reads this stream, and an unread pipe
            # would block the server once its buffer filled. Safe here (unlike
            # ChromeHost, which is long-lived and must drain its stderr).
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        return _failed(CODEX, "Codex", "not-installed")
    except OSError:
        return _failed(CODEX, "Codex", "spawn-failed")

    # `with`, so Popen.__exit__ closes stdin/stdout even on the path where the
    # bounded wait below times out. Without it a wedged app-server leaks two
    # pipe fds per poll round — every few minutes, for the life of the IDE.
    with proc:
        # A watchdog kill (rather than per-read timeouts) is what bounds this:
        # it closes stdout, so the read loop terminates on its own with no
        # select()/non-blocking plumbing.
        watchdog = threading.Timer(_CODEX_TIMEOUT_SECONDS, proc.kill)
        watchdog.daemon = True
        watchdog.start()
        try:
            result = _codex_exchange(proc)
        except OSError:
            result = None  # broken pipe — the watchdog killed it mid-write
        finally:
            watchdog.cancel()
            proc.kill()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                log.debug("usage: codex app-server did not reap in time")

    if result is None:
        return _failed(CODEX, "Codex", "timeout")
    return parse_codex_usage(result, time.time_ns())


def merge_tap_observation(
    previous: ProviderUsage | None,
    five_pct: int,
    five_reset: int,
    seven_pct: int,
    seven_reset: int,
    observed_at_ns: int,
) -> ProviderUsage:
    """Fold a Claude status-line tap into the last known snapshot.

    The tap is the FRESHEST source for Claude (it fires on every agent turn, for
    free) but the POOREST: it carries only the two account-wide percentages and
    their resets — no plan, no per-model buckets, no credits. Publishing it raw
    would therefore strip the popup back to two rows after every turn, and a
    later poll would restore them, so the detail view would visibly flicker.

    Carrying the un-observed fields forward from `previous` keeps the tap's
    freshness without its poverty. The scoped windows do go slightly stale
    between polls, which is the correct trade: they are popup-only detail, while
    the two numbers on the status bar are always current.
    """
    scoped = (
        tuple(w for w in previous.windows if w.key == KEY_SCOPED) if previous else ()
    )
    windows = (
        UsageWindow(key=KEY_SESSION, label="5h", pct=five_pct, resets_at=five_reset),
        UsageWindow(key=KEY_WEEKLY, label="7d", pct=seven_pct, resets_at=seven_reset),
        *scoped,
    )
    return ProviderUsage(
        provider=CLAUDE,
        label="Claude",
        plan=previous.plan if previous else "",
        windows=windows,
        extra=previous.extra if previous else {},
        observed_at_ns=observed_at_ns,
        source="tap",
    )


# --------------------------------------------------------------- registry


def _failed(provider: str, label: str, reason: str) -> ProviderUsage:
    """An errored snapshot: carries the reason, never wins the merge (ts 0)."""
    log.debug("usage: %s fetch failed (%s)", provider, reason)
    return ProviderUsage(provider=provider, label=label, error=reason)


_FETCHERS: dict[str, Callable[[], ProviderUsage]] = {
    CLAUDE: fetch_claude,
    CODEX: fetch_codex,
}


def fetch(provider: str) -> ProviderUsage:
    """Fetch one provider by id. Unknown id → an errored snapshot, not a raise.

    Adding a provider (opencode / GLM / Kimi) is: a `fetch_<name>` above, an
    entry here, and its id in `PROVIDER_IDS`. Nothing in the store, the poller
    or the QML needs to change.
    """
    fetcher = _FETCHERS.get(provider)
    if fetcher is None:
        return _failed(provider, provider.title(), "unknown-provider")
    return fetcher()
