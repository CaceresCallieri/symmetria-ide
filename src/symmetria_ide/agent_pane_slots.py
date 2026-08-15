"""Append-only registry of terminal-agent pane slots.

This model exists for ONE reason: it is what removes the five-agent cap
without killing live agent CLIs.

`Main.qml`'s agent surface used to be `Repeater { model: controller.maxAgentSlots }`
— a Repeater over a plain integer. Qt destroys and recreates EVERY delegate
when that integer changes, and each delegate owns a `QMLTermSession`, whose
destructor reaps the agent process. Measured 2026-08-15: growing the integer
model from 3 to 8 destroyed all three live panes. The same probe over an
append-only `QAbstractListModel` grew 3 → 9 with zero destructions and nine
live panes. Method + configuration: `tests/qml_harness/pane_growth_probe.py`
and `.claude/memory/reference/qt-pyside/append_only_pane_registry.md`.

The safety of a list model here rests entirely on the append-only discipline,
which is why this is a SEPARATE model from any list of *active* agents:

- Rows are only ever APPENDED. A row is never removed, never moved, and the
  whole model is never reset while the app runs.
- Closing an agent FREES its slot: the row stays and its `active` role flips
  to False, which deactivates exactly that one Loader.
- Row `i` therefore always describes slot `i + 1`, contiguously. `Main.qml`
  reaches panes via `agentSlotRepeater.itemAt(slot - 1)` in three places, and
  that arithmetic is only true because rows never leave and never reorder.

CLAUDE.md's standing warning — "do NOT convert to a list-model Repeater" —
remains correct for a model of ACTIVE agents, which reorders on focus and
deletes on close. It does not apply to this registry, which does neither.
"""

from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import QAbstractListModel, QModelIndex, QObject, Qt
from PySide6.QtQml import QmlElement

QML_IMPORT_NAME = "Symmetria.Ide"
QML_IMPORT_MAJOR_VERSION = 1


@QmlElement
class AgentPaneSlotModel(QAbstractListModel):
    """One row per pane slot ever allocated this run: `{slot, active}`.

    The single coupling between this registry and everything else is the
    integer slot number. Nothing about an agent — title, harness, activity,
    worktree — lives here; those ride the per-slot `QVariantList` properties
    on `AppController`, indexed `slot - 1`.
    """

    SlotRole = Qt.ItemDataRole.UserRole + 1
    ActiveRole = Qt.ItemDataRole.UserRole + 2

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        # Index i ↔ slot i + 1; the value is that slot's `active` flag.
        # A plain list of bools IS the whole registry — the slot number is
        # positional, which is what makes "rows never move" checkable by
        # reading this file rather than by auditing every caller.
        self._active: list[bool] = []

    # ------------------------------------------------------------------
    # QAbstractListModel
    # ------------------------------------------------------------------

    def rowCount(self, parent: QModelIndex | None = None) -> int:  # noqa: N802
        if parent is not None and parent.isValid():
            return 0
        return len(self._active)

    def roleNames(self) -> dict:  # noqa: N802
        return {
            self.SlotRole: b"slot",
            self.ActiveRole: b"active",
        }

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._active):
            return None
        if role == self.SlotRole:
            return index.row() + 1
        if role == self.ActiveRole:
            return self._active[index.row()]
        return None

    # ------------------------------------------------------------------
    # Registry maintenance
    # ------------------------------------------------------------------

    def sync(self, occupied: Iterable[int]) -> None:
        """Make the registry describe `occupied` — grow, then flip.

        `AppController._term_agents` is the source of truth for which slots
        hold an agent; this projects that set onto the registry. Taking the
        whole set (rather than per-slot `allocate`/`release` calls) is what
        makes drift between the two structurally impossible: there is one
        funnel, and it recomputes rather than accumulates.

        Growth appends through `beginInsertRows` so existing delegates are
        untouched (the measurement this whole model exists for). Occupancy
        changes emit `dataChanged` with an EXPLICIT role list — an empty role
        list forces QML to re-evaluate every binding on the delegate (gotcha
        #3), which for a pane delegate means re-reading the spawn argv.
        """
        wanted = {int(slot) for slot in occupied}
        high_water = max(wanted, default=0)
        if high_water > len(self._active):
            first = len(self._active)
            last = high_water - 1
            self.beginInsertRows(QModelIndex(), first, last)
            # Appended already in their FINAL state: the Repeater creates each
            # delegate inside endInsertRows, and a row that arrived False would
            # bind an inactive Loader that only a later notify repairs — a
            # visible flicker at best, and a dropped spawn if that notify ever
            # stops firing.
            self._active.extend((row + 1) in wanted for row in range(first, high_water))
            self.endInsertRows()
        for row in range(len(self._active)):
            active = (row + 1) in wanted
            if self._active[row] == active:
                continue
            self._active[row] = active
            position = self.index(row, 0)
            self.dataChanged.emit(position, position, [self.ActiveRole])

    def active_flags(self) -> list[bool]:
        """Per-slot occupancy indexed `slot - 1` — the shape QML reads.

        Handed out as a copy: the caller projects it into a `QVariantList`
        property, and a shared list would let a consumer mutate the registry
        by writing into what looks like a value.
        """
        return list(self._active)

    def is_active(self, slot: int) -> bool:
        """Whether `slot` currently holds a live pane (False for unknown slots)."""
        if not 1 <= slot <= len(self._active):
            return False
        return self._active[slot - 1]
