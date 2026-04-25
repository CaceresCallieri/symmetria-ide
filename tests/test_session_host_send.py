"""Unit tests for `SessionHost.send_user_message` — the write path.

Covers the stdin JSONL envelope shape and the graceful-drop
behaviour when there is no running subprocess. The envelope field
shape is load-bearing (a prior iteration omitted `"role":"user"` and
`claude` silently discarded the turn — see docs/phases.md Step 1).

No real subprocess is spawned; `_proc` is swapped for a tiny fake
that captures stdin writes. This keeps the test hermetic and fast
while still exercising the real serialisation + lock path.
"""

from __future__ import annotations

import json

from symmetria_ide.session_host import SessionHost


class _FakeStdin:
    def __init__(self) -> None:
        self.chunks: list[str] = []
        self.flush_count = 0
        self.raise_on_write: BaseException | None = None

    def write(self, s: str) -> int:
        if self.raise_on_write is not None:
            raise self.raise_on_write
        self.chunks.append(s)
        return len(s)

    def flush(self) -> None:
        self.flush_count += 1


class _FakeProc:
    def __init__(self) -> None:
        self.stdin = _FakeStdin()


def test_send_user_message_writes_claude_envelope():
    """Verify the exact JSONL envelope shape `claude` accepts."""
    host = SessionHost()
    fake = _FakeProc()
    # Reach into the private slot — this is a seam for unit testing
    # the write path without standing up a real subprocess.
    host._proc = fake  # type: ignore[assignment]  # pyright: ignore[reportPrivateUsage]

    host.send_user_message("hello sir")

    assert fake.stdin.chunks, "expected a write to stdin"
    assert fake.stdin.flush_count == 1, "flush must follow every write"
    line = "".join(fake.stdin.chunks).rstrip("\n")
    payload = json.loads(line)
    assert payload == {
        "type": "user",
        "message": {"role": "user", "content": "hello sir"},
    }


def test_send_user_message_preserves_unicode():
    """Non-ASCII content must round-trip as UTF-8 inside the JSON."""
    host = SessionHost()
    fake = _FakeProc()
    host._proc = fake  # type: ignore[assignment]  # pyright: ignore[reportPrivateUsage]

    host.send_user_message("¡hola! 🎉 — é")

    line = "".join(fake.stdin.chunks).rstrip("\n")
    payload = json.loads(line)
    assert payload["message"]["content"] == "¡hola! 🎉 — é"


def test_send_user_message_without_subprocess_is_a_noop():
    """No subprocess = silent drop; callers shouldn't need `is_running` branching."""
    host = SessionHost()
    # Default `_proc` is None — nothing to write to.
    host.send_user_message("orphan turn")  # must not raise


def test_send_user_message_tolerates_broken_pipe():
    """If the subprocess vanished, write/flush surfaces are logged, not raised."""
    host = SessionHost()
    fake = _FakeProc()
    fake.stdin.raise_on_write = BrokenPipeError()
    host._proc = fake  # type: ignore[assignment]  # pyright: ignore[reportPrivateUsage]

    # Should log the exception via `log.exception` but NOT propagate.
    host.send_user_message("after the crash")

    # flush must NOT be called when write already raised — the exception
    # exits the try-block before reaching the flush line.
    assert fake.stdin.flush_count == 0, "flush must not be called after a failed write"
