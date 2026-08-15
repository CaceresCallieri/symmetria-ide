"""The thread rail's list model — one row per agent conversation.

This is the SECOND list model of agents in the window, and the distinction
between the two is the single thing a future refactor must not lose:

- `agent_pane_slots.AgentPaneSlotModel` is an APPEND-ONLY registry of pane
  slots. Rows never move and never leave, because each row's delegate owns a
  `QMLTermSession` whose destructor reaps the agent CLI.
- This model is an ORDINARY list. Rows insert, move and leave as agents come
  and go, are re-ordered, and (from Phase 3) sleep. Nothing here owns a
  process, so churn is free.

Pointing `Main.qml`'s agent `Repeater` at THIS model would kill live agent
CLIs on every reorder. See `agent_pane_slots.py` for the measurement.

The only link from a row back to a pane is the integer `slot` role, where 0
means "no live pane" (a dead thread, from Phase 3 onward). Everything a row
needs about a LIVE agent — activity, browser ownership, coordination, STT — is
read by the QML delegate from the per-slot `QVariantList` properties on
`AppController`, indexed `slot - 1`. Those are deliberately NOT mirrored into
`ThreadRow`: a second source of truth for activity would need a row rewrite on
every sparkle tick, and would drift the moment one of those six signals changes
shape.

TWO fields are the stated exception — `title` and `worktree`. They are carried
on the row because a DEAD thread still has to show them and has no slot left to
index, so a history row is the only place they can live. The delegate reads the
per-slot list while `slot > 0` and falls back to the row only when it is 0, so
the live path keeps exactly one source of truth and the row copy is what
survives the agent. Nothing else earns that treatment: activity, browser and
coordination state simply cease to exist when the CLI does.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, replace

from PySide6.QtCore import QAbstractListModel, QModelIndex, QObject, Qt, Slot
from PySide6.QtQml import QmlElement

from .agent_thread_store import thread_key

QML_IMPORT_NAME = "Symmetria.Ide"
QML_IMPORT_MAJOR_VERSION = 1


@dataclass(slots=True, frozen=True)
class ThreadRow:
    """One agent conversation, live or (from Phase 3) dead.

    `thread_id` is the durable identity `"<harness>:<session_id>"`. A live
    agent that has not revealed a session id yet falls back to `"live:<slot>"`
    — the row is real and focusable, it just cannot be resumed once it dies.
    """

    thread_id: str
    harness: str
    session_id: str
    title: str
    updated_at: int
    work_root: str
    worktree: str
    slot: int


class ThreadHistory:
    """Conversations whose CLI is gone but which can still be resumed.

    Qt-free and controller-free on purpose: it is a pure map from slot-record
    dicts to `ThreadRow`s, so the rail's ordering rule — the one thing a reader
    of this feature actually has to find — is not buried inside a
    nine-thousand-line controller. It lives beside `ThreadRow` because that is
    the only type it speaks, and it is where the on-disk index's rows join the
    same merge in a later phase.

    Two sources, kept apart until `merge`:

    - `_rows` — threads THIS run watched leave the pool (`record`).
    - `_indexed` — threads read back off disk by the harness index
      (`set_index`), which is what makes a conversation survive a restart.

    Kept apart because they are replaced on different schedules: an index scan
    re-publishes one harness's whole history at once, and folding it into the
    same dict would either drop this run's own departures or resurrect rows the
    index has since stopped returning.
    """

    def __init__(self) -> None:
        self._rows: dict[str, ThreadRow] = {}
        # harness -> identity -> row. Per harness because each reader publishes
        # independently (claude finishes long before opencode's CLI spawn), so
        # a claude result must not clear opencode's rows.
        self._indexed: dict[str, dict[str, ThreadRow]] = {}

    def record(self, record: dict, work_root: str) -> None:
        """Keep a leaving agent's conversation, at `slot = 0`.

        Fed from the ONE funnel every local departure passes through, so "the
        row survives the CLI" cannot depend on whether the agent was slept,
        closed, or exited on its own.

        Two records are deliberately NOT kept:

        - a VPS slot: the server's hub owns its identity, and this IDE never
          published it;
        - a slot with no `session_id`: nothing can resume it, so a row would
          be an entry that fails the moment it is clicked.
        """
        if record.get("location", "local") != "local":
            return
        harness = str(record.get("harness") or "claude")
        session_id = str(record.get("session_id") or "")
        if not session_id:
            return
        identity = thread_key(harness, session_id)
        self._rows[identity] = ThreadRow(
            thread_id=identity,
            harness=harness,
            session_id=session_id,
            title=str(record.get("title") or ""),
            # When it STOPPED, not when it started: dead rows sort by recency
            # of use, and a long-running thread spawned this morning is more
            # recent than one spawned and closed an hour ago.
            updated_at=int(time.time()),
            work_root=work_root,
            worktree=str(record.get("worktree") or ""),
            slot=0,
        )

    def set_index(self, harness: str, rows: Sequence[ThreadRow]) -> None:
        """Replace one harness's on-disk history with a finished scan's rows.

        Wholesale replacement rather than a merge: the scan reads every
        transcript that still exists, so its result IS that harness's history —
        anything missing from it was pruned and can no longer be resumed.
        """
        self._indexed[harness] = {row.thread_id: row for row in rows}

    def merge(self, live: Sequence[ThreadRow]) -> list[ThreadRow]:
        """`live` rows first, then the dead ones, most recently ended first.

        A dead row is dropped whenever the same conversation is live again
        (resuming re-spawns under the same identity): the live row IS that
        thread, and carrying both would list it twice, once with a pane and
        once without.

        Where a thread appears in BOTH dead sources, this run's own record
        wins: it watched the agent leave, so its `updated_at` is when the CLI
        actually stopped and its `work_root` came from the live write-tool
        follow rather than a backfill. Empty fields still fall back to the
        indexed twin — the index is the only place an `ai-title` exists, and a
        pane that never received a title would otherwise show a blank row.
        """
        live_ids = {row.thread_id for row in live}
        dead: dict[str, ThreadRow] = {}
        for harness_rows in self._indexed.values():
            dead.update(harness_rows)
        for identity, row in self._rows.items():
            dead[identity] = self._prefer_recorded(row, dead.get(identity))
        rows = [row for identity, row in dead.items() if identity not in live_ids]
        rows.sort(key=lambda row: row.updated_at, reverse=True)
        return list(live) + rows

    @staticmethod
    def _prefer_recorded(recorded: ThreadRow, indexed: ThreadRow | None) -> ThreadRow:
        """`recorded`, with anything it lacks filled in from the index."""
        if indexed is None:
            return recorded
        return replace(
            recorded,
            title=recorded.title or indexed.title,
            work_root=recorded.work_root or indexed.work_root,
            worktree=recorded.worktree or indexed.worktree,
        )


@QmlElement
class AgentThreadModel(QAbstractListModel):
    """The rail's rows, in display order.

    `set_rows` is the only mutator, and it DIFFS rather than resets. A reset
    would be far simpler to write and is wrong here for a concrete reason: the
    rail is a `ListView` holding the user's selection, and a live agent emits
    a title or an activity edge every few seconds, so a reset-per-update would
    throw the selection back to the top of the list while the user is reading
    it.
    """

    ThreadIdRole = Qt.ItemDataRole.UserRole + 1
    HarnessRole = Qt.ItemDataRole.UserRole + 2
    SessionIdRole = Qt.ItemDataRole.UserRole + 3
    TitleRole = Qt.ItemDataRole.UserRole + 4
    UpdatedAtRole = Qt.ItemDataRole.UserRole + 5
    WorkRootRole = Qt.ItemDataRole.UserRole + 6
    WorktreeRole = Qt.ItemDataRole.UserRole + 7
    SlotRole = Qt.ItemDataRole.UserRole + 8

    # role -> the `ThreadRow` field it exposes. One table drives `roleNames`,
    # `data` and the scoped `dataChanged` below, so a new field cannot be
    # added to two of the three and forgotten in the last.
    _ROLE_FIELDS: dict[int, bytes] = {
        ThreadIdRole: b"threadId",
        HarnessRole: b"harness",
        SessionIdRole: b"sessionId",
        TitleRole: b"title",
        UpdatedAtRole: b"updatedAt",
        WorkRootRole: b"workRoot",
        WorktreeRole: b"worktree",
        SlotRole: b"slot",
    }
    _ROLE_ATTRS: dict[int, str] = {
        ThreadIdRole: "thread_id",
        HarnessRole: "harness",
        SessionIdRole: "session_id",
        TitleRole: "title",
        UpdatedAtRole: "updated_at",
        WorkRootRole: "work_root",
        WorktreeRole: "worktree",
        SlotRole: "slot",
    }

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._rows: list[ThreadRow] = []

    # ------------------------------------------------------------------
    # QAbstractListModel
    # ------------------------------------------------------------------

    def rowCount(self, parent: QModelIndex | None = None) -> int:  # noqa: N802
        if parent is not None and parent.isValid():
            return 0
        return len(self._rows)

    def roleNames(self) -> dict:  # noqa: N802
        return dict(self._ROLE_FIELDS)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        attribute = self._ROLE_ATTRS.get(role)
        if attribute is None:
            return None
        return getattr(self._rows[index.row()], attribute)

    def rows(self) -> list[ThreadRow]:
        """The current rows, as a copy — the shape tests and callers read."""
        return list(self._rows)

    @Slot(int, result=int)
    def slot_at(self, row: int) -> int:
        """The pane slot at display position `row`; 0 for a dead or absent row.

        The rail's keyboard path resolves its selection through THIS rather
        than through `ListView.currentItem.slot`. Measured 2026-08-15 in an
        offscreen probe: after `set_rows` dropped a row, `currentIndex` had not
        yet been clamped and `currentItem` still pointed at the RECYCLED
        delegate of the removed row (`reuseItems: true`), so Enter dispatched
        the departed agent's slot. Reading the model cannot go stale, and an
        out-of-range row answers 0 — which the rail already treats as "not
        live, do nothing".
        """
        if not 0 <= row < len(self._rows):
            return 0
        return self._rows[row].slot

    @Slot(int, result=str)
    def thread_id_at(self, row: int) -> str:
        """The durable identity at display position `row`; "" when absent.

        The dead-row twin of `slot_at`, and read through the model for the
        same recycled-delegate reason documented there. "" is what the rail
        already treats as "nothing to resume".
        """
        if not 0 <= row < len(self._rows):
            return ""
        return self._rows[row].thread_id

    # ------------------------------------------------------------------
    # The single mutator
    # ------------------------------------------------------------------

    def set_rows(self, rows: Sequence[ThreadRow]) -> None:
        """Make the model describe `rows`, in three passes and no reset.

        Remove-then-align-then-update, rather than one clever pass: each of
        the three emits exactly the signal QML needs to keep its delegates and
        its selection, and each is separately checkable.
        """
        incoming = list(rows)
        self._remove_departed(incoming)
        self._align_order(incoming)
        for position, row in enumerate(incoming):
            self._apply_fields(position, row)

    def _remove_departed(self, incoming: list[ThreadRow]) -> None:
        """Drop rows no longer present, bottom-up so indices stay valid."""
        wanted = {row.thread_id for row in incoming}
        for position in range(len(self._rows) - 1, -1, -1):
            if self._rows[position].thread_id in wanted:
                continue
            self.beginRemoveRows(QModelIndex(), position, position)
            del self._rows[position]
            self.endRemoveRows()

    def _align_order(self, incoming: list[ThreadRow]) -> None:
        """Bring each position into agreement — move a survivor, or insert.

        Survivors are only ever searched for AT OR AFTER the target position,
        so a move always travels upward. That keeps the `beginMoveRows`
        destination outside `[source, source + 1]`, which Qt rejects.
        """
        for target, row in enumerate(incoming):
            if (
                target < len(self._rows)
                and self._rows[target].thread_id == row.thread_id
            ):
                continue
            source = -1
            for candidate in range(target, len(self._rows)):
                if self._rows[candidate].thread_id == row.thread_id:
                    source = candidate
                    break
            if source < 0:
                self.beginInsertRows(QModelIndex(), target, target)
                self._rows.insert(target, row)
                self.endInsertRows()
                continue
            self.beginMoveRows(QModelIndex(), source, source, QModelIndex(), target)
            self._rows.insert(target, self._rows.pop(source))
            self.endMoveRows()

    def _apply_fields(self, position: int, row: ThreadRow) -> None:
        """Update one row, emitting `dataChanged` scoped to what changed.

        The role list is EXPLICIT because an empty one forces QML to
        re-evaluate every binding on the delegate (gotcha #3) — here that
        means re-reading every per-slot indicator list on every title tick.
        """
        existing = self._rows[position]
        if existing == row:
            return
        changed = [
            role
            for role, attribute in self._ROLE_ATTRS.items()
            if getattr(existing, attribute) != getattr(row, attribute)
        ]
        self._rows[position] = row
        if not changed:
            return
        index = self.index(position, 0)
        self.dataChanged.emit(index, index, changed)
