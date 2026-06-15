"""Parser tests for `git log -z --pretty=format:<_LOG_FORMAT>` output.

Pure-data tests — no subprocess, no Qt, no temporary repos. We construct log
payloads directly as bytes (the wire format the parser ingests) and assert the
resulting `list[CommitRow]`. Mirrors the porcelain-parser test style in
`test_git_controller.py`: intent-revealing fixture helpers keep each case
reading like "this record shape produces this commit."

See `src/symmetria_ide/git_log_controller.py` for the parser and the field
order (`_LOG_FORMAT`).
"""

from __future__ import annotations

from symmetria_ide.git_log_controller import (
    _LOG_FIELD_COUNT,
    CommitRow,
    parse_git_log,
)

# Control characters the format uses — kept as locals so the fixtures read
# literally against the parser's `_FIELD_SEP` / `_RECORD_SEP`.
_US = "\x1f"  # field separator (within a commit)
_NUL = "\x00"  # record separator (between commits, git's `-z`)


def _commit(
    *,
    full_hash: str = "0123456789abcdef0123456789abcdef01234567",
    abbrev: str = "0123456",
    parents: str = "aaaaaaa",
    author_name: str = "jc",
    author_email: str = "jc@example.com",
    date_iso: str = "2026-06-15T12:00:00+00:00",
    relative: str = "2 hours ago",
    subject: str = "do a thing",
    body: str = "",
    refs: str = "",
    trailer: str = "",
) -> bytes:
    """Build one commit record: the 11 fields joined by the unit separator."""
    fields = [
        full_hash,
        abbrev,
        parents,
        author_name,
        author_email,
        date_iso,
        relative,
        subject,
        body,
        refs,
        trailer,
    ]
    assert len(fields) == _LOG_FIELD_COUNT
    return _US.join(fields).encode("utf-8")


def _join(*records: bytes) -> bytes:
    """Join commit records with NUL — matches `git log -z` separator semantics."""
    return _NUL.encode("utf-8").join(records)


# ---------------------------------------------------------------------------
# Empty / degenerate inputs
# ---------------------------------------------------------------------------


def test_empty_blob_returns_empty_list() -> None:
    assert parse_git_log(b"") == []


def test_only_separators_returns_empty_list() -> None:
    assert parse_git_log(_NUL.encode() * 3) == []


# ---------------------------------------------------------------------------
# Single commit — every field round-trips
# ---------------------------------------------------------------------------


def test_single_commit_parses_all_fields() -> None:
    blob = _commit(
        full_hash="f" * 40,
        abbrev="fffffff",
        parents="1111111",
        author_name="Ada Lovelace",
        author_email="ada@example.com",
        date_iso="2026-01-02T03:04:05+00:00",
        relative="3 days ago",
        subject="feat: add the analytical engine",
        body="Longer rationale here.",
        refs="HEAD -> dev, origin/dev",
    )
    rows = parse_git_log(blob)
    assert len(rows) == 1
    row = rows[0]
    assert isinstance(row, CommitRow)
    assert row.hash == "f" * 40
    assert row.abbrev_hash == "fffffff"
    assert row.parents == ("1111111",)
    assert row.author_name == "Ada Lovelace"
    assert row.author_email == "ada@example.com"
    assert row.author_date_iso == "2026-01-02T03:04:05+00:00"
    assert row.relative_date == "3 days ago"
    assert row.subject == "feat: add the analytical engine"
    assert row.body == "Longer rationale here."
    assert row.refs == "HEAD -> dev, origin/dev"
    # Future-proof seams default empty / false in v0.
    assert row.agent_id == ""
    assert row.additions == 0
    assert row.deletions == 0
    assert row.seen is False


# ---------------------------------------------------------------------------
# Parents — root, single, merge
# ---------------------------------------------------------------------------


def test_root_commit_has_no_parents() -> None:
    rows = parse_git_log(_commit(parents=""))
    assert rows[0].parents == ()


def test_single_parent() -> None:
    rows = parse_git_log(_commit(parents="abc1234"))
    assert rows[0].parents == ("abc1234",)


def test_merge_commit_has_multiple_parents() -> None:
    # `%P` emits parent hashes space-separated — a 2-parent merge, the case
    # the native graph ribbon (Phase 3) will route lanes for.
    rows = parse_git_log(_commit(parents="aaaaaaa bbbbbbb"))
    assert rows[0].parents == ("aaaaaaa", "bbbbbbb")


def test_octopus_merge_three_parents() -> None:
    rows = parse_git_log(_commit(parents="aaa bbb ccc"))
    assert rows[0].parents == ("aaa", "bbb", "ccc")


# ---------------------------------------------------------------------------
# Ordering, multiplicity, trailing separator robustness
# ---------------------------------------------------------------------------


def test_multiple_commits_preserve_order() -> None:
    blob = _join(
        _commit(abbrev="aaaaaaa", subject="newest"),
        _commit(abbrev="bbbbbbb", subject="middle"),
        _commit(abbrev="ccccccc", subject="oldest"),
    )
    rows = parse_git_log(blob)
    assert [r.abbrev_hash for r in rows] == ["aaaaaaa", "bbbbbbb", "ccccccc"]
    assert [r.subject for r in rows] == ["newest", "middle", "oldest"]


def test_trailing_separator_does_not_yield_phantom_row() -> None:
    # Some git versions terminate (rather than separate) entries under `-z`;
    # the parser must filter the resulting empty trailing chunk.
    blob = _join(_commit(subject="a"), _commit(subject="b")) + _NUL.encode()
    rows = parse_git_log(blob)
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# Body — empty, multi-line, trailing-newline stripping
# ---------------------------------------------------------------------------


def test_empty_body() -> None:
    rows = parse_git_log(_commit(body=""))
    assert rows[0].body == ""


def test_multiline_body_preserved() -> None:
    body = "First paragraph.\n\nSecond paragraph with detail.\n"
    rows = parse_git_log(_commit(body=body))
    # Interior newlines preserved; only the trailing newline is stripped.
    assert rows[0].body == "First paragraph.\n\nSecond paragraph with detail."


# ---------------------------------------------------------------------------
# Unicode
# ---------------------------------------------------------------------------


def test_unicode_in_subject_and_author() -> None:
    rows = parse_git_log(
        _commit(author_name="José Núñez", subject="fix: café ☕ rendering")
    )
    assert rows[0].author_name == "José Núñez"
    assert rows[0].subject == "fix: café ☕ rendering"


# ---------------------------------------------------------------------------
# Agent-correlation seam — the Symmetria-Agent-Id trailer
# ---------------------------------------------------------------------------


def test_agent_id_trailer_populates_field() -> None:
    rows = parse_git_log(_commit(trailer="12345_2"))
    assert rows[0].agent_id == "12345_2"


def test_absent_agent_trailer_is_empty() -> None:
    rows = parse_git_log(_commit(trailer=""))
    assert rows[0].agent_id == ""


def test_agent_id_trailer_whitespace_stripped() -> None:
    rows = parse_git_log(_commit(trailer="  98765_4  "))
    assert rows[0].agent_id == "98765_4"


# ---------------------------------------------------------------------------
# Malformed records
# ---------------------------------------------------------------------------


def test_record_with_too_few_fields_is_skipped() -> None:
    # A truncated record (fewer than 11 fields) is dropped, not crashed on; a
    # well-formed neighbour still parses.
    truncated = _US.join(["abc", "abc", ""]).encode("utf-8")
    blob = _join(truncated, _commit(subject="survives"))
    rows = parse_git_log(blob)
    assert len(rows) == 1
    assert rows[0].subject == "survives"
