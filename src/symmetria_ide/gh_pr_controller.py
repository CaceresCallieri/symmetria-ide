"""GitHub pull-request viewing (+ checkout) for the "git" central surface.

The fourth git controller in the IDE, and the first to speak to GitHub. The
local three cover the working tree (``GitController``), the committed log
(``GitLogController``), and transport (``GitOpsController``); this one covers
the *remote collaboration* layer — pull requests fetched via the ``gh`` CLI
(compose, don't reimplement: gh owns auth, pagination, and rate limits).

Three layers, top-down (mirrors ``git_log_controller.py``):

  1. Pure parsing/formatting — ``relative_time``, ``classify_gh_failure``,
     ``parse_concatenated_json_arrays``, ``parse_pr_list``,
     ``flatten_timeline``. No subprocess, no Qt, no I/O. These carry the
     tricky logic (timeline merging, gh's paginated-JSON quirk) and are the
     unit-test centerpiece.

  2. ``PrRow`` / ``TimelineEntry`` — frozen slots dataclasses crossing the
     worker→GUI boundary (project-standards §1 P1).

  3. ``GhPrController(QObject)`` + ``GhPrListModel`` / ``GhPrTimelineModel``
     — the Qt facade. One daemon worker drains a ``queue.Queue`` of typed
     requests (list page, one PR's detail, checkout), with the standard
     discipline: ``daemon=True`` worker owning a ``_stop_event`` (§1 P0),
     ``gc.disable()`` around every cross-thread emit (gotcha #10), receivers
     hopping the GUI thread via ``Qt.QueuedConnection`` (§4 P2), and
     apply-side race guards so a stale fetch from a previous repo root or
     state filter never lands in the model.

Freshness is strictly manual by design (user decision — no polling, ever; an
event-driven system may supersede this later): ``ensure_loaded()`` fetches
once per ``(repo_root, state_filter)`` when the PR tab is entered, and
``refresh()`` (the ``r`` key) is the only refetch after that.

Checkout rides the SAME worker behind a ``GitOpsController``-style ``_busy``
single-flight gate (reads are never gated) and re-emits that controller's
exact signal contract — ``operationStarted(op)`` / ``operationFinished(op,
ok, message)`` — so Main.qml's toast and AppController's post-op refresh path
reuse verbatim. Trade-off: a slow checkout briefly queues a list/detail read
behind it; acceptable for a surface the user is about to navigate away from.
"""

from __future__ import annotations

import gc
import json
import logging
import queue
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from PySide6.QtCore import (
    Property,
    QAbstractListModel,
    QModelIndex,
    QObject,
    Qt,
    Signal,
    Slot,
)

from .git_subprocess import resolve_repo_root

log = logging.getLogger(__name__)

# Subprocess timeouts (seconds). List/detail are network-bound reads; checkout
# additionally moves the working tree (fetch + branch switch), so it gets the
# transport-op budget GitOpsController uses for pull/push.
_LIST_TIMEOUT_SEC = 30.0
_DETAIL_TIMEOUT_SEC = 30.0
_CHECKOUT_TIMEOUT_SEC = 120.0

# One page of PRs. No paging UI in v1 — 50 open PRs is beyond this IDE's
# realistic per-repo scale; revisit with a "load more" if it ever isn't.
_LIST_LIMIT = 50

# The --json field lists, kept as module constants so the fetch argv and any
# parser expectations can't drift apart. Verified against gh 2.96.
_LIST_FIELDS = (
    "number,title,author,createdAt,updatedAt,state,headRefName,"
    "isDraft,reviewDecision,url"
)
_DETAIL_FIELDS = (
    "number,title,body,state,author,headRefName,baseRefName,isDraft,"
    "createdAt,mergedAt,reviewDecision,url,additions,deletions,changedFiles,"
    "comments,reviews"
)

# Op kind carried in operationStarted/operationFinished (read by QML/toast).
_OP_CHECKOUT = "checkout"

# Worker request kinds. The repo root AND the state filter travel WITH the
# request so the worker never reads mutable GUI state mid-fetch and the apply
# step can race-guard against the live values.
_REQ_LIST = "list"
_REQ_DETAIL = "detail"
_STOP = None  # sentinel pushed by stop() to unblock the worker's queue.get()

# Relative-time thresholds (seconds), coarsest-first at apply time.
_MINUTE = 60
_HOUR = 60 * _MINUTE
_DAY = 24 * _HOUR
_WEEK = 7 * _DAY
_MONTH = 30 * _DAY
_YEAR = 365 * _DAY


# ---------------------------------------------------------------------------
# Pure helpers — no subprocess, no Qt, no I/O. Unit-tested directly.
# ---------------------------------------------------------------------------


def relative_time(iso: str, now: datetime) -> str:
    """Render an ISO-8601 timestamp as a compact "3d ago" style string.

    ``now`` is injected (never read from the clock here) so tests pin it.
    Unparseable/empty input degrades to ``""`` rather than raising — gh's
    timestamps are well-formed, but a malformed record must not kill a scan.
    """
    if not iso:
        return ""
    try:
        then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    seconds = int((now - then).total_seconds())
    if seconds < _MINUTE:
        return "just now"
    if seconds < _HOUR:
        return f"{seconds // _MINUTE}m ago"
    if seconds < _DAY:
        return f"{seconds // _HOUR}h ago"
    if seconds < _WEEK:
        return f"{seconds // _DAY}d ago"
    if seconds < _MONTH:
        return f"{seconds // _WEEK}w ago"
    if seconds < _YEAR:
        return f"{seconds // _MONTH}mo ago"
    return f"{seconds // _YEAR}y ago"


def classify_gh_failure(rc: int, stderr: str) -> str:
    """Map a failed ``gh`` invocation to a human-actionable message.

    The interesting failures are environmental (gh missing, unauthenticated,
    repo has no GitHub remote) — each gets a clear sentence the list pane can
    show verbatim. Anything else degrades to gh's own stderr tail.
    """
    low = stderr.lower()
    if rc == -1 and "could not run gh" in low:
        return "GitHub CLI (gh) is not installed."
    if rc == -1 and "timed out" in low:
        return stderr.strip()
    if "gh auth login" in low or "authentication" in low or "auth status" in low:
        return "gh is not authenticated — run `gh auth login`."
    if (
        "no git remotes" in low
        or "could not determine base repo" in low
        or "not a git repository" in low
        or "no default remote" in low
    ):
        return "No GitHub remote for this repository."
    return _tail_lines(stderr, 5) or "gh command failed."


def parse_concatenated_json_arrays(text: str) -> list[dict]:
    """Parse ``gh api --paginate`` output: CONCATENATED JSON arrays.

    On an array endpoint, ``--paginate`` emits one JSON array PER PAGE,
    back-to-back (``[...][...]``) — NOT a single merged array — so a plain
    ``json.loads`` fails on any multi-page response. A ``raw_decode`` loop
    consumes the stream document by document and flattens the arrays.
    Non-list documents and trailing garbage are skipped with a warning.
    """
    items: list[dict] = []
    decoder = json.JSONDecoder()
    pos = 0
    length = len(text)
    while pos < length:
        while pos < length and text[pos].isspace():
            pos += 1
        if pos >= length:
            break
        try:
            doc, end = decoder.raw_decode(text, pos)
        except ValueError:
            log.warning("unparseable trailing gh api output at offset %d", pos)
            break
        if isinstance(doc, list):
            items.extend(item for item in doc if isinstance(item, dict))
        else:
            log.warning("gh api page was not a JSON array — skipped")
        pos = end
    return items


@dataclass(slots=True, frozen=True)
class PrRow:
    """One pull request, normalized for the list model."""

    number: int
    title: str
    author_login: str
    created_at_iso: str
    updated_at_iso: str
    relative_updated: str
    state: str  # OPEN / CLOSED / MERGED
    head_ref: str
    is_draft: bool
    review_decision: str  # APPROVED / CHANGES_REQUESTED / REVIEW_REQUIRED / ""
    url: str


@dataclass(slots=True, frozen=True)
class TimelineEntry:
    """One flattened conversation event, normalized for the timeline model."""

    kind: str  # "comment" | "review" | "inline"
    author: str  # login; "ghost" when the account was deleted
    timestamp_iso: str
    relative_time: str
    body: str
    review_state: str  # APPROVED / CHANGES_REQUESTED / COMMENTED / DISMISSED / ""
    location: str  # "path:line" for kind == "inline", else ""


def _login(obj: dict | None) -> str:
    """Author login with the deleted-account ("ghost") fallback."""
    return (obj or {}).get("login") or "ghost"


def parse_pr_list(text: str, now: datetime) -> list[PrRow]:
    """Parse ``gh pr list --json`` stdout into rows. ``[]`` on bad JSON.

    Malformed items are skipped with a warning rather than crashing the scan
    (same posture as ``parse_git_log``).
    """
    try:
        raw = json.loads(text or "[]")
    except ValueError:
        log.warning("gh pr list returned unparseable JSON")
        return []
    if not isinstance(raw, list):
        log.warning("gh pr list JSON was not an array")
        return []
    rows: list[PrRow] = []
    for item in raw:
        try:
            updated = str(item.get("updatedAt") or "")
            rows.append(
                PrRow(
                    number=int(item["number"]),
                    title=str(item.get("title") or ""),
                    author_login=_login(item.get("author")),
                    created_at_iso=str(item.get("createdAt") or ""),
                    updated_at_iso=updated,
                    relative_updated=relative_time(updated, now),
                    state=str(item.get("state") or ""),
                    head_ref=str(item.get("headRefName") or ""),
                    is_draft=bool(item.get("isDraft", False)),
                    review_decision=str(item.get("reviewDecision") or ""),
                    url=str(item.get("url") or ""),
                )
            )
        except (KeyError, TypeError, ValueError):
            log.warning("malformed gh pr list item skipped: %r", item)
    return rows


def flatten_timeline(
    detail: dict, inline: list[dict], now: datetime
) -> list[TimelineEntry]:
    """Merge a PR's three comment species into one chronological timeline.

    Sources (the "flatten, don't lose review signal" decision):
      - ``detail["comments"]``  — issue-style conversation comments.
      - ``detail["reviews"]``   — review summaries (APPROVED / CHANGES_REQUESTED
        / COMMENTED / DISMISSED). Empty-body COMMENTED reviews are SKIPPED:
        GitHub synthesizes one as a container for every inline-comment batch,
        so they carry zero information the inline entries don't.
      - ``inline``              — code-anchored review comments from the REST
        endpoint, rendered with a ``path:line`` location. ``line`` is null for
        comments on outdated diffs; fall back to ``original_line``, then "?".

    Sorted by ISO timestamp. All GitHub timestamps are Z-suffixed UTC, so the
    LEXICOGRAPHIC sort is chronological — pinned by a unit test; revisit if
    GitHub ever emits offsets. The sort is stable, so same-second entries keep
    source order (comments, then reviews, then inline).
    """
    entries: list[TimelineEntry] = []
    for c in detail.get("comments") or []:
        ts = str(c.get("createdAt") or "")
        entries.append(
            TimelineEntry(
                kind="comment",
                author=_login(c.get("author")),
                timestamp_iso=ts,
                relative_time=relative_time(ts, now),
                body=str(c.get("body") or ""),
                review_state="",
                location="",
            )
        )
    for r in detail.get("reviews") or []:
        state = str(r.get("state") or "")
        body = str(r.get("body") or "")
        if state == "COMMENTED" and not body.strip():
            continue
        ts = str(r.get("submittedAt") or "")
        entries.append(
            TimelineEntry(
                kind="review",
                author=_login(r.get("author")),
                timestamp_iso=ts,
                relative_time=relative_time(ts, now),
                body=body,
                review_state=state,
                location="",
            )
        )
    for ic in inline:
        ts = str(ic.get("created_at") or "")
        line = ic.get("line") or ic.get("original_line") or "?"
        entries.append(
            TimelineEntry(
                kind="inline",
                author=_login(ic.get("user")),
                timestamp_iso=ts,
                relative_time=relative_time(ts, now),
                body=str(ic.get("body") or ""),
                review_state="",
                location=f"{ic.get('path') or '?'}:{line}",
            )
        )
    entries.sort(key=lambda e: e.timestamp_iso)
    return entries


# Soft cap on a toast/error message — same rationale as GitOpsController.
_MESSAGE_CHAR_CAP = 400


def _tail_lines(text: str, max_lines: int) -> str:
    """Last ``max_lines`` non-empty lines, capped to a readable size."""
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ""
    msg = "\n".join(lines[-max_lines:])
    if len(msg) > _MESSAGE_CHAR_CAP:
        msg = msg[: _MESSAGE_CHAR_CAP - 1].rstrip() + "…"
    return msg


# ---------------------------------------------------------------------------


class GhPrController(QObject):
    """Async GitHub PR provider (list + detail + checkout) via the gh CLI.

    Threading model (mirrors ``GitLogController``):
      - The GUI thread constructs this, calls the slots, reads the properties.
      - A daemon worker drains ``_queue`` running ``gh`` subprocesses so the
        GUI never blocks on the network.
      - The worker stores results under ``_lock`` and emits parameterless
        change signals; receivers use ``Qt.QueuedConnection`` (§4 P2).

    Lifecycle: construct on the GUI thread, bind ``set_repo_root`` to the
    project anchor (same source as the other git controllers), ``stop()`` at
    teardown.
    """

    prListChanged = Signal()
    # listLoading / listError — kept separate from prListChanged so the empty/
    # error center-states don't re-bind on every list refresh and vice versa.
    listMetaChanged = Signal()
    stateFilterChanged = Signal()
    prDetailChanged = Signal()
    # Checkout feedback — the exact GitOpsController contract, so Main.qml's
    # toast and AppController._on_git_op_finished reuse verbatim.
    # op name only — fired on the GUI thread when a request is accepted.
    operationStarted = Signal(str)
    # (op, ok, message) — fired on the WORKER thread; connect QUEUED.
    operationFinished = Signal(str, bool, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)

        # `_repo_root` is the ASKED path (the project anchor); `_resolved_root`
        # is `git rev-parse --show-toplevel`'s answer, populated by the first
        # successful list fetch and used as the detail/checkout race-guard key.
        self._repo_root: str = ""
        self._resolved_root: str = ""

        # Which PRs the list shows. "open" (default) or "closed" (which, per
        # gh semantics, includes MERGED — the row's `state` distinguishes).
        # Travels with each list request for the apply-side race guard.
        self._state_filter: str = "open"

        self._prs: list[PrRow] = []
        self._list_loading: bool = False
        self._list_error: str = ""
        # ensure_loaded() one-shot latch: the (root, filter) pair the current
        # list contents were fetched for. None = nothing fetched yet.
        self._loaded_key: tuple[str, str] | None = None

        # Detail header fields, all published together under prDetailChanged.
        # _detail_number == 0 means "nothing loaded"; QML's stale-guard is
        # `controller.detailNumber === requestedNumber`.
        self._detail_number: int = 0
        self._detail_title: str = ""
        self._detail_body: str = ""
        self._detail_author: str = ""
        self._detail_state: str = ""
        self._detail_head_ref: str = ""
        self._detail_base_ref: str = ""
        self._detail_is_draft: bool = False
        self._detail_created_relative: str = ""
        self._detail_additions: int = 0
        self._detail_deletions: int = 0
        self._detail_changed_files: int = 0
        self._detail_url: str = ""
        self._detail_error: str = ""
        self._timeline: list[TimelineEntry] = []

        # Single-flight gate for CHECKOUT only (reads are never gated) —
        # deliberately NOT a QML property, per the GitOpsController note:
        # busy state surfaces via operationStarted/operationFinished.
        self._busy: bool = False

        # Guards every field above against worker/GUI concurrent access.
        self._lock = threading.Lock()

        self._queue: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()
        self._worker = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="gh-pr-worker",
        )
        self._worker.start()

    # -- QML-facing properties ----------------------------------------------

    @Property(str, notify=stateFilterChanged)
    def stateFilter(self) -> str:
        """Which PRs the list shows: ``"open"`` or ``"closed"``."""
        with self._lock:
            return self._state_filter

    @Property(bool, notify=listMetaChanged)
    def listLoading(self) -> bool:
        """True while a list fetch is in flight (drives the loading state)."""
        with self._lock:
            return self._list_loading

    @Property(str, notify=listMetaChanged)
    def listError(self) -> str:
        """Human-readable list-fetch failure (gh missing / auth / no remote)."""
        with self._lock:
            return self._list_error

    @Property(int, notify=prDetailChanged)
    def detailNumber(self) -> int:
        """PR number the detail fields currently hold (0 = none loaded)."""
        with self._lock:
            return self._detail_number

    @Property(str, notify=prDetailChanged)
    def detailTitle(self) -> str:
        with self._lock:
            return self._detail_title

    @Property(str, notify=prDetailChanged)
    def detailBody(self) -> str:
        with self._lock:
            return self._detail_body

    @Property(str, notify=prDetailChanged)
    def detailAuthor(self) -> str:
        with self._lock:
            return self._detail_author

    @Property(str, notify=prDetailChanged)
    def detailState(self) -> str:
        with self._lock:
            return self._detail_state

    @Property(str, notify=prDetailChanged)
    def detailHeadRef(self) -> str:
        with self._lock:
            return self._detail_head_ref

    @Property(str, notify=prDetailChanged)
    def detailBaseRef(self) -> str:
        with self._lock:
            return self._detail_base_ref

    @Property(bool, notify=prDetailChanged)
    def detailIsDraft(self) -> bool:
        with self._lock:
            return self._detail_is_draft

    @Property(str, notify=prDetailChanged)
    def detailCreatedRelative(self) -> str:
        with self._lock:
            return self._detail_created_relative

    @Property(int, notify=prDetailChanged)
    def detailAdditions(self) -> int:
        with self._lock:
            return self._detail_additions

    @Property(int, notify=prDetailChanged)
    def detailDeletions(self) -> int:
        with self._lock:
            return self._detail_deletions

    @Property(int, notify=prDetailChanged)
    def detailChangedFiles(self) -> int:
        with self._lock:
            return self._detail_changed_files

    @Property(str, notify=prDetailChanged)
    def detailUrl(self) -> str:
        with self._lock:
            return self._detail_url

    @Property(str, notify=prDetailChanged)
    def detailError(self) -> str:
        """Human-readable detail-fetch failure for the requested PR."""
        with self._lock:
            return self._detail_error

    def pr_rows(self) -> list[PrRow]:
        """Snapshot the PR list for the model (GUI thread, under lock)."""
        with self._lock:
            return list(self._prs)

    def timeline_entries(self) -> list[TimelineEntry]:
        """Snapshot the timeline for the model (GUI thread, under lock)."""
        with self._lock:
            return list(self._timeline)

    # -- Wiring (Python side) ------------------------------------------------

    def set_repo_root(self, value: str) -> None:
        """Point the PR surface at a new repo root. Idempotent on equal values.

        Clears everything synchronously (list, detail, errors, the
        ensure_loaded latch) and resets the filter to "open" — the previous
        project's PRs must never show for the new one. Deliberately does NOT
        enqueue a fetch: PR data is lazy (``ensure_loaded`` on tab entry), so
        a project switch costs no network until the tab is visited.
        """
        with self._lock:
            if value == self._repo_root:
                return
            self._repo_root = value
            self._resolved_root = ""
            filter_was_closed = self._state_filter != "open"
            self._state_filter = "open"
            self._prs = []
            self._list_loading = False
            self._list_error = ""
            self._loaded_key = None
            self._clear_detail_locked()
        if filter_was_closed:
            self.stateFilterChanged.emit()
        self.prListChanged.emit()
        self.listMetaChanged.emit()
        self.prDetailChanged.emit()

    def _clear_detail_locked(self) -> None:
        """Reset every detail field + the timeline. Caller holds ``_lock``."""
        self._detail_number = 0
        self._detail_title = ""
        self._detail_body = ""
        self._detail_author = ""
        self._detail_state = ""
        self._detail_head_ref = ""
        self._detail_base_ref = ""
        self._detail_is_draft = False
        self._detail_created_relative = ""
        self._detail_additions = 0
        self._detail_deletions = 0
        self._detail_changed_files = 0
        self._detail_url = ""
        self._detail_error = ""
        self._timeline = []

    # -- QML-facing slots -----------------------------------------------------

    @Slot()
    def refresh(self) -> None:
        """Re-fetch the PR list for the current root + filter (the ``r`` key)."""
        with self._lock:
            root = self._repo_root
            state_filter = self._state_filter
            if not root:
                return
            self._list_loading = True
        self.listMetaChanged.emit()
        self._queue.put((_REQ_LIST, root, state_filter))

    @Slot()
    def ensure_loaded(self) -> None:
        """Fetch the list once per (root, filter) — the tab-entry lazy load.

        A no-op when the current list already belongs to this key, so
        re-entering the tab never re-hits the network; ``refresh()`` is the
        only deliberate refetch (manual-freshness decision).
        """
        with self._lock:
            key = (self._repo_root, self._state_filter)
            if not self._repo_root or self._loaded_key == key:
                return
        self.refresh()

    @Slot()
    def toggle_state_filter(self) -> None:
        """Flip open ↔ closed and fetch the other page (lazy, no cache)."""
        with self._lock:
            self._state_filter = "closed" if self._state_filter == "open" else "open"
            # Clear synchronously so open PRs are never shown under the
            # "closed" header (and vice versa) while the fetch runs.
            self._prs = []
            self._list_error = ""
            self._loaded_key = None
        self.stateFilterChanged.emit()
        self.prListChanged.emit()
        self.listMetaChanged.emit()
        self.refresh()

    @Slot(int)
    def request_detail(self, number: int) -> None:
        """Load one PR's detail + flattened timeline (async, Enter-only).

        Deliberately never fired per j/k step — each detail load is two gh
        subprocesses (``pr view`` + the REST inline-comments call), so the
        list navigates locally and only an explicit open costs network.
        """
        if number <= 0:
            return
        with self._lock:
            resolved = self._resolved_root
            # A repeated Enter on the already-loaded PR is free.
            if self._detail_number == number and not self._detail_error:
                return
        if resolved:
            self._queue.put((_REQ_DETAIL, resolved, number))

    @Slot(int)
    def checkout(self, number: int) -> None:
        """Check out a PR's branch locally (``gh pr checkout``).

        Single-flight like GitOpsController: a second checkout while one is
        in flight is dropped (the running toast already says what's
        happening). Reads are never gated by ``_busy``.
        """
        if number <= 0:
            return
        with self._lock:
            if self._busy:
                return
            root = self._resolved_root or self._repo_root
            if root:
                self._busy = True
        if not root:
            self.operationFinished.emit(
                _OP_CHECKOUT, False, "No git repository for this project."
            )
            return
        # Accepted: announce "running" (GUI thread) and hand off to the worker.
        self.operationStarted.emit(_OP_CHECKOUT)
        self._queue.put((_OP_CHECKOUT, root, number))

    # -- Lifecycle -------------------------------------------------------------

    def stop(self) -> None:
        """Signal the worker to exit and join it (≤2s). Idempotent.

        Short join budget on purpose (matches GitOpsController): stop() runs
        on the GUI thread during shutdown, so a wedged network call must not
        hold IDE-close for the full op timeout — the daemon worker is reaped
        by the OS on process exit.
        """
        if self._stop_event.is_set():
            return
        self._stop_event.set()
        self._queue.put(_STOP)
        self._worker.join(timeout=2.0)

    # -- Worker thread -----------------------------------------------------------

    def _run_loop(self) -> None:
        """Worker main: drain the request queue until stopped. Never raises."""
        while not self._stop_event.is_set():
            request = self._queue.get()
            if request is _STOP or self._stop_event.is_set():
                return
            try:
                kind = request[0]
                if kind == _REQ_LIST:
                    self._do_list(asked_root=request[1], state_filter=request[2])
                elif kind == _REQ_DETAIL:
                    self._do_detail(resolved_root=request[1], number=request[2])
                elif kind == _OP_CHECKOUT:
                    self._do_checkout(root=request[1], number=request[2])
            except Exception:  # noqa: BLE001
                # The worker must NEVER crash — a dead daemon thread would
                # leave every future request stuck in the queue forever (and
                # `_busy` stuck True after a checkout).
                log.exception("gh-pr worker request failed: %r", request)
                if request[0] == _OP_CHECKOUT:
                    with self._lock:
                        self._busy = False
                    gc.disable()
                    try:
                        self.operationFinished.emit(
                            _OP_CHECKOUT,
                            False,
                            "Internal error running gh — see the IDE log.",
                        )
                    finally:
                        gc.enable()

    def _do_list(self, *, asked_root: str, state_filter: str) -> None:
        """Resolve the repo, fetch one PR page, apply under the race guard."""
        resolved = resolve_repo_root(asked_root)
        rows: list[PrRow] = []
        error = ""
        if not resolved:
            error = "Not a git repository."
        else:
            rc, out, err = self._run_gh(
                resolved,
                [
                    "pr",
                    "list",
                    "--state",
                    state_filter,
                    "--limit",
                    str(_LIST_LIMIT),
                    "--json",
                    _LIST_FIELDS,
                ],
                timeout=_LIST_TIMEOUT_SEC,
            )
            if rc != 0:
                error = classify_gh_failure(rc, err)
            else:
                rows = parse_pr_list(out, datetime.now(timezone.utc))

        with self._lock:
            # Race guard: a project switch OR a filter toggle may have landed
            # while gh ran; the newer request re-fetches, so drop this one.
            if asked_root != self._repo_root or state_filter != self._state_filter:
                return
            self._resolved_root = resolved
            self._prs = rows
            self._list_error = error
            self._list_loading = False
            self._loaded_key = None if error else (asked_root, state_filter)

        # gc.disable around the cross-thread emits — gotcha #10.
        gc.disable()
        try:
            self.prListChanged.emit()
            self.listMetaChanged.emit()
        finally:
            gc.enable()

    def _do_detail(self, *, resolved_root: str, number: int) -> None:
        """Fetch one PR's detail + inline comments, flatten, publish."""
        rc, out, err = self._run_gh(
            resolved_root,
            ["pr", "view", str(number), "--json", _DETAIL_FIELDS],
            timeout=_DETAIL_TIMEOUT_SEC,
        )
        now = datetime.now(timezone.utc)
        detail: dict = {}
        error = ""
        timeline: list[TimelineEntry] = []
        if rc != 0:
            error = classify_gh_failure(rc, err)
        else:
            try:
                parsed = json.loads(out)
                detail = parsed if isinstance(parsed, dict) else {}
            except ValueError:
                detail = {}
            if not detail:
                error = "Could not parse the PR detail from gh."

        if not error:
            # Inline review comments ride a second, REST call. gh substitutes
            # {owner}/{repo} from the cwd repo's remote. A failure here
            # DEGRADES (timeline without inline comments) rather than blanking
            # the pane — the conversation is still worth showing.
            inline: list[dict] = []
            rc2, out2, err2 = self._run_gh(
                resolved_root,
                [
                    "api",
                    f"repos/{{owner}}/{{repo}}/pulls/{number}/comments?per_page=100",
                    "--paginate",
                ],
                timeout=_DETAIL_TIMEOUT_SEC,
            )
            if rc2 == 0:
                inline = parse_concatenated_json_arrays(out2)
            else:
                log.warning(
                    "inline review comments fetch failed for PR #%d: %s",
                    number,
                    _tail_lines(err2, 2),
                )
            timeline = flatten_timeline(detail, inline, now)

        with self._lock:
            # Same race guard as the log controller's diff path: a project
            # switch mid-fetch must not land the old repo's PR in the pane.
            if resolved_root != self._resolved_root:
                return
            self._detail_number = number
            self._detail_error = error
            self._detail_title = str(detail.get("title") or "")
            self._detail_body = str(detail.get("body") or "")
            self._detail_author = _login(detail.get("author"))
            self._detail_state = str(detail.get("state") or "")
            self._detail_head_ref = str(detail.get("headRefName") or "")
            self._detail_base_ref = str(detail.get("baseRefName") or "")
            self._detail_is_draft = bool(detail.get("isDraft", False))
            self._detail_created_relative = relative_time(
                str(detail.get("createdAt") or ""), now
            )
            self._detail_additions = int(detail.get("additions") or 0)
            self._detail_deletions = int(detail.get("deletions") or 0)
            self._detail_changed_files = int(detail.get("changedFiles") or 0)
            self._detail_url = str(detail.get("url") or "")
            self._timeline = timeline

        gc.disable()
        try:
            self.prDetailChanged.emit()
        finally:
            gc.enable()

    def _do_checkout(self, *, root: str, number: int) -> None:
        """Run ``gh pr checkout`` and emit the operation result."""
        ok = False
        message = ""
        try:
            rc, out, err = self._run_gh(
                root,
                ["pr", "checkout", str(number)],
                timeout=_CHECKOUT_TIMEOUT_SEC,
            )
            if rc == 0:
                # gh/git write the branch-switch summary to stderr.
                combined = "\n".join(s for s in (out, err) if s.strip())
                ok = True
                message = _tail_lines(combined, 3) or f"Checked out PR #{number}."
            else:
                message = classify_gh_failure(rc, err)
        finally:
            with self._lock:
                self._busy = False
            # gc.disable around the cross-thread emit — gotcha #10.
            gc.disable()
            try:
                self.operationFinished.emit(_OP_CHECKOUT, ok, message)
            finally:
                gc.enable()

    # -- Subprocess shell (worker thread only) -----------------------------------

    def _run_gh(
        self, cwd: str, args: list[str], *, timeout: float
    ) -> tuple[int, str, str]:
        """Run a gh subprocess. Returns ``(returncode, stdout, stderr)``.

        Returns ``(-1, "", <reason>)`` when gh can't be run at all (missing
        binary, timeout, OS error) so callers branch on ``rc != 0`` uniformly
        and ``classify_gh_failure`` maps the reason to a clear message.
        """
        try:
            proc = subprocess.run(
                ["gh", *args],
                cwd=cwd,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return (-1, "", f"gh {args[0]} timed out after {int(timeout)}s.")
        except (FileNotFoundError, OSError) as exc:
            log.debug("gh %s failed in %s: %s", args[0], cwd, exc)
            return (-1, "", f"Could not run gh: {exc}")
        return (
            proc.returncode,
            proc.stdout.decode("utf-8", errors="replace"),
            proc.stderr.decode("utf-8", errors="replace"),
        )


# ---------------------------------------------------------------------------


class GhPrListModel(QAbstractListModel):
    """Flat list of pull requests, projected from ``GhPrController``.

    Auto-refreshes on ``prListChanged`` (queued — the controller emits from
    its worker). Always a full reset: PR pages are small (≤50) and replaced
    wholesale, so append-aware splicing buys nothing here.
    """

    NumberRole = Qt.ItemDataRole.UserRole + 1
    TitleRole = Qt.ItemDataRole.UserRole + 2
    AuthorLoginRole = Qt.ItemDataRole.UserRole + 3
    RelativeUpdatedRole = Qt.ItemDataRole.UserRole + 4
    StateRole = Qt.ItemDataRole.UserRole + 5
    HeadRefNameRole = Qt.ItemDataRole.UserRole + 6
    IsDraftRole = Qt.ItemDataRole.UserRole + 7
    ReviewDecisionRole = Qt.ItemDataRole.UserRole + 8
    UrlRole = Qt.ItemDataRole.UserRole + 9

    countChanged = Signal()

    def __init__(
        self,
        controller: GhPrController,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._rows: list[PrRow] = []
        # queued: GhPrController worker → GhPrListModel GUI (§4 P2) — a direct
        # connection would run begin/endResetModel on the worker thread.
        self._controller.prListChanged.connect(
            self._refresh,
            Qt.ConnectionType.QueuedConnection,
        )

    def roleNames(self) -> dict[int, bytes]:
        return {
            self.NumberRole: b"number",
            self.TitleRole: b"title",
            self.AuthorLoginRole: b"authorLogin",
            self.RelativeUpdatedRole: b"relativeUpdated",
            self.StateRole: b"state",
            self.HeadRefNameRole: b"headRefName",
            self.IsDraftRole: b"isDraft",
            self.ReviewDecisionRole: b"reviewDecision",
            self.UrlRole: b"url",
        }

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008 — QModelIndex() default required by Qt; ARG002 — parent unused per QAbstractListModel contract
        return len(self._rows)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self._rows):
            return None
        row = self._rows[index.row()]
        if role == self.NumberRole:
            return row.number
        if role == self.TitleRole:
            return row.title
        if role == self.AuthorLoginRole:
            return row.author_login
        if role == self.RelativeUpdatedRole:
            return row.relative_updated
        if role == self.StateRole:
            return row.state
        if role == self.HeadRefNameRole:
            return row.head_ref
        if role == self.IsDraftRole:
            return row.is_draft
        if role == self.ReviewDecisionRole:
            return row.review_decision
        if role == self.UrlRole:
            return row.url
        return None

    @Property(int, notify=countChanged)
    def count(self) -> int:
        """Row count as a bindable property (tab badge, empty states)."""
        return len(self._rows)

    @Slot()
    def _refresh(self) -> None:
        """Rebuild rows from the controller (GUI thread — queued signal)."""
        new_rows = self._controller.pr_rows()
        if new_rows == self._rows:
            return
        old_len = len(self._rows)
        self.beginResetModel()
        self._rows = new_rows
        self.endResetModel()
        if len(new_rows) != old_len:
            self.countChanged.emit()


class GhPrTimelineModel(QAbstractListModel):
    """Flattened conversation timeline of the loaded PR detail.

    Auto-refreshes on ``prDetailChanged`` (queued). Full reset per detail
    load — the timeline is replaced wholesale with each PR.
    """

    KindRole = Qt.ItemDataRole.UserRole + 1
    AuthorRole = Qt.ItemDataRole.UserRole + 2
    RelativeTimeRole = Qt.ItemDataRole.UserRole + 3
    BodyRole = Qt.ItemDataRole.UserRole + 4
    ReviewStateRole = Qt.ItemDataRole.UserRole + 5
    LocationRole = Qt.ItemDataRole.UserRole + 6

    countChanged = Signal()

    def __init__(
        self,
        controller: GhPrController,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._rows: list[TimelineEntry] = []
        # queued: GhPrController worker → GhPrTimelineModel GUI (§4 P2)
        self._controller.prDetailChanged.connect(
            self._refresh,
            Qt.ConnectionType.QueuedConnection,
        )

    def roleNames(self) -> dict[int, bytes]:
        return {
            self.KindRole: b"kind",
            self.AuthorRole: b"author",
            self.RelativeTimeRole: b"relativeTime",
            self.BodyRole: b"body",
            self.ReviewStateRole: b"reviewState",
            self.LocationRole: b"location",
        }

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008 — QModelIndex() default required by Qt; ARG002 — parent unused per QAbstractListModel contract
        return len(self._rows)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self._rows):
            return None
        row = self._rows[index.row()]
        if role == self.KindRole:
            return row.kind
        if role == self.AuthorRole:
            return row.author
        if role == self.RelativeTimeRole:
            return row.relative_time
        if role == self.BodyRole:
            return row.body
        if role == self.ReviewStateRole:
            return row.review_state
        if role == self.LocationRole:
            return row.location
        return None

    @Property(int, notify=countChanged)
    def count(self) -> int:
        """Row count as a bindable property (empty-timeline state)."""
        return len(self._rows)

    @Slot()
    def _refresh(self) -> None:
        """Rebuild rows from the controller (GUI thread — queued signal)."""
        new_rows = self._controller.timeline_entries()
        if new_rows == self._rows:
            return
        old_len = len(self._rows)
        self.beginResetModel()
        self._rows = new_rows
        self.endResetModel()
        if len(new_rows) != old_len:
            self.countChanged.emit()
