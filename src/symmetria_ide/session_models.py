"""Agent-pane event model: SessionModel for SDK sidecar events.

Extracted here (rather than living in `app.py`) to mirror the
convention started by `cmdline_models.py` and `whichkey_models.py`:
each QML-facing model group lives in its own file, keeps `app.py`
lean, and registers its types via `@QmlElement` as an import-time
side effect. That side-effect import (with `noqa: F401`) is kept in
`app.py`, alongside an `_ = SessionModel` anchor in
`_register_qml_types` so linters cannot silently drop it (the same
second-layer protection the existing QML-registered modules use).

These models render the JSONL event stream emitted by the Node
sidecar (`sidecar/src/index.ts`). The sidecar drives
`@anthropic-ai/claude-agent-sdk` programmatically and translates
typed SDK messages back to dict shapes that `_row_from_*` here
already consumes (assistant, system, stream_event, result,
rate_limit_event), plus a sidecar-synthesized `permission_request`
envelope when the SDK's `canUseTool` callback fires. See CLAUDE.md
`## The agent backend (Node SDK sidecar)` for the wire-protocol
contract and `sidecar/src/protocol.ts` for the TypeScript types.

Shape: one ListView row per event, with partial coalescing for
streaming assistant text. Turn grouping and tool-call drill-in land
later — designing them against guessed event cadence is exactly the
waste the placeholder discipline exists to avoid.
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
    """One sidecar event rendered as a flat ListView row.

    `kind` carries the raw event `type` (grep-able back to the
    sidecar's wire protocol — see `sidecar/src/protocol.ts`). `role`
    is an inferred attribution that QML delegates colour-map against
    `Theme.color.agent.*`. `text` is the human-readable body,
    possibly grown by partial-text coalescing while `partial=True`.
    `subtype` adds discriminator detail for system/result/tool events.
    `raw` keeps the original event dict so future richer delegates
    (tool drill-in, image rendering, etc.) have the full payload
    without requiring a model rewrite.

    `permission_state` and `request_id` are populated only on
    `kind == "permission_request"` rows. They drive the in-pane
    approve/deny card in `AgentPane.qml` and the lookup that
    `resolve_permission()` uses to flip a row from "pending" to
    "approved"/"denied" once the user decides. Empty strings on
    every other row — the QML delegate gates the card on
    `permission_state === "pending"` so non-permission rows are
    unaffected.
    """

    kind: str
    role: str
    text: str
    partial: bool
    subtype: str
    raw: dict
    permission_state: str = ""
    request_id: str = ""


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
    **replaces the streaming row in place** — text is re-extracted
    from the canonical content blocks (so `[tool: name]` markers
    interleaved with text are preserved, even though tool blocks were
    never part of the streamed text), `partial` flips to `False`, and
    `raw` is swapped to the assistant event. If no streaming row was
    open (very short non-streaming responses), a fresh canonical row
    is appended.

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
    PermissionStateRole = Qt.ItemDataRole.UserRole + 7
    RequestIdRole = Qt.ItemDataRole.UserRole + 8

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
            self.PermissionStateRole: b"permissionState",
            self.RequestIdRole: b"requestId",
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
        if role == self.PermissionStateRole:
            return row.permission_state
        if role == self.RequestIdRole:
            return row.request_id
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
        # coalesce so the next text_delta opens a fresh row. Snapshot
        # first so the `assistant` branch can finalise the row in
        # place rather than appending a duplicate.
        streaming_idx = self._streaming_row_index
        self._streaming_row_index = None
        if kind == "assistant":
            if streaming_idx is not None and streaming_idx < len(self._rows):
                self._finalize_streaming_assistant(streaming_idx, event)
            else:
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
        if kind == "permission_request":
            self._append(_row_from_permission_request(event))
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

    @Slot(str, str)
    def resolve_permission(self, request_id: str, behavior: str) -> None:
        """Mark the matching permission row as approved or denied.

        Locates the most recent row whose `request_id` matches and
        whose `permission_state == "pending"`, replaces it via
        `dataclasses.replace`, and emits `dataChanged` with a role
        list scoped to `PermissionStateRole` (gotcha #3 — empty role
        lists force full re-bind; scoped lists let QML re-evaluate
        only the binding that actually changed). No-op if no matching
        pending row exists — keeps the QML invocation surface
        tolerant of races where the host already closed before the
        user's click landed.
        """
        new_state = "approved" if behavior == "allow" else "denied"
        # Walk in reverse — pending permission requests are
        # short-lived and the relevant row is almost always near the
        # tail. Bail on the first match.
        for idx in range(len(self._rows) - 1, -1, -1):
            row = self._rows[idx]
            if row.request_id == request_id and row.permission_state == "pending":
                self._rows[idx] = replace(row, permission_state=new_state)
                model_index = self.index(idx)
                self.dataChanged.emit(
                    model_index, model_index, [self.PermissionStateRole]
                )
                return
        log.debug("resolve_permission: no pending row for request_id=%s", request_id)

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

    def _finalize_streaming_assistant(self, idx: int, event: dict) -> None:
        """Replace the in-flight streaming row with the canonical assistant row.

        The text-delta-built body is text-only — tool_use blocks were
        ignored during streaming because their `input_json_delta`
        frames carry tool input fragments, not visible content. The
        finalised `assistant` event ships the complete, ordered list
        of content blocks; re-extracting via `_extract_assistant_text`
        gives us text + `[tool: name]` markers correctly interleaved.

        Mutating in place (rather than appending a new row) keeps the
        UI from showing a duplicate "Claude" entry every turn — the
        bug a user submitted while testing multi-turn. The `dataChanged`
        signal carries an explicit role list so QML only re-evaluates
        the bindings that actually changed (gotcha #3).
        """
        old = self._rows[idx]
        text = _extract_assistant_text(event.get("message") or {})
        self._rows[idx] = replace(
            old,
            kind="assistant",
            text=text,
            partial=False,
            subtype="",
            raw=event,
        )
        model_index = self.index(idx)
        self.dataChanged.emit(
            model_index,
            model_index,
            [
                self.KindRole,
                self.TextRole,
                self.PartialRole,
                self.SubtypeRole,
                self.RawRole,
            ],
        )

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


def _row_from_permission_request(event: dict) -> AgentRow:
    """Build a pending-permission row from a `permission_request` envelope.

    Envelope shape (sidecar-synthesized — see `sidecar/src/protocol.ts`
    `PermissionRequestEvent`):

        {"type": "permission_request",
         "request_id": "<uuid>",
         "tool_name": "<name>",
         "tool_use_id": "<uuid>",
         "input": {...},
         "title"?: "...", "description"?: "...", ...,
         "note": "sidecar-synthesized"}

    The `text` field prefers the SDK's bridge-rendered `title` when
    present (matches what the canUseTool docstring recommends as the
    primary prompt) and falls back to a tool-name + summary
    reconstruction. `subtype` carries the tool name so the QML
    delegate can label the approve button without re-deriving it.
    """
    request_id = str(event.get("request_id") or "")
    tool_name = str(event.get("tool_name") or "")
    title = str(event.get("title") or "")
    if title:
        text = title
    elif tool_name:
        text = f"Allow {tool_name}?"
    else:
        text = "Allow tool call?"
    return AgentRow(
        kind="permission_request",
        role="system",
        text=text,
        partial=False,
        subtype=tool_name,
        raw=event,
        permission_state="pending",
        request_id=request_id,
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
