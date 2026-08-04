#!/usr/bin/env python3
"""Fail a commit when a QML file gains new qmllint findings.

WHY A RATCHET AND NOT A PLAIN GATE
==================================
`.claude/project-standards.md` P0 forbids committing QML with `unqualified`
findings. The tree does not meet that bar and cannot be made to: measured over
every tracked `.qml` file, qmllint reports ~1500 findings, of which ~500 are
`unqualified` — and the single largest contributor is `controller`, the PySide6
context property that most of the chrome is wired through. qmllint has no
concept of a context property, so those accesses are unresolvable by
construction; clearing them means replacing context properties with singletons
across the whole UI, not editing a binding.

So a hook that fails on any finding fails on every commit, and a hook that
fails on none (what we had — `qmllint` exits 0 on warnings, so the entry in
`.pre-commit-config.yaml` printed findings and passed regardless) is
decorative. It let a real `unqualified` violation into `BrowserPane.qml` that a
reviewer caught afterwards.

This ratchets instead: a file may keep the findings the baseline records for
it, and may never gain more. The count can only go down. `qmllint-baseline.json`
is the ledger, so growing the debt means committing a visible diff to it rather
than silently adding to a number nobody reads.

WHAT THE KEY IS, AND WHY IT IS COARSE
=====================================
Findings are counted per `(file, category)` — not per line, and not per
message. Line numbers move on every edit above them, and the `unqualified`
message is the bare string "Unqualified access" with no identifier in it, so
there is nothing finer to key on that would survive a day. The cost is real and
worth naming: swapping one finding for another inside the same file and
category passes. The guarantee is only that the total never grows.

TWO HOLES, BOTH DELIBERATE
==========================
1. Same-category swaps, per the paragraph above.
2. **Cross-file findings.** pre-commit passes only the files being committed,
   and qmllint findings are not local: removing a property from `Theme.qml` or
   `PillSurface.qml` raises `missing-property` counts in every consumer. A
   commit can therefore add findings across files it never touched and pass.
   Closing this means tallying the whole tree on every commit, which is the
   cost the per-file scope exists to avoid; run `--update` and read the diff
   when changing a shared component.

Usage:
    qmllint_gate.py <file.qml> ...   # check (what pre-commit runs)
    qmllint_gate.py --update         # regenerate the baseline from every
                                     # tracked .qml file
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import subprocess
import sys
from pathlib import Path

# Qt6's binary, explicitly: the bare `qmllint` on PATH is Qt5's on Arch and
# chokes on Qt6 syntax. Overridable so a different prefix (or a Qt bump under
# test) does not need an edit here.
QMLLINT = os.environ.get("SYMMETRIA_QMLLINT", "/usr/lib/qt6/bin/qmllint")

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = REPO_ROOT / "scripts" / "qmllint-baseline.json"

# qmllint severities that are NEVER baselined: a parse error stops the analysis,
# so the file's warning count afterwards is not comparable to anything, and
# grandfathering one in would hide whatever it masked.
#
# A deny-list rather than an allow-list, deliberately. Inverted, an unfamiliar
# severity (qmllint emits `debug` in some configurations) would be reclassified
# as a hard error and block every commit touching the file, with a message that
# reads like a real defect. Unknown severities are advisory.
_FATAL_TYPES = frozenset({"critical", "error"})


def _relative(path: Path) -> str:
    """Repo-relative POSIX path — the baseline's key.

    Absolute paths would encode this checkout's location, so the file would
    differ between the dev and stable worktrees for identical content.
    """
    return path.resolve().relative_to(REPO_ROOT).as_posix()


def _run_qmllint(files: list[Path]) -> dict:
    """qmllint's JSON report over `files`.

    JSON rather than the human output because the category is a discrete field
    there (`id`) instead of a bracketed suffix to be parsed back out of a
    message that also contains source text.
    """
    try:
        result = subprocess.run(
            [QMLLINT, "--json", "-", *[str(f) for f in files]],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        # A traceback out of a pre-commit hook reads as "the hook is broken",
        # and the reflex that follows is `--no-verify` — which disables the
        # gate permanently for a missing binary.
        raise SystemExit(
            f"qmllint not found at {QMLLINT}.\n"
            "Install Qt6's qmllint (`pacman -S qt6-declarative`) or point\n"
            "SYMMETRIA_QMLLINT at it."
        ) from None
    if not result.stdout.strip():
        raise SystemExit(
            f"{QMLLINT} produced no JSON report.\n"
            f"exit={result.returncode}\nstderr:\n{result.stderr}"
        )
    return json.loads(result.stdout)


def _tally(report: dict) -> tuple[dict[str, dict[str, int]], list[str]]:
    """Per-file, per-category finding counts, plus any hard errors.

    Errors come back separately because they bypass the ratchet entirely.
    """
    counts: dict[str, dict[str, int]] = {}
    errors: list[str] = []
    for entry in report.get("files", []):
        rel = _relative(Path(entry["filename"]))
        per_category: collections.Counter[str] = collections.Counter()
        for warning in entry.get("warnings", []):
            category = warning.get("id") or "uncategorised"
            if warning.get("type") in _FATAL_TYPES:
                errors.append(
                    f"{rel}:{warning.get('line')}:{warning.get('column')}: "
                    f"{warning.get('message')} [{category}]"
                )
                continue
            per_category[category] += 1
        counts[rel] = dict(sorted(per_category.items()))
    return counts, errors


def _tracked_qml_files() -> list[Path]:
    # `-z`, because plain `split()` breaks on any path containing a space (and
    # `git ls-files` would additionally quote it) — and this is the path that
    # WRITES the ledger, so a mangled name here corrupts the baseline itself.
    listing = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z", "*.qml"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [REPO_ROOT / name for name in listing.stdout.split("\0") if name]


def _load_baseline() -> dict[str, dict[str, int]]:
    if not BASELINE_PATH.exists():
        return {}
    try:
        return json.loads(BASELINE_PATH.read_text())
    except json.JSONDecodeError as exc:
        # The likely cause is a merge conflict left in the ledger. Naming the
        # file and the repair command keeps this from reading as a hook bug.
        raise SystemExit(
            f"{_relative(BASELINE_PATH)} is not valid JSON ({exc}).\n"
            "Resolve the conflict, or regenerate it with:\n"
            "  python3 scripts/qmllint_gate.py --update"
        ) from None


def update_baseline() -> int:
    files = _tracked_qml_files()
    if not files:
        print("no tracked .qml files", file=sys.stderr)
        return 1
    counts, errors = _tally(_run_qmllint(files))
    if errors:
        # A baseline written while an error is outstanding would silently
        # inherit whatever the error masked — qmllint stops analysing a file it
        # cannot parse, so its warning count is not comparable.
        print(
            "refusing to update the baseline: qmllint reports errors", file=sys.stderr
        )
        for error in errors:
            print(f"  {error}", file=sys.stderr)
        return 1
    # Files with nothing to report are dropped rather than stored as `{}`, so
    # the baseline shrinks as the tree is cleaned instead of accumulating rows.
    counts = {path: cats for path, cats in sorted(counts.items()) if cats}
    BASELINE_PATH.write_text(json.dumps(counts, indent=2) + "\n")
    total = sum(sum(cats.values()) for cats in counts.values())
    print(
        f"baseline: {len(counts)} files, {total} findings -> {_relative(BASELINE_PATH)}"
    )
    return 0


def regressions(
    counts: dict[str, dict[str, int]], baseline: dict[str, dict[str, int]]
) -> list[str]:
    """Categories whose count exceeds the baseline, as printable lines.

    A file absent from the baseline gets a budget of zero, so a NEW file must
    arrive clean or arrive with its debt recorded. That is deliberate: it is
    the only moment the choice is cheap.
    """
    failures: list[str] = []
    for rel, per_category in sorted(counts.items()):
        allowed = baseline.get(rel, {})
        for category, count in sorted(per_category.items()):
            budget = allowed.get(category, 0)
            if count > budget:
                failures.append(
                    f"{rel}: {category} findings {budget} -> {count} (+{count - budget})"
                )
    return failures


def check(paths: list[str]) -> int:
    files = [Path(p) for p in paths if p.endswith(".qml")]
    if not files:
        return 0
    # Named explicitly. qmllint reports a nonexistent path as an `import`
    # finding, which the ratchet then presents as "0 -> 1" — a typo would read
    # as a code regression.
    missing = [f for f in files if not f.exists()]
    if missing:
        raise SystemExit("no such file: " + ", ".join(str(f) for f in missing))
    counts, errors = _tally(_run_qmllint(files))
    failures = regressions(counts, _load_baseline())

    if not errors and not failures:
        return 0

    print("qmllint gate FAILED\n", file=sys.stderr)
    for error in errors:
        print(f"  error: {error}", file=sys.stderr)
    for failure in failures:
        print(f"  new:   {failure}", file=sys.stderr)
    if failures:
        print(
            "\nRun qmllint on the file to see them:\n"
            f"  {QMLLINT} {' '.join(str(f) for f in files)}\n"
            "\nFix them, or — if the increase is deliberate — record it:\n"
            "  python3 scripts/qmllint_gate.py --update\n"
            "and commit the baseline change alongside, so the added debt is\n"
            "visible in review rather than absorbed silently.",
            file=sys.stderr,
        )
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--update",
        action="store_true",
        help="regenerate the baseline from every tracked .qml file",
    )
    parser.add_argument("files", nargs="*", help="QML files to check")
    args = parser.parse_args()

    if args.update:
        return update_baseline()
    return check(args.files)


if __name__ == "__main__":
    raise SystemExit(main())
