"""Phase-4 specs for the bounded, project-scoped Claude transcript reader."""

from __future__ import annotations

import builtins
import importlib
import json
import threading
from pathlib import Path

WINDOW_BYTES = 512 * 1024


def _module():
    # Import inside each test: before Phase 4 the module does not exist, but the
    # specs must still collect and report an observed failure independently.
    return importlib.import_module("symmetria_ide.agent_threads")


def _repo(path: Path) -> Path:
    (path / ".git").mkdir(parents=True)
    return path


def _linked_worktree(main: Path, path: Path, name: str = "feature") -> Path:
    (main / ".git" / "worktrees" / name).mkdir(parents=True, exist_ok=True)
    path.mkdir(parents=True)
    (path / ".git").write_text(
        f"gitdir: {main}/.git/worktrees/{name}\n", encoding="utf-8"
    )
    return path


def _user(cwd: Path, text: str, **fields: object) -> dict:
    return {
        "type": "user",
        "cwd": str(cwd),
        "isSidechain": False,
        "userType": "external",
        "message": {"role": "user", "content": text},
        **fields,
    }


def _tool_use(cwd: Path, name: str, path: Path) -> dict:
    key = "notebook_path" if name == "NotebookEdit" else "file_path"
    return {
        "type": "assistant",
        "cwd": str(cwd),
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": f"tool-{name}-{path.name}",
                    "name": name,
                    "input": {key: str(path)},
                }
            ],
        },
    }


def _write_transcript(path: Path, *records: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def _read(projects_root: Path, project_root: Path) -> list:
    reader = _module().ClaudeThreadReader(projects_root=projects_root)
    return list(reader.read(str(project_root), threading.Event()))


def _field(row: object, name: str):
    if isinstance(row, dict):
        return row[name]
    return getattr(row, name)


def _by_session(rows: list) -> dict[str, object]:
    return {_field(row, "session_id"): row for row in rows}


def test_reader_uses_internal_cwd_instead_of_decoding_or_matching_bucket_name(
    tmp_path,
) -> None:
    """Spec: encoded directory names never decide transcript ownership."""
    project = _repo(tmp_path / "real.project" / "checkout")
    foreign = _repo(tmp_path / "foreign" / "checkout")
    projects_root = tmp_path / "claude" / "projects"

    # A deliberately unrelated bucket must still be found from its internal cwd.
    _write_transcript(
        projects_root / "bucket-that-cannot-name-the-project" / "owned.jsonl",
        _user(project, "Owned by the real cwd"),
    )
    # Conversely, a bucket that looks exactly like a lossy Claude encoding of
    # this project must not claim a transcript whose real cwd is foreign.
    encoded_project = "-" + str(project).lstrip("/").replace("/", "-").replace(".", "-")
    _write_transcript(
        projects_root / encoded_project / "foreign.jsonl",
        _user(foreign, "The bucket name is a false positive"),
    )
    # Discovery is exactly projects/*/*.jsonl: a JSONL placed directly under
    # the projects root is not a harness session bucket and must be ignored.
    _write_transcript(
        projects_root / "not-in-a-bucket.jsonl",
        _user(project, "Wrong discovery depth"),
    )

    rows = _read(projects_root, project)

    assert set(_by_session(rows)) == {"owned"}


def test_reader_claims_main_and_linked_worktree_cwds_but_rejects_other_repos(
    tmp_path,
) -> None:
    """Spec: ownership compares canonical project roots from transcript cwd."""
    main = _repo(tmp_path / "project")
    worktree = _linked_worktree(main, tmp_path / "project-feature")
    foreign = _repo(tmp_path / "other-project")
    projects_root = tmp_path / "claude" / "projects"

    _write_transcript(
        projects_root / "one" / "main-session.jsonl",
        _user(main, "Main checkout"),
    )
    _write_transcript(
        projects_root / "two" / "worktree-session.jsonl",
        _user(worktree, "Linked worktree"),
    )
    _write_transcript(
        projects_root / "three" / "foreign-session.jsonl",
        _user(foreign, "Different repository"),
    )

    rows = _read(projects_root, main)

    assert set(_by_session(rows)) == {"main-session", "worktree-session"}


def test_reader_globs_only_top_level_transcripts_and_excludes_subagents(
    tmp_path,
) -> None:
    """Spec: ``<session>/subagents/*.jsonl`` is outside discovery depth."""
    project = _repo(tmp_path / "project")
    projects_root = tmp_path / "claude" / "projects"
    bucket = projects_root / "bucket"
    _write_transcript(
        bucket / "parent-session.jsonl", _user(project, "Top-level parent")
    )
    _write_transcript(
        bucket / "parent-session" / "subagents" / "child-agent.jsonl",
        _user(project, "Must never become a project thread"),
    )

    rows = _read(projects_root, project)

    assert set(_by_session(rows)) == {"parent-session"}


def test_title_prefers_ai_title_and_falls_back_to_first_external_user_message(
    tmp_path,
) -> None:
    """Spec: Claude titles use ai-title, then the first resumable human turn."""
    project = _repo(tmp_path / "project")
    projects_root = tmp_path / "claude" / "projects"
    _write_transcript(
        projects_root / "bucket" / "titled.jsonl",
        _user(project, "Raw prompt that should lose"),
        {
            "type": "ai-title",
            "cwd": str(project),
            "aiTitle": "Repair the compiler pipeline",
        },
    )
    _write_transcript(
        projects_root / "bucket" / "fallback.jsonl",
        _user(project, "Sidechain noise", isSidechain=True),
        _user(project, "Internal noise", userType="internal"),
        _user(project, "First real user request"),
        _user(project, "Later user request"),
    )

    rows = _by_session(_read(projects_root, project))

    assert _field(rows["titled"], "title") == "Repair the compiler pipeline"
    assert _field(rows["fallback"], "title") == "First real user request"


def test_ai_title_in_tail_still_overrides_opening_prompt(tmp_path) -> None:
    """Regression: an ai-title past the head window remains authoritative."""
    project = _repo(tmp_path / "project")
    projects_root = tmp_path / "claude" / "projects"
    transcript = projects_root / "bucket" / "late-title.jsonl"
    transcript.parent.mkdir(parents=True)
    opening = (json.dumps(_user(project, "Raw opening prompt")) + "\n").encode()
    title = (
        json.dumps(
            {
                "type": "ai-title",
                "cwd": str(project),
                "aiTitle": "Authoritative late AI title",
            }
        )
        + "\n"
    ).encode()
    # Put the title wholly beyond the 512 KiB head while keeping it inside the
    # bounded tail. The reader must prefer it without scanning the middle.
    transcript.write_bytes(
        opening + (b"not-json-padding" * (WINDOW_BYTES // 16 + 64)) + b"\n" + title
    )
    assert transcript.stat().st_size > WINDOW_BYTES

    rows = _by_session(_read(projects_root, project))

    assert _field(rows["late-title"], "title") == "Authoritative late AI title"


class _BoundedFile:
    """File wrapper that turns an unbounded transcript read into a hard failure."""

    def __init__(self, raw, reads: list[int]) -> None:
        self._raw = raw
        self._reads = reads

    def __enter__(self):
        self._raw.__enter__()
        return self

    def __exit__(self, *args):
        return self._raw.__exit__(*args)

    def seek(self, *args):
        return self._raw.seek(*args)

    def tell(self):
        return self._raw.tell()

    def read(self, size: int = -1):
        assert 0 <= size <= WINDOW_BYTES, (
            "Claude transcript reads must be explicit and bounded to 512 KiB"
        )
        self._reads.append(size)
        return self._raw.read(size)


def test_reader_reads_only_bounded_head_and_tail_and_skips_unreadable_middle(
    tmp_path, monkeypatch
) -> None:
    """Spec: a transcript's inaccessible middle is never scanned or decoded."""
    project = _repo(tmp_path / "project")
    worktree = _linked_worktree(main=project, path=tmp_path / "project-feature")
    projects_root = tmp_path / "claude" / "projects"
    transcript = projects_root / "bucket" / "bounded.jsonl"
    transcript.parent.mkdir(parents=True)
    head = (json.dumps(_user(project, "Bounded transcript")) + "\n").encode()
    tail = (
        "\n" + json.dumps(_tool_use(project, "Edit", worktree / "tail.py")) + "\n"
    ).encode()
    # Invalid UTF-8 and invalid JSON make the middle unusable as text. Its size
    # also exceeds both allowed windows, so a whole-file read cannot hide.
    transcript.write_bytes(head + (b"\xff" * (2 * WINDOW_BYTES + 97)) + tail)

    real_open = Path.open
    real_builtin_open = builtins.open
    reads: list[int] = []

    def bounded_open(path: Path, *args, **kwargs):
        raw = real_open(path, *args, **kwargs)
        if path == transcript:
            return _BoundedFile(raw, reads)
        return raw

    def bounded_builtin_open(path, *args, **kwargs):
        raw = real_builtin_open(path, *args, **kwargs)
        if Path(path) == transcript:
            return _BoundedFile(raw, reads)
        return raw

    monkeypatch.setattr(Path, "open", bounded_open)
    monkeypatch.setattr(builtins, "open", bounded_builtin_open)

    rows = _read(projects_root, project)

    assert set(_by_session(rows)) == {"bounded"}
    assert _field(rows[0], "work_root") == str(worktree)
    assert reads
    assert sum(reads) <= 2 * WINDOW_BYTES


def test_backfill_uses_last_same_repo_write_and_clears_foreign_write_targets(
    tmp_path,
) -> None:
    """Spec: legacy worktree inference exactly mirrors live write-tool follow."""
    main = _repo(tmp_path / "project")
    worktree = _linked_worktree(main, tmp_path / "project-feature")
    foreign = _repo(tmp_path / "other-project")
    projects_root = tmp_path / "claude" / "projects"
    bucket = projects_root / "bucket"

    _write_transcript(
        bucket / "last-same-repo.jsonl",
        _user(main, "Move between same-repo roots"),
        _tool_use(main, "Write", main / "first.py"),
        _tool_use(main, "Read", foreign / "read-only.py"),
        _tool_use(main, "NotebookEdit", worktree / "last.ipynb"),
    )
    for session_id, bad_path in (
        ("foreign-write", foreign / "outside.py"),
        ("tmp-write", tmp_path / "scratch.py"),
        ("dotfile-write", tmp_path / ".config" / "settings.json"),
    ):
        _write_transcript(
            bucket / f"{session_id}.jsonl",
            _user(main, "A later foreign write clears the candidate"),
            _tool_use(main, "Edit", worktree / "candidate.py"),
            _tool_use(main, "Write", bad_path),
        )

    rows = _by_session(_read(projects_root, main))

    assert _field(rows["last-same-repo"], "work_root") == str(worktree)
    for session_id in ("foreign-write", "tmp-write", "dotfile-write"):
        # A foreign/noisy write clears the previous candidate. Session cwd is
        # deliberately not a fallback for where the agent was writing.
        assert _field(rows[session_id], "work_root") == ""
