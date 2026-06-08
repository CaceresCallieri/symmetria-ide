"""Shared JSONL line framing for the headless backends.

The agent SDK sidecar host
(``session_host.py``) speak newline-delimited JSON, and a future Tauri bridge
(``ide_service.py``) will too. This module is the single home for the
line-level encode/parse primitives so a third consumer doesn't copy them a
third time (the consolidation called for in ``docs/tauri-pivot-worklog.md``
§3.D.1).

Role-agnostic on purpose: it knows nothing about terminals, the SDK, Qt, or
which direction a stream flows. Callers own their stream lifecycle, locking
policy, EOF handling, and dispatch — this module only turns objects into lines
and lines into dicts.
"""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)

__all__ = ["parse_jsonl_line", "encode_jsonl_line"]


def parse_jsonl_line(line: str) -> dict | None:
    """Parse one JSONL line into an event dict, leniently.

    Returns ``None`` for blank / whitespace-only lines and for lines that
    aren't a valid JSON *object* (logged, not raised), so a reader loop can
    ``continue`` past junk without crashing.

    BOM-tolerance: a leading BOM is stripped defensively so a stray one
    doesn't wedge the parser.

    Note: this swallows the distinction between "blank" and "malformed".
    Callers that must report parse failures back to a peer (e.g. the REPL's
    client-facing ``error`` envelopes) should parse themselves rather than
    use this helper.
    """
    stripped = line.strip().lstrip("\ufeff")
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        log.exception("malformed JSONL line: %r", line[:200])
        return None
    if not isinstance(parsed, dict):
        log.warning("JSONL line is not an object: %r", parsed)
        return None
    return parsed


def encode_jsonl_line(obj: Any, *, ensure_ascii: bool = True) -> str:
    """Serialise ``obj`` to a single newline-terminated JSON line.

    ``ensure_ascii=False`` keeps non-ASCII characters literal (the REPL uses
    this so snapshot payloads stay compact and human-readable); the default
    ``True`` matches ``json.dumps`` and the sidecar command stream.
    """
    return json.dumps(obj, ensure_ascii=ensure_ascii) + "\n"
