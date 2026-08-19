"""Per-harness readers for the conversation history the thread rail lists.

The rail shows a project's agent threads, live AND dead. The live ones come
from `AppController`'s pool; the dead ones have to be recovered from whatever
each harness wrote down, because the IDE keeps almost nothing of its own (see
`agent_thread_store` for the one exception, `work_root`).

Every harness gets ONE reader behind the `ThreadIndexReader` protocol, and the
readers are registered in `THREAD_READERS`. Adding Pi later is writing a third
reader and registering it — no caller changes, because the indexer only ever
iterates the registry. The two that exist prove the interface is not
Claude-shaped: `ClaudeThreadReader` walks files this project has to interpret
itself, while `OpenCodeThreadReader` asks a CLI that already did the
interpreting. Qt-free and synchronous on purpose, like `agent_harness` and
`worktree`: the Qt lane lives in `agent_thread_indexer.py`, which is the only
thing that knows these calls are slow.

Claude's index — the non-obvious parts, all measured 2026-08-15 against the
developer's 315 real transcripts:

- **Discovery is `projects/*/*.jsonl`, exactly one level deep.** Two properties
  fall out of that depth for free: subagent transcripts live at
  `<session_id>/subagents/*.jsonl` and are simply out of reach, and the encoded
  bucket NAME is never read. It must not be: Claude's encoding maps both `/`
  and `.` to `-`, so decoding it is ambiguous and substring-matching it drags
  in other repositories. Ownership is decided by the `cwd` field INSIDE the
  transcript, folded through `canonical_project_root` so a linked worktree's
  sessions belong to the same project as the main checkout's.
- **Discover from the transcripts that EXIST, never from `~/.claude/history.jsonl`.**
  Claude prunes transcripts: history remembered 110 sessions for this project
  where 17 survived on disk. A pruned session cannot be resumed, so listing it
  would offer a row that fails the moment it is clicked.
- **Two bounded reads per file and no third** — a 512 KiB head and a 512 KiB
  tail. Head + tail extraction of cwd, title and ordering over all 315
  transcripts took **0.47 s**, against 1.15 s for full reads, and resolved cwd
  for 315/315 and a title for 313/315. The tail answers a third question in the
  same pass: the worktree backfill below.
- **Ordering is the file's mtime**, not a timestamp parsed out of the tail.
  Only 246 of 315 tails yielded a timestamp record — a tail slice can begin
  mid-record, and the trailing envelopes are often `mode`/`queue-operation`
  shapes that carry none — while mtime is always there and is exactly what
  "most recently used" means.
- **Title prefers Claude's own `ai-title` record** (265 of 315 carry one, at
  the 2.7% mark of the file at the median) because it is a written summary
  rather than a truncated prompt; the first external, non-sidechain user
  message is the fallback for the rest. Searched across BOTH windows: 4 of a
  70-file sample carried the record past the head, and those bytes are already
  parsed by the time the title is derived.
- **The worktree backfill mirrors the LIVE follow exactly**: last same-repo
  write-tool path wins, and a FOREIGN write clears the candidate rather than
  moving it. Unfiltered "last write wins" is noisy — it points at /tmp, at
  dotfiles and at other repositories. This only matters for threads older than
  `agent_thread_store`; once the store has an entry, the store wins.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .agent_coordination import message_text
from .agent_harness import parse_opencode_sessions
from .project_browser_marker import resolve_project_root
from .worktree import canonical_project_root

log = logging.getLogger(__name__)

# The tools whose target path says where an agent is WORKING. Read tools are
# deliberately absent: exploration must never move an agent's work root (the
# live follow's rule, and the backfill below has to match it or a resumed
# thread would land somewhere the live IDE never followed).
#
# Canonical here rather than in `app.py` so the live hook path and the history
# backfill cannot drift apart; `app._AGENT_WRITE_TOOLS` is an alias of this.
AGENT_WRITE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})

# One read window. Both ends of a transcript get one, and nothing between them
# is ever opened — see the module docstring's measurement.
WINDOW_BYTES = 512 * 1024

# Points the Claude reader at a different transcript root. Exists for the test
# suite: the reader's default is the developer's REAL `~/.claude/projects`, and
# `tests/conftest.py` isolates it at the environment level per
# `.claude/rules/test_env_isolation.md` (a constructor default cannot be
# isolated by an autouse fixture).
PROJECTS_DIR_ENV = "SYMMETRIA_IDE_CLAUDE_PROJECTS_DIR"

# A title long enough to identify a conversation, short enough for a rail row.
_MAX_TITLE_CHARS = 120


@dataclass(slots=True, frozen=True)
class ThreadRecord:
    """One conversation as its harness recorded it.

    Deliberately NOT `ThreadRow` (the rail's model type): this is the harness's
    view, with no notion of a pane slot. `AppController` is what merges a record
    onto a live row — by `(harness, session_id)` — and decides the slot.

    `work_root` is a BEST EFFORT recovery for threads predating
    `agent_thread_store`; "" means "could not tell", which the resume path
    treats as "start in the main checkout".

    `session_cwd` is the field OWNERSHIP was decided by — the transcript's own
    `cwd` for claude, the session's `directory` for opencode. It is
    deliberately NOT a work-root fallback: measured 2026-08-15, a session
    rooted in the main checkout made 158 of its 230 writes inside a linked
    worktree, so the session cwd is exactly the signal that does not answer
    "where was this working".

    `resumable` is the reader's verdict on whether its harness can reopen the
    conversation; a False row is dropped before it reaches the rail
    (`AppController._on_threads_indexed`), because a row that fails on click is
    worse than its absence.
    """

    harness: str
    session_id: str
    title: str
    updated_at: float
    session_cwd: str
    work_root: str
    resumable: bool


class ThreadIndexReader(Protocol):
    """One harness's history, read from wherever that harness keeps it.

    `read` runs on the indexer's worker thread and may be slow (Claude walks a
    few hundred files; OpenCode spawns a CLI). It must poll `stop_event` and
    return early when it is set — a superseding project change sets it, and a
    reader that ignores it delays every later request behind itself.
    """

    harness: str

    def read(
        self, project_root: str, stop_event: threading.Event
    ) -> list[ThreadRecord]: ...


def _parse_window(window: bytes) -> list[dict]:
    """The JSONL records inside one read window, in file order.

    Tolerant by construction, because both windows are cut at byte offsets
    rather than record boundaries: the head's last line and the tail's first
    line are routinely half a record, and the bytes between the windows are
    never seen at all. The `b"{"` guard is what keeps a 512 KiB slab of
    non-JSON from being decoded before being rejected.
    """
    records: list[dict] = []
    for line in window.split(b"\n"):
        line = line.strip()
        if not line.startswith(b"{"):
            continue
        try:
            record = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue  # a sliced record, or a non-JSONL line
        if isinstance(record, dict):
            records.append(record)
    return records


def _first_cwd(records: Iterable[dict]) -> str:
    for record in records:
        cwd = record.get("cwd")
        if isinstance(cwd, str) and cwd:
            return cwd
    return ""


def _clean_title(text: str) -> str:
    title = " ".join(str(text).split())
    if len(title) > _MAX_TITLE_CHARS:
        title = title[:_MAX_TITLE_CHARS].rstrip() + "…"
    return title


def _title_of(records: Iterable[dict]) -> str:
    """Claude's own `ai-title` when it wrote one, else the opening human turn.

    The fallback skips two kinds of `user` record that are not the user
    talking: a sidechain (a subagent's conversation replayed into the parent)
    and an internal one (a harness-synthesised turn). Both would title the
    thread with something the user never typed.
    """
    fallback = ""
    for record in records:
        if record.get("type") == "ai-title":
            ai_title = record.get("aiTitle") or record.get("title") or ""
            if isinstance(ai_title, str) and ai_title.strip():
                return _clean_title(ai_title)
            continue
        if fallback or record.get("type") != "user":
            continue
        if (
            record.get("isSidechain")
            or record.get("userType", "external") != "external"
        ):
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        text = message_text(message.get("content", ""))
        if text:
            fallback = _clean_title(text)
    return fallback


def _write_tool_paths(record: dict) -> list[str]:
    """Absolute target paths of the write-tool calls in one record."""
    message = record.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    paths: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        if block.get("name") not in AGENT_WRITE_TOOLS:
            continue
        payload = block.get("input")
        if not isinstance(payload, dict):
            continue
        target = payload.get("file_path") or payload.get("notebook_path") or ""
        # The live hook path applies the same isabs guard: a relative path
        # would send `resolve_project_root` up a meaningless chain.
        if isinstance(target, str) and target and os.path.isabs(target):
            paths.append(target)
    return paths


def recover_work_root(records: Iterable[dict], project_root: str) -> str:
    """Where a past conversation was working, from its write-tool history.

    The live follow's rule, replayed over a transcript: the last write into
    THIS project wins, and a write into anything else — another repository,
    /tmp, a dotfile — CLEARS the candidate instead of moving it. Clearing
    rather than ignoring is what makes an agent that wandered off resume in the
    main checkout, which is where the live follow would have left the chrome.
    """
    candidate = ""
    for record in records:
        for target in _write_tool_paths(record):
            root = resolve_project_root(target)
            candidate = (
                root if root and canonical_project_root(root) == project_root else ""
            )
    return candidate


class ClaudeThreadReader:
    """Claude Code's transcripts, scoped to one project.

    `projects_root` is injectable so tests never read the developer's real
    transcripts; unset, it resolves per call (so an env override applied after
    construction still bites) to `$SYMMETRIA_IDE_CLAUDE_PROJECTS_DIR` and then
    to `~/.claude/projects`.
    """

    harness = "claude"

    def __init__(self, projects_root: str | Path | None = None) -> None:
        self._projects_root = Path(projects_root) if projects_root else None

    def projects_root(self) -> Path:
        if self._projects_root is not None:
            return self._projects_root
        override = os.environ.get(PROJECTS_DIR_ENV, "")
        if override:
            return Path(override)
        return Path.home() / ".claude" / "projects"

    def read(
        self, project_root: str, stop_event: threading.Event
    ) -> list[ThreadRecord]:
        project = canonical_project_root(resolve_project_root(project_root))
        if not project:
            return []
        root = self.projects_root()
        records: list[ThreadRecord] = []
        # ONE level. See the module docstring: this depth is both the subagent
        # filter and the reason no bucket name is ever interpreted.
        try:
            transcripts = sorted(root.glob("*/*.jsonl"))
        except OSError as e:
            log.warning("claude thread index: cannot list %s: %s", root, e)
            return []
        for path in transcripts:
            if stop_event.is_set():
                log.debug("claude thread index: cancelled after %d rows", len(records))
                break
            record = self._read_transcript(path, project)
            if record is not None:
                records.append(record)
        records.sort(key=lambda record: record.updated_at, reverse=True)
        return records

    def _read_transcript(self, path: Path, project_root: str) -> ThreadRecord | None:
        """One transcript, or None when it belongs to another project."""
        try:
            size = path.stat().st_size
        except OSError:
            return None  # pruned between the glob and here — not an error
        head, tail = self._windows(path, size)
        head_records = _parse_window(head)
        tail_records = _parse_window(tail)
        session_cwd = _first_cwd(head_records) or _first_cwd(tail_records)
        if not session_cwd:
            return None
        if canonical_project_root(resolve_project_root(session_cwd)) != project_root:
            return None
        # BOTH windows, not just the head. The head is where an `ai-title`
        # normally sits (2.7% of the file at the median), but 4 of a 70-file
        # sample carried it past 512 KiB — and the tail bytes are already read
        # and parsed, so restricting the search to the head buys nothing and
        # titles those rows with a truncated opening prompt instead.
        windows = head_records + tail_records
        return ThreadRecord(
            harness=self.harness,
            session_id=path.stem,
            title=_title_of(windows),
            # mtime, not a parsed timestamp — see the module docstring.
            updated_at=self._mtime(path),
            session_cwd=session_cwd,
            work_root=recover_work_root(windows, project_root),
            # The transcript exists, which is exactly what `claude -r <id>`
            # needs; a session whose transcript was pruned is never seen here.
            resumable=True,
        )

    @staticmethod
    def _mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    @staticmethod
    def _windows(path: Path, size: int) -> tuple[bytes, bytes]:
        """The head and tail slabs, in ONE open and at most two bounded reads.

        Every `read` is given an explicit size — never `read()` — so a large
        transcript cannot be pulled into memory whole by accident. The tail's
        offset is clamped to the end of the head so the two windows never
        overlap, which keeps a mid-sized file from being read twice.
        """
        try:
            with path.open("rb") as f:
                head = f.read(min(size, WINDOW_BYTES))
                if size <= WINDOW_BYTES:
                    return head, b""
                offset = max(WINDOW_BYTES, size - WINDOW_BYTES)
                f.seek(offset)
                return head, f.read(min(WINDOW_BYTES, size - offset))
        except OSError as e:
            log.debug("claude thread index: unreadable transcript %s: %s", path, e)
            return b"", b""


def opencode_rows_in_project(
    rows: Iterable[dict], project_root: str, *, keep_unscoped: bool = False
) -> list[dict]:
    """The parsed OpenCode rows that belong to `project_root`.

    The SECOND of the two overlapping defences described on
    `OpenCodeThreadReader` — the cwd makes the good answer likely, this is what
    keeps a global-scope answer out. Shared with the resume picker's cold path
    (`AppController._fetch_opencode_sessions`), because a filter that guards
    the rail and not the picker still resumes another project's conversation:
    the two surfaces read the same command and must agree on what it means.

    `keep_unscoped` is the one place they differ, and it is about a row with no
    `directory` at all. Every row the real CLI emits carries one (measured
    2026-08-15), so this only decides the synthetic case. The rail drops it —
    an unscopable row in a durable list is exactly how a foreign thread gets in
    — while the picker keeps it, because the picker is an explicit action the
    user just took in this project, and its own contract tests pin that a row
    with no scope information still lists.
    """
    project = canonical_project_root(resolve_project_root(project_root))
    kept: list[dict] = []
    for row in rows:
        directory = str(row.get("directory") or "")
        if not directory:
            if keep_unscoped:
                kept.append(row)
            continue
        if canonical_project_root(resolve_project_root(directory)) == project:
            kept.append(row)
    return kept


class OpenCodeThreadReader:
    """OpenCode's own session store, via its CLI, scoped to one project.

    Nothing is read off disk here: OpenCode keeps its sessions behind
    `opencode session list --format json`, which also generates the titles, so
    this reader is a subprocess where Claude's is a file walk. Two consequences
    it does not share with Claude, both measured 2026-08-15 (method and the
    three cwd configurations in
    `.claude/memory/reference/agent-sdk/opencode_session_list_scoping.md`):

    - **The cwd decides the scope, and an unscoped cwd fails OPEN.** Run from
      the linked worktree and from the main checkout, the command returned the
      identical 4 rows with the same `projectId` and `directory` at the main
      checkout — the CLI folds worktrees itself. Run from a cwd inside no git
      project at all, the same command returned **15 unrelated sessions** with
      `projectId: "global"` and `directory` values in other repositories. Hence
      two defences that overlap on purpose: ask at the CANONICAL project root,
      and then discard every row whose `directory` does not canonicalise back
      onto it. The first makes the good answer likely; the second is what keeps
      a global-scope answer from putting another project's conversations in
      this project's rail.
    - **There is no worktree information on this side.** `directory` is the
      main checkout even for a session done inside a linked worktree, so
      `work_root` is always "" here and an OpenCode thread older than
      `agent_thread_store` resumes in the main checkout with a visible notice.
      Do not try to infer one — the field that would carry it does not exist.

    A warm run took 1.56 s, which is why this reader publishes second and why
    `agent_thread_indexer` exists at all.
    """

    harness = "opencode"

    # The exact command, as one constant, because two things run it: this
    # reader and the resume picker's cold-start fallback
    # (`AppController._fetch_opencode_sessions`, which imports both constants
    # rather than repeating the literal).
    LIST_ARGV: tuple[str, ...] = ("opencode", "session", "list", "--format", "json")

    # Well over the 1.56 s measured warm (2026-08-15; see
    # `.claude/memory/reference/agent-sdk/opencode_session_list_scoping.md`),
    # so the timeout only fires on a genuinely wedged CLI. No cold figure is
    # cited: the 3.8 s this project once recorded is retracted, and no
    # replacement cold run was taken.
    TIMEOUT_SEC = 15

    # Points the reader at a different executable. Exists for the test suite:
    # the default reaches the developer's REAL opencode and its live session
    # store, which is the class of reach `.claude/rules/test_env_isolation.md`
    # requires an environment-level switch for (the `SYMMETRIA_IDE_CHROME_BIN`
    # precedent). Resolved per call, so an override applied after construction
    # still bites.
    BIN_ENV = "SYMMETRIA_IDE_OPENCODE_BIN"

    def list_argv(self) -> list[str]:
        """`LIST_ARGV` with the executable the environment selects."""
        argv = list(self.LIST_ARGV)
        argv[0] = os.environ.get(self.BIN_ENV) or argv[0]
        return argv

    def read(
        self, project_root: str, stop_event: threading.Event
    ) -> list[ThreadRecord]:
        project = canonical_project_root(resolve_project_root(project_root))
        if not project or stop_event.is_set():
            return []
        stdout = self._list_sessions(project)
        if stdout is None:
            return []
        rows = parse_opencode_sessions(stdout)
        if rows is None:
            log.warning("opencode thread index: unparseable `session list` output")
            return []
        records: list[ThreadRecord] = []
        for row in opencode_rows_in_project(rows, project):
            directory = str(row.get("directory") or "")
            timestamp = row.get("updated") or row.get("created") or 0
            records.append(
                ThreadRecord(
                    harness=self.harness,
                    session_id=str(row.get("id") or ""),
                    title=str(row.get("title") or ""),
                    # The CLI reports milliseconds; `ThreadRecord` is in
                    # seconds, like Claude's mtime, so the rail can order the
                    # two harnesses against each other.
                    updated_at=float(timestamp) / 1000.0,
                    session_cwd=directory,
                    # Always "" — see the class docstring.
                    work_root="",
                    # `--session <id>` reopens anything this command lists.
                    resumable=True,
                )
            )
        return records

    def _list_sessions(self, cwd: str) -> str | None:
        """The CLI's stdout, or None with a logged reason on any failure.

        Every failure mode is contained here and none escapes as an exception:
        this runs on the indexer's single worker thread, and although that loop
        catches per-reader failures, an exception is a harness losing its whole
        history where a logged empty result is one scan that found nothing.
        """
        try:
            result = subprocess.run(
                self.list_argv(),
                check=False,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=self.TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired:
            log.warning(
                "opencode thread index: `session list` timed out after %ss in %s",
                self.TIMEOUT_SEC,
                cwd,
            )
            return None
        except OSError as e:
            # FileNotFoundError is the ordinary case: OpenCode is optional, and
            # a project that never uses it must degrade to no rows, not to an
            # error surface.
            log.warning("opencode thread index: cannot run `session list`: %s", e)
            return None
        if result.returncode != 0:
            log.warning(
                "opencode thread index: `session list` exited %d: %s",
                result.returncode,
                (result.stderr or "").strip(),
            )
            return None
        return result.stdout


# harness name -> a zero-argument factory for its reader. The indexer builds
# every registered reader and asks each for the same project, so a new harness
# joins the rail's history by adding one line here.
#
# Registration ORDER is publication order, and it is deliberate: claude reads a
# few hundred local files in well under a second while opencode spawns a CLI,
# so listing claude first puts rows on the rail while opencode is still
# starting. Pi would be a third entry and nothing else.
THREAD_READERS: dict[str, Callable[[], ThreadIndexReader]] = {
    "claude": ClaudeThreadReader,
    "opencode": OpenCodeThreadReader,
}


def default_readers() -> list[ThreadIndexReader]:
    """One reader per registered harness, in registration order."""
    return [factory() for factory in THREAD_READERS.values()]
