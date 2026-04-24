"""Agent-pane event model: SessionModel for stream-json events.

Extracted here (rather than living in `app.py`) to mirror the
convention started by `cmdline_models.py` and `whichkey_models.py`:
each QML-facing model group lives in its own file, keeps `app.py`
lean, and registers its types via `@QmlElement` as an import-time
side effect. That side-effect import (with `noqa: F401`) is kept in
`app.py`, alongside an `_ = SessionModel` anchor in
`_register_qml_types` so linters cannot silently drop it (the same
second-layer protection the existing QML-registered modules use).

These models render the `claude -p --output-format stream-json`
event stream driven by `session_host.py`. See `docs/phases.md`
(Phase 2 placeholder spike) and the upcoming CLAUDE.md
`## The stream-json protocol` section for the routing detail.

Shape for the placeholder spike: one ListView row per event, with
partial coalescing for streaming assistant text. Turn grouping and
tool-call drill-in land after the spike — designing them against
guessed protocol vocabulary is exactly the waste the spike exists
to avoid.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import TypedDict

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QObject,
    Qt,
    Signal,
    Slot,
)
from PySide6.QtQml import QmlElement

QML_IMPORT_NAME = "Symmetria.Ide"
QML_IMPORT_MAJOR_VERSION = 1


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Payload typing
# ---------------------------------------------------------------------------
#
# Stream-json events have a `type` discriminator that decides which
# other fields to read. Rather than exhaustively type every subtype,
# we TypedDict-document only the envelope fields we route on, and
# carry the rest as free-form `dict`. Project-standards §1 P1 prefers
# TypedDict for RPC payloads — this is minimum-viable adherence, not
# a full schema. Adding richer subtype dicts is cheap later and
# doesn't require touching the model surface.


class StreamJsonEvent(TypedDict, total=False):
    """Envelope shared by every line on `claude -p` stream-json stdout.

    All keys are optional from Python's perspective — every event
    carries `type`, most carry `session_id`/`uuid`, and the rest is
    subtype-specific. Fields we don't read today stay unannotated and
    flow through the `AgentRow.raw` escape hatch.
    """

    type: str
    subtype: str
    uuid: str
    session_id: str
    message: dict
    event: dict
    parent_tool_use_id: str | None


# ---------------------------------------------------------------------------
# Row value object
# ---------------------------------------------------------------------------
#
# frozen + slots — project-standards §1 P1: reduces GC pressure
# (directly relevant to gotcha #10 — every tracked dict in a hot
# worker path is a GC root candidate on Python 3.14) and makes the
# row shape explicit. Partial-text extension uses `dataclasses.replace`
# to produce a new row and swap it into the backing list; mutable
# rows would be cheaper but would break the "rows are value objects"
# invariant the model exposes to delegates.


@dataclass(slots=True, frozen=True)
class AgentRow:
    """One stream-json event rendered as a flat ListView row.

    `kind` carries the raw stream-json event `type` (grep-able back
    to the protocol). `role` is an inferred attribution that QML
    delegates colour-map against `Theme.color.agent.*`. `text` is the
    human-readable body, possibly grown by partial-text coalescing
    while `partial=True`. `subtype` adds discriminator detail for
    system/result/tool events. `raw` keeps the original event dict so
    future richer delegates (tool drill-in, image rendering, etc.)
    have the full payload without requiring a model rewrite.
    """

    kind: str
    role: str
    text: str
    partial: bool
    subtype: str
    raw: dict


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


@QmlElement
class SessionModel(QAbstractListModel):
    """Flat list of stream-json events for the agent pane.

    **Shape — placeholder spike.** One row per event, with partial
    coalescing for streaming assistant text (see
    `_handle_stream_event`). Turn grouping and tool-call drill-in are
    deferred to a follow-up iteration; both need the real event
    cadence to be designed well.

    **Role semantics.** `apply()` maps the top-level `type`
    discriminator to an inferred `role` string the QML delegate uses
    to choose a `Theme` token. Unknown types land as empty-role rows
    so a protocol addition surfaces visibly rather than being
    silently dropped — useful during exploration.

    **Partial-text coalescing.** When a `stream_event` carrying a
    `content_block_delta` with `delta.type == "text_delta"` arrives,
    the most recent streaming assistant row's text is extended by the
    delta and `dataChanged` is emitted with an explicit role list
    scoped to `TextRole` (gotcha #3: empty role lists force full
    re-bind; scoped lists let QML only re-evaluate the one binding
    that actually changed). The finalised `assistant` event then
    appends a canonical row and clears the streaming coalesce — the
    streaming row stays in place with `partial=True` so a future
    delegate can visually distinguish the in-flight view from the
    final canonical text.

    **Thread affinity.** `apply()` runs on the GUI thread only. It is
    the `@Slot(dict)` wired to `SessionHost.event_received` via a
    queued cross-thread connection (project-standards §4 P0); the
    worker thread never calls it directly.
    """

    KindRole = Qt.ItemDataRole.UserRole + 1
    RoleRole = Qt.ItemDataRole.UserRole + 2
    TextRole = Qt.ItemDataRole.UserRole + 3
    PartialRole = Qt.ItemDataRole.UserRole + 4
    SubtypeRole = Qt.ItemDataRole.UserRole + 5
    RawRole = Qt.ItemDataRole.UserRole + 6

    # Emitted once after the host subprocess has fully closed. Scalar
    # state so delegates don't reshape when it fires — UI can bind
    # to this for a "session ended" affordance without any row
    # boundary games.
    hostClosed = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._rows: list[AgentRow] = []
        # Index of the in-progress assistant row (extended by
        # content_block_delta / text_delta events). `None` when no
        # stream is currently open. Any non-streaming event crossing
        # the row boundary resets this to `None` so the next
        # text_delta starts a fresh streaming row.
        self._streaming_row_index: int | None = None

    # --- Qt model overrides ---------------------------------------------

    def roleNames(self) -> dict[int, bytes]:
        return {
            self.KindRole: b"kind",
            self.RoleRole: b"role",
            self.TextRole: b"text",
            self.PartialRole: b"partial",
            self.SubtypeRole: b"subtype",
            self.RawRole: b"raw",
        }

    def rowCount(  # noqa: B008
        self,
        parent: QModelIndex = QModelIndex(),  # noqa: ARG002
    ) -> int:
        return len(self._rows)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self._rows):
            return None
        row = self._rows[index.row()]
        if role == self.KindRole:
            return row.kind
        if role == self.RoleRole:
            return row.role
        if role == self.TextRole:
            return row.text
        if role == self.PartialRole:
            return row.partial
        if role == self.SubtypeRole:
            return row.subtype
        if role == self.RawRole:
            return row.raw
        return None

    # --- Application API (GUI thread only) ------------------------------

    @Slot(dict)
    def apply(self, event: dict) -> None:
        """Ingest one stream-json event. GUI-thread only.

        Routes on the top-level `type` discriminator. Unknown types
        append an empty-role row rather than being silently dropped —
        during the placeholder spike this gives us a visible signal
        that the protocol shipped a new envelope we haven't mapped
        yet.
        """
        kind = str(event.get("type") or "")
        if kind == "stream_event":
            self._handle_stream_event(event)
            return
        # Any non-stream-event event closes the current streaming
        # coalesce so the next text_delta opens a fresh row.
        self._streaming_row_index = None
        if kind == "assistant":
            self._append(_row_from_assistant(event))
            return
        if kind == "user":
            self._append(_row_from_user(event))
            return
        if kind == "system":
            self._append(_row_from_system(event))
            return
        if kind == "result":
            self._append(_row_from_result(event))
            return
        if kind == "rate_limit_event":
            self._append(_row_from_rate_limit(event))
            return
        self._append(
            AgentRow(
                kind=kind,
                role="",
                text="",
                partial=False,
                subtype="",
                raw=event,
            )
        )

    @Slot()
    def clear(self) -> None:
        """Drop all rows. Used when starting a fresh session in-place."""
        if not self._rows:
            return
        self.beginResetModel()
        self._rows = []
        self._streaming_row_index = None
        self.endResetModel()

    @Slot()
    def on_host_closed(self) -> None:
        """Mark the host closed and emit `hostClosed` for UI wiring."""
        self._streaming_row_index = None
        self.hostClosed.emit()

    # --- Internal helpers -----------------------------------------------

    def _append(self, row: AgentRow) -> None:
        idx = len(self._rows)
        self.beginInsertRows(QModelIndex(), idx, idx)
        self._rows.append(row)
        self.endInsertRows()

    def _handle_stream_event(self, event: dict) -> None:
        """Route a `stream_event`; extend the streaming row on text deltas."""
        inner = event.get("event") or {}
        inner_type = str(inner.get("type") or "")
        if inner_type != "content_block_delta":
            # Non-delta stream_event subtypes (message_start,
            # content_block_start / _stop, message_delta / _stop,
            # ping) carry framing data the finalised `assistant`
            # event already summarises. Rendering them adds noise
            # without value until streaming-progress affordances land
            # in a later iteration.
            return
        delta = inner.get("delta") or {}
        if str(delta.get("type") or "") != "text_delta":
            # Non-text deltas (tool input_json_delta, etc.) are
            # ignored in the placeholder; the finalised `assistant`
            # event carries the complete content blocks.
            return
        text = str(delta.get("text") or "")
        if not text:
            return
        self._extend_streaming_text(text, event)

    def _extend_streaming_text(self, delta_text: str, event: dict) -> None:
        """Append to the streaming row, or open a fresh one if none open."""
        if self._streaming_row_index is None:
            row = AgentRow(
                kind="stream_event",
                role="assistant",
                text=delta_text,
                partial=True,
                subtype="streaming",
                raw=event,
            )
            self._streaming_row_index = len(self._rows)
            self._append(row)
            return
        idx = self._streaming_row_index
        if idx >= len(self._rows):
            # Defensive: row was cleared out from under us. Reopen.
            self._streaming_row_index = None
            self._extend_streaming_text(delta_text, event)
            return
        old = self._rows[idx]
        self._rows[idx] = replace(old, text=old.text + delta_text)
        model_index = self.index(idx)
        self.dataChanged.emit(model_index, model_index, [self.TextRole])


# ---------------------------------------------------------------------------
# Event → row translators
# ---------------------------------------------------------------------------
#
# Kept as free functions so tests can exercise them without
# instantiating the model. Each takes the full event dict and returns
# an `AgentRow`. Shape choices are driven by the Step 1 protocol
# discovery samples (/tmp/claude-stream-baseline.jsonl during the
# spike, not committed).


def _row_from_assistant(event: dict) -> AgentRow:
    return AgentRow(
        kind="assistant",
        role="assistant",
        text=_extract_assistant_text(event.get("message") or {}),
        partial=False,
        subtype="",
        raw=event,
    )


def _row_from_user(event: dict) -> AgentRow:
    message = event.get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = _flatten_content_blocks(content)
    else:
        text = ""
    return AgentRow(
        kind="user",
        role="user",
        text=text,
        partial=False,
        subtype="",
        raw=event,
    )


def _row_from_system(event: dict) -> AgentRow:
    subtype = str(event.get("subtype") or "")
    if subtype == "init":
        session = str(event.get("session_id") or "")[:8]
        text = f"session started ({session})" if session else "session started"
    elif subtype.startswith("hook_"):
        hook_name = str(event.get("hook_name") or event.get("hook_event") or "")
        outcome = str(event.get("outcome") or "")
        phase = subtype[len("hook_") :]
        text = f"hook {phase}: {hook_name}"
        if outcome:
            text = f"{text} — {outcome}"
    else:
        text = subtype
    return AgentRow(
        kind="system",
        role="system",
        text=text,
        partial=False,
        subtype=subtype,
        raw=event,
    )


def _row_from_result(event: dict) -> AgentRow:
    duration_ms = event.get("duration_ms")
    cost = event.get("total_cost_usd")
    pieces: list[str] = ["done"]
    if isinstance(duration_ms, (int, float)):
        pieces.append(f"{int(duration_ms)}ms")
    if isinstance(cost, (int, float)):
        pieces.append(f"${cost:.4f}")
    return AgentRow(
        kind="result",
        role="system",
        text=" · ".join(pieces),
        partial=False,
        subtype=str(event.get("subtype") or ""),
        raw=event,
    )


def _row_from_rate_limit(event: dict) -> AgentRow:
    info = event.get("rate_limit_info") or {}
    status = str(info.get("status") or "")
    rate_type = str(info.get("rateLimitType") or "")
    text = f"rate limit: {status}" if status else "rate limit"
    if rate_type:
        text = f"{text} ({rate_type})"
    return AgentRow(
        kind="rate_limit_event",
        role="system",
        text=text,
        partial=False,
        subtype=status,
        raw=event,
    )


def _extract_assistant_text(message: dict) -> str:
    """Flatten an assistant message's content blocks to visible text.

    Text blocks contribute `text`. Tool-use blocks contribute a
    `[tool: <name>]` marker so the flat view still shows invocations;
    drill-in with full inputs lands in a follow-up. Unknown block
    types contribute nothing rather than crashing — a future content-
    block addition surfaces as a gap to notice, not a rendering fault.
    """
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    return _flatten_content_blocks(content)


def _flatten_content_blocks(content: list) -> str:
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = str(block.get("type") or "")
        if btype == "text":
            parts.append(str(block.get("text") or ""))
        elif btype == "tool_use":
            name = str(block.get("name") or "?")
            parts.append(f"[tool: {name}]")
        elif btype == "tool_result":
            parts.append("[tool result]")
    return "".join(parts)
